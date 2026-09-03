from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import euboulia.optimization.target as target_module
from euboulia.optimization.target import (
    BuildCommandSpec,
    BuildSpec,
    ReadinessSpec,
    ServerArgumentPatch,
    SGLangTargetController,
    TargetBuildError,
    TargetChangeSet,
    TargetLaunchError,
    TargetOwnershipError,
    TargetSpec,
)

SERVER_SOURCE = r"""#!/usr/bin/env python3
import argparse
import http.server
import json
import os
import signal

parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--model-path", required=True)
parser.add_argument("--marker", default="baseline")
parser.add_argument("--ignore-term", action="store_true")
args = parser.parse_args()

if args.command != "serve":
    raise SystemExit(2)
if args.ignore_term:
    signal.signal(signal.SIGTERM, lambda *_: None)

print(json.dumps({
    "cuda": os.environ.get("CUDA_VISIBLE_DEVICES"),
    "declared": os.environ.get("EUBOULIA_DECLARED"),
    "undeclared": os.environ.get("EUBOULIA_UNDECLARED_SECRET", "missing"),
    "context": os.environ.get("EUBOULIA_CONTEXT"),
    "marker": args.marker,
    "model": args.model_path,
}), flush=True)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, *_):
        return

class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

Server(("127.0.0.1", args.port), Handler).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _fake_sglang(workspace: Path) -> Path:
    executable = workspace / "sglang"
    executable.write_text(SERVER_SOURCE, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _target_spec(workspace: Path, port: int, *, ignore_term: bool = False) -> TargetSpec:
    _fake_sglang(workspace)
    argv = [
        "{workspace}/sglang",
        "serve",
        "--port",
        str(port),
        "--model-path",
        "{model}",
    ]
    if ignore_term:
        argv.append("--ignore-term")
    endpoint = f"http://127.0.0.1:{port}"
    return TargetSpec(
        provider="sglang",
        model="test/model",
        launch_argv=tuple(argv),
        launch_env={
            "EUBOULIA_DECLARED": "present",
            "EUBOULIA_CONTEXT": "{workspace}|{model}|{endpoint}|{run_id}|{trial_id}",
        },
        endpoint=endpoint,
        readiness=ReadinessSpec(
            url=f"{endpoint}/health_generate",
            timeout_seconds=5.0,
            interval_seconds=0.01,
        ),
        gpus=(0, "GPU-acde"),
        shutdown_timeout_seconds=0.1,
        provenance={"recipe": "unit-test"},
    )


def test_server_argument_patch_overlays_semantically_and_is_immutable() -> None:
    baseline = (
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        "test/model",
        "--tp-size=1",
        "--trust-remote-code",
        "--host",
        "127.0.0.1",
    )
    patch = ServerArgumentPatch(
        set={"--tp-size": "2", "--mem-fraction-static": "0.8"},
        remove=("--trust-remote-code",),
    )

    assert patch.apply(baseline) == (
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        "test/model",
        "--tp-size",
        "2",
        "--host",
        "127.0.0.1",
        "--mem-fraction-static",
        "0.8",
    )
    assert baseline[5] == "--tp-size=1"
    with pytest.raises(TypeError):
        patch.set["--tp-size"] = "4"  # type: ignore[index]
    with pytest.raises(ValueError, match="both set and removed"):
        ServerArgumentPatch(set={"--tp-size": "2"}, remove=("--tp-size",))
    with pytest.raises(ValueError, match="duplicate long options"):
        patch.apply(("sglang", "--tp-size=1", "--tp-size", "2"))
    assert ServerArgumentPatch(set={"--tp-size": "2"}).apply(
        ("sglang", "--lora-path", "a", "--lora-path", "b")
    ) == ("sglang", "--lora-path", "a", "--lora-path", "b", "--tp-size", "2")
    assert ServerArgumentPatch(remove=("--trust-remote-code",)).apply(
        ("sglang", "--trust-remote-code", "--", "--trust-remote-code")
    ) == ("sglang", "--", "--trust-remote-code")


def test_target_and_build_reject_unknown_placeholders_during_declaration() -> None:
    with pytest.raises(ValueError, match="unknown placeholder"):
        TargetSpec(
            provider="sglang",
            model="test/model",
            launch_argv=("sglang", "--model-path", "{undeclared}"),
            endpoint="http://127.0.0.1:30000",
            readiness=ReadinessSpec("http://127.0.0.1:30000/health_generate"),
            gpus=(0,),
        )

    with pytest.raises(ValueError, match="unknown placeholder"):
        BuildCommandSpec(name="invalid", argv=("python", "{model}"))


def test_start_wait_ready_and_stop_preserve_owned_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EUBOULIA_UNDECLARED_SECRET", "must-not-leak")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = SGLangTargetController()
    spec = _target_spec(workspace, _free_port())
    changes = TargetChangeSet(arg_patch=ServerArgumentPatch(set={"--marker": "candidate"}))

    handle = controller.start(
        workspace,
        spec,
        changes,
        tmp_path / "evidence",
        run_id="run-1",
        trial_id="candidate-1",
    )
    try:
        controller.wait_ready(handle)
        launch_record = json.loads(handle.stdout_path.read_text(encoding="utf-8").splitlines()[0])
        assert launch_record == {
            "cuda": "0,GPU-acde",
            "declared": "present",
            "undeclared": "missing",
            "context": (f"{workspace.resolve()}|test/model|{spec.endpoint}|run-1|candidate-1"),
            "marker": "candidate",
            "model": "test/model",
        }
        ready_manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
        assert ready_manifest["status"] == "ready"
        assert ready_manifest["handle"]["pid"] == handle.pid
        assert ready_manifest["fixed_gpu_ids"] == ["0", "GPU-acde"]
        assert "EUBOULIA_UNDECLARED_SECRET" not in ready_manifest["environment_keys"]
    finally:
        controller.stop(handle)

    stopped_manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    assert stopped_manifest["status"] == "stopped"
    assert stopped_manifest["sigterm_sent"] is True
    assert stopped_manifest["forced_kill"] is False
    assert handle.stdout_path.is_file()
    assert handle.stderr_path.is_file()


def test_non_sglang_entrypoint_is_rejected_before_process_start(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "should-not-exist"
    spec = TargetSpec(
        provider="sglang",
        model="test/model",
        launch_argv=(
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('unsafe')",
            str(marker),
        ),
        endpoint="http://127.0.0.1:30000",
        readiness=ReadinessSpec("http://127.0.0.1:30000/health_generate"),
        gpus=(0,),
    )

    with pytest.raises(TargetLaunchError, match=r"SGLang|sglang"):
        SGLangTargetController().start(
            workspace,
            spec,
            TargetChangeSet(),
            tmp_path / "evidence",
            run_id="run",
            trial_id="trial",
        )

    assert not marker.exists()


def test_start_terminates_owned_process_if_initial_manifest_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = SGLangTargetController()
    spec = _target_spec(workspace, _free_port())
    captured_pid: int | None = None
    real_abort = target_module._abort_unidentified_process

    def record_abort(process: object, process_group_id: int) -> None:
        nonlocal captured_pid
        captured_pid = process.pid  # type: ignore[attr-defined]
        real_abort(process, process_group_id)  # type: ignore[arg-type]

    def fail_manifest_write(state: object, status: str) -> None:
        del state, status
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(target_module, "_abort_unidentified_process", record_abort)
    monkeypatch.setattr(controller, "_write_service_manifest", fail_manifest_write)

    with pytest.raises(TargetLaunchError, match="owned process was terminated"):
        controller.start(
            workspace,
            spec,
            TargetChangeSet(),
            tmp_path / "evidence",
            run_id="run-manifest-failure",
            trial_id="trial-manifest-failure",
        )

    assert captured_pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(captured_pid, 0)


def test_handles_are_instance_owned_unforgeable_single_use_capabilities(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = SGLangTargetController()
    spec = _target_spec(workspace, _free_port())
    handle = controller.start(
        workspace,
        spec,
        TargetChangeSet(),
        tmp_path / "evidence",
        run_id="run-owned",
        trial_id="trial-owned",
    )
    try:
        controller.wait_ready(handle)
        with pytest.raises(TargetOwnershipError, match="not issued"):
            SGLangTargetController().stop(handle)
        with pytest.raises(TargetOwnershipError, match=r"signature|forged"):
            controller.stop(replace(handle, pid=handle.pid + 1))
    finally:
        controller.stop(handle)

    with pytest.raises(TargetOwnershipError, match="consumed"):
        controller.stop(handle)
    with pytest.raises(TargetOwnershipError, match="already been used"):
        controller.start(
            workspace,
            spec,
            TargetChangeSet(),
            tmp_path / "evidence-2",
            run_id="run-owned",
            trial_id="trial-owned",
        )


def test_stop_escalates_only_owned_process_group_after_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = SGLangTargetController()
    spec = _target_spec(workspace, _free_port(), ignore_term=True)
    handle = controller.start(
        workspace,
        spec,
        TargetChangeSet(),
        tmp_path / "evidence",
        run_id="run-timeout",
        trial_id="trial-timeout",
    )
    controller.wait_ready(handle)

    controller.stop(handle)

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "stopped"
    assert manifest["sigterm_sent"] is True
    assert manifest["forced_kill"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(handle.pid, 0)


def test_build_is_finite_sequential_and_stops_with_failure_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    third_marker = workspace / "third-ran"
    spec = BuildSpec(
        commands=(
            BuildCommandSpec(
                name="first",
                argv=(
                    sys.executable,
                    "-c",
                    "import pathlib; pathlib.Path('first-ran').write_text('yes')",
                ),
            ),
            BuildCommandSpec(
                name="fail",
                argv=(
                    sys.executable,
                    "-c",
                    "import sys; print('expected failure', file=sys.stderr); raise SystemExit(7)",
                ),
            ),
            BuildCommandSpec(
                name="must-not-run",
                argv=(
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('unsafe')",
                    str(third_marker),
                ),
            ),
        ),
        default_timeout_seconds=5.0,
    )

    with pytest.raises(TargetBuildError, match="exit status 7") as captured:
        SGLangTargetController().build(workspace, spec, tmp_path / "evidence")

    error = captured.value
    assert len(error.results) == 2
    assert error.results[0].succeeded
    assert not error.results[1].succeeded
    assert (workspace / "first-ran").read_text(encoding="utf-8") == "yes"
    assert not third_marker.exists()
    assert "expected failure" in error.results[1].stderr_path.read_text(encoding="utf-8")
    manifest = json.loads(error.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["declared_command_count"] == 3
    assert manifest["completed_command_count"] == 2
