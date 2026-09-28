from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
import yaml

from euboulia.cli import build_parser
from euboulia.playground.config import load_config
from euboulia.playground.kubernetes import Kubernetes, PlaygroundError, manifest, parse_gpus
from euboulia.playground.manager import ACTIVE, Manager, parse_arguments
from euboulia.playground.profiling import DEFAULT_ARGUMENTS, validate_options
from euboulia.playground.server import Server
from euboulia.playground.worker import Worker


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {},
        "spec": {"containers": [{"name": "workspace", "image": "test/cuda:fixed"}]},
    }
    (tmp_path / "pod.yaml").write_text(yaml.safe_dump(pod))
    config = {
        "version": 1,
        "storage": "./state",
        "run_timeout_seconds": 3,
        "setup_timeout_seconds": 20,
        "clusters": {
            "test": {
                "context": "test-context",
                "kubeconfig": "./kubeconfig",
                "pod_template": "./pod.yaml",
                "gpu_access": "shared-nvidia-runtime",
                "nodes": ["node-a"],
                "workdir": str(tmp_path / "remote"),
            }
        },
        "profiles": {"python": {"packages": []}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_local_configuration_is_explicit_and_private(config_path: Path) -> None:
    config = load_config(config_path)
    c = config.clusters["test"]
    assert c.kubeconfig == config_path.parent / "kubeconfig"
    assert c.namespace == "molou"
    assert c.context == "test-context"
    assert Kubernetes(c).prefix() == [
        "kubectl",
        "--context",
        "test-context",
        "--namespace",
        "molou",
        "--kubeconfig",
        str(c.kubeconfig),
    ]
    assert config.storage == config_path.parent / "state"
    profile = config.profiles["python"]
    assert profile.fingerprint != replace(profile, packages=("tilelang==0.1.7",)).fingerprint
    args = build_parser().parse_args(["playground", "--config", str(config_path), "--open"])
    assert args.open and args.port == 8766


@pytest.mark.parametrize(
    "update",
    [
        {"version": True},
        {"run_timeout_seconds": -1},
        {"profiles": {}},
        {"max_output_bytes": 0},
        {"typo": 1},
        {"profiles": {"python": {"packages": ["--target=/root"]}}},
        {"profiles": {"python": {"env": {"CUDA_VISIBLE_DEVICES": "7"}}}},
    ],
)
def test_malformed_config_rejected(config_path: Path, update: dict[str, Any]) -> None:
    raw = yaml.safe_load(config_path.read_text())
    raw.update(update)
    config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_config(config_path)


def test_exclusive_gpu_template_rejected(config_path: Path) -> None:
    path = config_path.parent / "pod.yaml"
    pod = yaml.safe_load(path.read_text())
    pod["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/B300": 1}}
    path.write_text(yaml.safe_dump(pod))
    with pytest.raises(ValueError, match="must not request extended"):
        load_config(config_path)


def test_pod_owned_shared_and_template_not_mutated(config_path: Path) -> None:
    c = load_config(config_path).clusters["test"]
    original = copy.deepcopy(c.template)
    session = {"id": "abc", "pod": "molou-playground-abc", "node": "node-a"}
    pod = manifest(c, session)
    assert c.template == original
    assert pod["metadata"]["namespace"] == "molou"
    assert pod["metadata"]["labels"]["euboulia.io/playground-session"] == "abc"
    assert pod["spec"]["nodeName"] == "node-a"
    assert not pod["spec"]["automountServiceAccountToken"]
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["NVIDIA_VISIBLE_DEVICES"] == "all"


def test_gpu_inventory_preserves_physical_indices() -> None:
    gpus = parse_gpus(
        '3, GPU-three, "NVIDIA, B300", 288000, 100, 0\n7, GPU-seven, B300, 288000, 2, 1'
    )
    assert [g["index"] for g in gpus] == [3, 7]
    assert gpus[0]["uuid"] == "GPU-three"
    with pytest.raises(PlaygroundError):
        parse_gpus("")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("--seq_len_k 2048\n--skip_ref_check", ["--seq_len_k", "2048", "--skip_ref_check"]),
        ("--seq_len_k 2048 \\\n  --skip_ref_check", ["--seq_len_k", "2048", "--skip_ref_check"]),
        ("--seq_len_k 2048 \\\r\n  --skip_ref_check", ["--seq_len_k", "2048", "--skip_ref_check"]),
        ('--label "two\\\n words"', ["--label", "two words"]),
        ("--label 'two\\\nwords'", ["--label", "two\\\nwords"]),
        ("--label " + "\\" * 2 + "\n--flag", ["--label", "\\", "--flag"]),
        ('--empty ""\n--label "test run"', ["--empty", "", "--label", "test run"]),
    ],
)
def test_multiline_arguments_preserve_quoting(text: str, expected: list[str]) -> None:
    assert parse_arguments(text) == expected


def test_many_kernel_arguments() -> None:
    lines = [
        "--batch_size 8192",
        "--seq_len_q 1",
        "--seq_len_k 2048",
        "--num_heads 64",
        "--latent_dim 512",
        "--rope_dim 64",
        "--page_size 64",
        "--in_dtype Float8E4M3FN",
        "--out_dtype BFloat16",
        "--acc_dtype Float32",
        "--lse_dtype Float32",
        "--softmax_scale 0.0625",
        "--output_scale 1.0",
        "--mma_qk_tiler_mn 128,128",
        "--mma_pv_tiler_mn 128,256",
        "--split_kv 1",
        "--warmup_iterations 20",
        "--iterations 100",
        "--skip_ref_check",
    ]
    argv = parse_arguments(" \\\n  ".join(lines))
    assert argv == [value for line in lines for value in line.split()]
    assert len(argv) == 37


def test_uid_precondition_and_foreign_pod_protection(config_path: Path, monkeypatch: Any) -> None:
    c = load_config(config_path).clusters["test"]
    session = {"id": "abc", "pod": "molou-playground-abc", "node": "node-a", "uid": "uid-a"}
    pod = manifest(c, session)
    pod["metadata"]["uid"] = "uid-a"
    calls = []

    def call(self: Any, args: list[str], **kw: Any) -> str:
        calls.append((args, kw))
        return json.dumps(pod) if args[0] == "get" else "{}"

    monkeypatch.setattr(Kubernetes, "call", call)
    Kubernetes(c).delete(session)
    assert json.loads(calls[-1][1]["data"])["preconditions"] == {"uid": "uid-a"}
    pod["metadata"]["uid"] = "replacement-pod"
    with pytest.raises(PlaygroundError, match="different owner or UID"):
        Kubernetes(c).delete(session)
    assert calls[-1][0][0] == "get"


def fake_gpu(tmp_path: Path, monkeypatch: Any) -> None:
    executable = tmp_path / "nvidia-smi"
    executable.write_text("#!/bin/sh\nprintf '3, GPU-test-three\\n'\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


def worker_request(tmp_path: Path, code: str) -> dict[str, Any]:
    return {
        "id": "a" * 32,
        "root": str(tmp_path / "remote"),
        "code": code,
        "gpu_index": 3,
        "gpu_uuid": "GPU-test-three",
        "setup_timeout": 20,
        "run_timeout": 3,
        "max_output_bytes": 1024,
        "profile": {"name": "python", "packages": [], "system_site_packages": True},
    }


def test_real_worker_streams_failures_and_reuses_venv(tmp_path: Path, monkeypatch: Any) -> None:
    fake_gpu(tmp_path, monkeypatch)
    request = worker_request(
        tmp_path,
        "import os,sys,json; print(os.environ['CUDA_VISIBLE_DEVICES']); "
        "print('RUNTIME='+json.dumps([sys.executable,sys.argv[0],os.getcwd()])); "
        "print('错误',file=sys.stderr); sys.exit(7)",
    )
    request["profile"]["env"] = {"MOLOU_TEST_ENV": "configured"}
    worker = Worker(request)
    assert worker.env["MOLOU_TEST_ENV"] == "configured"
    assert worker.env["CUDA_VISIBLE_DEVICES"] == "GPU-test-three"
    events = []
    monkeypatch.setattr(
        worker, "emit", lambda kind, **fields: events.append({"kind": kind, **fields})
    )
    worker.execute()
    assert events[-1] == {"kind": "result", "status": "failed", "exit_code": 7}
    assert any(e.get("stream") == "stderr" and "错误" in e["data"] for e in events)
    environment = next(e for e in events if e["kind"] == "environment" and e["status"] == "ready")
    assert environment["reused"] is False
    assert environment["python_version"] == sys.version.split()[0]
    assert environment["base_python"] == sys.executable
    execution = next(e for e in events if e["kind"] == "execution")
    output = "".join(e.get("data", "") for e in events if e.get("stream") == "stdout")
    runtime = json.loads(
        next(
            line.removeprefix("RUNTIME=")
            for line in output.splitlines()
            if line.startswith("RUNTIME=")
        )
    )
    assert execution["argv"] == [runtime[0], "-u", runtime[1]]
    assert Path(execution["cwd"]).resolve() == Path(runtime[2]).resolve()
    assert execution["env"] == {"CUDA_VISIBLE_DEVICES": "GPU-test-three"}
    assert "MOLOU_TEST_ENV" not in json.dumps(execution)
    ready = next((tmp_path / "remote/venvs").glob("*/.ready.json"))
    timestamp = ready.stat().st_mtime_ns
    request.update(id="b" * 32, code="print('reused')")
    second = Worker(request)
    monkeypatch.setattr(
        second, "emit", lambda kind, **fields: events.append({"kind": kind, **fields})
    )
    second.execute()
    assert events[-1]["status"] == "succeeded"
    assert ready.stat().st_mtime_ns == timestamp
    environments = [e for e in events if e["kind"] == "environment" and e["status"] == "ready"]
    assert environments[-1]["reused"] is True
    assert environments[-1]["path"] == environment["path"]


def test_worker_records_planned_environment_when_gpu_verification_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fake_gpu(tmp_path, monkeypatch)
    request = worker_request(tmp_path, "print('must not run')")
    request["gpu_index"] = 0
    worker = Worker(request)
    events = []
    monkeypatch.setattr(
        worker, "emit", lambda kind, **fields: events.append({"kind": kind, **fields})
    )
    with pytest.raises(RuntimeError, match="GPU identity verification failed"):
        worker.execute()
    assert events[0]["kind"] == "environment"
    assert events[0]["status"] == "preparing"
    assert events[0]["profile"] == "python"
    assert not any(e["kind"] == "execution" for e in events)


def test_worker_inherits_image_venv_and_dsl_pth(tmp_path: Path, monkeypatch: Any) -> None:
    fake_gpu(tmp_path, monkeypatch)
    parent = tmp_path / "image-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(parent)],
        check=True,
        capture_output=True,
    )
    python = str(parent / "bin/python")
    site_dir = Path(
        subprocess.check_output(
            [python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip()
    )
    (site_dir / "molou_image_dependency.py").write_text("VALUE = 'image dependency'\n")
    dsl_dir = site_dir / "dsl_packages"
    dsl_dir.mkdir()
    (dsl_dir / "molou_dsl_dependency.py").write_text("VALUE = 'nested DSL'\n")
    (site_dir / "dsl.pth").write_text(str(dsl_dir) + "\n")
    request = worker_request(
        tmp_path,
        "import molou_image_dependency, molou_dsl_dependency; "
        "print(molou_image_dependency.VALUE, molou_dsl_dependency.VALUE)",
    )
    worker_path = Path(__file__).parents[1] / "src/euboulia/playground/worker.py"
    result = subprocess.run(
        [python, "-u", "-c", worker_path.read_text()],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["status"] == "succeeded", events
    assert "image dependency nested DSL" in "".join(e.get("data", "") for e in events)


@pytest.mark.parametrize("mode", ["cancelled", "timed_out", "overflow"])
def test_worker_stop_timeout_and_bounded_output(
    tmp_path: Path, monkeypatch: Any, mode: str
) -> None:
    from euboulia.playground.worker import Stopped

    request = worker_request(tmp_path, "unused")
    worker = Worker(request)
    events = []
    monkeypatch.setattr(
        worker, "emit", lambda kind, **fields: events.append({"kind": kind, **fields})
    )
    if mode == "overflow":
        code = "import sys; sys.stdout.write('x'*100000); sys.stderr.write('error')"
        assert worker.command([sys.executable, "-c", code], time.monotonic() + 5, tmp_path) == 0
        assert worker.truncated and worker.output_bytes <= 1024
        assert sum(e.get("data", "").count("Output limit reached") for e in events) == 1
    else:
        timer = None
        if mode == "cancelled":
            timer = threading.Timer(0.2, worker.cancel_file.touch)
            timer.start()
        try:
            with pytest.raises(Stopped) as exc:
                worker.command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    time.monotonic() + 0.6,
                    tmp_path,
                )
            assert exc.value.status == mode
        finally:
            if timer:
                timer.join()


def test_manager_streaming_history_and_session_lock(config_path: Path, monkeypatch: Any) -> None:
    config = load_config(config_path)
    fake_gpu(config_path.parent, monkeypatch)

    def call(self: Any, args: list[str], **kw: Any) -> str:
        if args[0] == "create":
            return kw["data"]
        if args[:2] == ["exec", "missing"]:
            raise PlaygroundError("missing")
        return "{}"

    def inspect(self: Any, session: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {
            "metadata": {"uid": "uid-test"},
            "status": {
                "phase": "Running",
                "hostIP": "10.0.0.3",
                "containerStatuses": [{"name": "workspace", "ready": True}],
            },
        }

    monkeypatch.setattr(Kubernetes, "call", call)
    monkeypatch.setattr(Kubernetes, "inspect", inspect)
    monkeypatch.setattr(
        Kubernetes,
        "inventory",
        lambda *a: [{"index": 3, "uuid": "GPU-test-three", "name": "Test GPU"}],
    )
    monkeypatch.setattr(
        Kubernetes, "exec_args", lambda self, session, cmd: [sys.executable, *cmd[1:]]
    )
    manager = Manager(config)
    try:
        with pytest.raises(PlaygroundError, match="another playground"):
            Manager(config)
        session = manager.connect("test", "node-a")
        deadline = time.monotonic() + 3
        while (
            manager.sessions[session["id"]]["status"] == "starting" and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert manager.sessions[session["id"]]["status"] == "ready"
        payload = {
            "session": session["id"],
            "gpu_index": 3,
            "profile": "python",
            "code": (
                "import argparse,json,time\n"
                "parser=argparse.ArgumentParser()\n"
                "parser.add_argument('--seq_len_k',type=int)\n"
                "parser.add_argument('--label')\n"
                "parser.add_argument('--literal')\n"
                "parser.add_argument('--empty')\n"
                "print('ARGS='+json.dumps(vars(parser.parse_args())))\n"
                "print('first',flush=True); time.sleep(0.4); print('last')\n"
            ),
            "arguments": (
                '--seq_len_k 128 \\\n --label "two words" \\\r\n'
                '--literal "$(echo unexpected); *"\n--empty ""'
            ),
        }
        for invalid in ('--label "unclosed', "\x00", "a" * 8193, "a " * 257, []):
            with pytest.raises(ValueError, match="arguments"):
                manager.submit(payload | {"arguments": invalid})
        assert not manager.runs
        with pytest.raises(ValueError, match="inventory"):
            manager.submit(payload | {"gpu_index": 0})
        run = manager.submit(payload)
        with pytest.raises(PlaygroundError, match="active run"):
            manager.submit(payload)
        with pytest.raises(PlaygroundError, match="stop the active"):
            manager.release(session["id"])
        deadline = time.monotonic() + 20
        while manager.runs[run["id"]]["status"] in ACTIVE and time.monotonic() < deadline:
            time.sleep(0.02)
        assert manager.runs[run["id"]]["status"] == "succeeded"
        events = manager.events(run["id"], 0)
        output = "".join(e.get("data", "") for e in events["events"])
        assert "first" in output and "last" in output
        parsed_args = json.loads(
            next(line[5:] for line in output.splitlines() if line.startswith("ARGS="))
        )
        assert parsed_args == {
            "seq_len_k": 128,
            "label": "two words",
            "literal": "$(echo unexpected); *",
            "empty": "",
        }
        assert "Node IP: 10.0.0.3" in output
        assert f"Pod: molou / {session['pod']}" in output
        assert "Venv ready:" in output and "(created)" in output
        assert "Node IP: 10.0.0.3" in events["events"][0]["data"]
        run_dir = config.storage / "runs" / run["id"]
        execution = json.loads((run_dir / "execution.json").read_text())
        command = next(
            line.removeprefix("Command (inside Pod): ")
            for line in output.splitlines()
            if line.startswith("Command (inside Pod): ")
        )
        assert shlex.split(command) == execution["argv"]
        saved_run = json.loads((run_dir / "run.json").read_text())
        assert saved_run["node_ip"] == "10.0.0.3"
        assert saved_run["pod"] == session["pod"]
        assert saved_run["arguments"] == payload["arguments"]
        assert saved_run["args"] == execution["argv"][3:]
        assert saved_run["execution"] == execution
        assert manager.snapshot()["runs"][0]["execution"] == execution
        assert manager.events(run["id"], events["next"])["events"] == []
        assert manager.code(run["id"]) == payload["code"]
    finally:
        manager.close()
    # Existing history predates inline execution metadata; it must still render.
    saved_run.pop("execution")
    (run_dir / "run.json").write_text(json.dumps(saved_run))
    restarted = Manager(config)
    try:
        assert restarted.snapshot()["runs"][0]["execution"] == execution
        assert restarted.sessions[session["id"]]["status"] == "disconnected"
        assert restarted.runs[run["id"]]["status"] == "succeeded"
        assert restarted.runs[run["id"]]["arguments"] == payload["arguments"]
        assert restarted.events(run["id"], 0)["events"] == events["events"]
        reconnected = restarted.connect("test", "node-a")
        assert reconnected["id"] == session["id"]
        deadline = time.monotonic() + 3
        while restarted.sessions[session["id"]]["status"] == "starting":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert restarted.sessions[session["id"]]["status"] == "ready"
    finally:
        restarted.close()


def test_http_security_and_assets(config_path: Path) -> None:
    manager = Manager(load_config(config_path))
    server = Server(manager, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        try:
            connection.request(
                method, path, body="{}" if method == "POST" else None, headers=headers or {}
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        assert request("GET", "/")[0] == 200
        assert b"Python source code" in request("GET", "/")[1]
        assert request("GET", "/app.js")[0] == 200
        assert request("GET", "/api/config", {"Host": "attacker.example"})[0] == 403
        assert request("GET", "/api/state", {"Origin": "https://attacker.example"})[0] == 403
        assert request("POST", "/api/runs")[0] == 403
        public = json.loads(request("GET", "/api/config")[1])
        assert "kubeconfig" not in json.dumps(public)
        headers = {"X-Playground-Token": public["token"], "Content-Type": "application/json"}
        assert request("POST", "/api/runs", headers)[0] == 400
        assert request("GET", "/api/runs/not-a-run/code")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        manager.close()


@pytest.mark.parametrize(
    "mode,args",
    [
        ("ncu", DEFAULT_ARGUMENTS["ncu"]),
        (
            "ncu",
            "--set detailed --kernel-name-base function "
            "--kernel-name 'regex:Sm100SimpleCopyKernel' --launch-count 1",
        ),
        ("ncu", "--set detailed\n--kernel-name 'regex:copy|add'\n--launch-count=2 --nvtx"),
        ("nsys", "--trace=cuda,nvtx --sample none --capture-range cudaProfilerApi"),
        ("run", ""),
    ],
)
def test_profiler_options_accept_quoted_multiline(mode: str, args: str) -> None:
    validate_options(mode, parse_arguments(args))


@pytest.mark.parametrize(
    "mode,args",
    [
        ("ncu", "--export /tmp/other"),
        ("ncu", "-o/tmp/other"),
        ("ncu", "--config-file-path /tmp/config"),
        ("ncu", "--devices 0"),
        ("ncu", "--clock-control base"),
        ("ncu", "--set detailed python another.py"),
        ("ncu", "--kernel-name"),
        ("nsys", "--output=x"),
        ("nsys", "--sample system-wide"),
        ("nsys", "--command-file options.txt"),
        ("run", "--set basic"),
        ("bogus", ""),
    ],
)
def test_profiler_options_protect_target_gpu_and_output(mode: str, args: str) -> None:
    with pytest.raises(ValueError):
        validate_options(mode, parse_arguments(args))


def fake_profiler(tmp_path: Path, monkeypatch: Any, mode: str) -> None:
    binary = tmp_path / mode
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
case = os.environ.get("TEST_PROFILER_CASE", "")
if args == ["--version"]:
    print("test profiler 1.0")
elif "--import" in args or args[0] == "stats":
    if case == "export-fail":
        print("partial text", flush=True)
        print("export failed", file=sys.stderr)
        sys.exit(4)
    print("指标 " * 2000)
elif "--export" in args or "--output" in args:
    key = "--export" if "--export" in args else "--output"
    i = args.index(key)
    target = args[i+2:]
    status = subprocess.call(target)
    if case != "missing":
        suffix = ".ncu-rep" if key == "--export" else ".nsys-rep"
        Path(args[i+1] + suffix).write_text(json.dumps({
            "target": target, "gpu": os.environ["CUDA_VISIBLE_DEVICES"],
            "cwd": os.getcwd(), "options": args[:i],
        }))
    sys.exit(status)
"""
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


@pytest.mark.parametrize("mode", ["ncu", "nsys"])
def test_profiling_pipeline_preserves_target_and_untruncated_text(
    config_path: Path,
    monkeypatch: Any,
    mode: str,
) -> None:
    fake_profiler(config_path.parent, monkeypatch, mode)
    config = load_config(config_path)
    manager = Manager(config)
    rid = "d" * 32
    manager.runs[rid] = {
        "id": rid,
        "session": "session",
        "status": "queued",
        "created_at": 1,
        "mode": mode,
        "reports": {},
    }
    req = worker_request(config_path.parent, "unused")
    req.update(
        id=rid,
        mode=mode,
        profiler_args=parse_arguments(DEFAULT_ARGUMENTS[mode]),
        export_sass=False,  # Legacy requests cannot suppress the required SASS report.
        max_report_bytes=config.max_report_bytes,
    )
    worker = Worker(req)
    events = []

    def emit(kind: str, **fields: Any) -> None:
        event = {"kind": kind, **fields}
        events.append(event)
        manager._event(rid, event)

    monkeypatch.setattr(worker, "emit", emit)
    # Profiling wraps a complete command: module/file modes and quoted args survive.
    target = [sys.executable, "-c", "print('x'*2048)", "--label", "two words", "$(literal)"]
    try:
        worker.profile_command(target)
        assert manager.runs[rid]["status"] == "succeeded"
        info = manager.runs[rid]["profiling"]
        assert info["version"] == "test profiler 1.0"
        raw = Path(info["remote_reports"][0])
        captured = json.loads(raw.read_text())
        assert captured["target"] == target
        assert captured["gpu"] == req["gpu_uuid"]
        assert captured["cwd"] == str(worker.run_dir)
        if mode == "ncu":
            assert captured["options"][-2:] == ["--clock-control", "none"]
            assert captured["options"][2:-2] == [
                "--kernel-name-base",
                "function",
                "--kernel-name",
                "regex:.*",
                "--set",
                "detailed",
                "--launch-count",
                "1",
            ]
            exports = [e["argv"] for e in events if e.get("stage") == "export"]
            assert [argv[1:] for argv in exports] == [
                ["--import", str(raw), "--page", "details", "--print-details", "all"],
                ["--import", str(raw), "--page", "source", "--print-source", "sass"],
            ]
        assert worker.truncated  # Console cap does not truncate reports.
        reports = manager.runs[rid]["reports"]
        assert set(reports) == ({"details.txt", "sass.txt"} if mode == "ncu" else {"stats.txt"})
        for name, record in reports.items():
            assert record["status"] == "ready"
            assert manager.report_path(rid, name).read_text() == "指标 " * 2000 + "\n"
            local_text = manager.report_path(rid, name).read_bytes()
            assert (worker.run_dir / name).read_bytes() == local_text
        assert manager.runs[rid]["execution"]["stage"] == "capture"
        assert all(c["stage"] == "export" for c in manager.runs[rid]["export_commands"])
        assert not any(e["kind"] == "report_chunk" for e in manager.events(rid, 0)["events"])
        # Simulate deletion of all remote files: local text remains downloadable.
        import shutil

        shutil.rmtree(worker.run_dir)
    finally:
        manager.close()
    restarted = Manager(config)
    server = Server(restarted, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in restarted.runs[rid]["reports"]:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", f"/api/runs/{rid}/reports/{name}")
            response = connection.getresponse()
            assert response.status == 200
            assert "attachment" in response.getheader("Content-Disposition", "")
            assert response.read().decode() == "指标 " * 2000 + "\n"
            connection.close()
        with pytest.raises(KeyError):
            restarted.report_path(rid, "../../run.json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        restarted.close()


@pytest.mark.parametrize("case", ["missing", "export-fail", "limit"])
def test_profiler_missing_and_partial_reports(
    tmp_path: Path,
    monkeypatch: Any,
    case: str,
) -> None:
    fake_profiler(tmp_path, monkeypatch, "ncu")
    req = worker_request(tmp_path, "unused")
    req.update(mode="ncu", profiler_args=[], max_report_bytes=1024)
    req["profile"]["env"] = {"TEST_PROFILER_CASE": case}
    worker = Worker(req)
    events = []
    monkeypatch.setattr(worker, "emit", lambda k, **v: events.append({"kind": k, **v}))
    worker.profile_command([sys.executable, "-c", "pass"])
    if case == "missing":
        assert events[-1]["status"] == "failed"
        assert not any(e["kind"] == "report_begin" for e in events)
    else:
        reports = [e for e in events if e["kind"] == "report_end"]
        assert {report["name"] for report in reports} == {"details.txt", "sass.txt"}
        for report in reports:
            assert report["status"] == "partial"
            text = "".join(
                e["data"]
                for e in events
                if e["kind"] == "report_chunk" and e["name"] == report["name"]
            )
            assert len(text.encode()) <= 1024
            assert report["truncated"] == (case == "limit")
        assert events[-1]["status"] == ("succeeded" if case == "limit" else "failed")


@pytest.mark.parametrize("cancel", [False, True])
def test_profiler_stop_allows_finalization(
    tmp_path: Path,
    monkeypatch: Any,
    cancel: bool,
) -> None:
    from euboulia.playground.worker import Stopped

    worker = Worker(worker_request(tmp_path, "unused"))
    monkeypatch.setattr(worker, "emit", lambda *a, **kw: None)
    code = (
        "import signal,time,pathlib; "
        "signal.signal(signal.SIGINT, lambda *a: "
        "(pathlib.Path('finalized').write_text('yes'),exit(0))); "
        "pathlib.Path('started').touch(); time.sleep(30)"
    )
    timer = None
    if cancel:

        def stop_when_started() -> None:
            deadline = time.monotonic() + 3
            while not (tmp_path / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.cancel_file.touch()

        timer = threading.Thread(target=stop_when_started)
        timer.start()
    try:
        with pytest.raises(Stopped) as exc:
            worker.command(
                [sys.executable, "-c", code], time.monotonic() + 1, tmp_path, graceful=True
            )
        assert exc.value.status == ("cancelled" if cancel else "timed_out")
        assert (tmp_path / "finalized").read_text() == "yes"
    finally:
        if timer:
            timer.join()


@pytest.mark.parametrize("legacy_sass", [{}, {"export_sass": False}, {"export_sass": True}])
def test_profiling_submission_uses_separate_timeout_and_persists_options(
    config_path: Path,
    monkeypatch: Any,
    legacy_sass: dict[str, bool],
) -> None:
    config = load_config(config_path)
    manager = Manager(config)
    calls = []
    monkeypatch.setattr(manager, "_spawn", lambda *args: calls.append(args))
    manager.sessions["s"] = {
        "id": "s",
        "status": "ready",
        "cluster": "test",
        "node": "node-a",
        "namespace": "molou",
        "pod": "test-pod",
        "gpus": [{"index": 3, "uuid": "GPU-test-three"}],
    }
    payload = {"session": "s", "profile": "python", "gpu_index": 3, "code": "pass"}
    try:
        with pytest.raises(ValueError, match="timeout"):
            manager.submit(payload | {"timeout_seconds": config.run_timeout + 1})
        with pytest.raises(ValueError, match="export_sass"):
            manager.submit(payload | {"mode": "nsys", "export_sass": True})
        with pytest.raises(ValueError, match="unsupported ncu"):
            manager.submit(payload | {"mode": "ncu", "profiler_arguments": "--export=x"})
        run = manager.submit(payload | {"mode": "ncu"} | legacy_sass)
        request = calls[0][2]
        assert run["timeout_seconds"] == config.profiling_timeout
        assert run["profiler_arguments"] == DEFAULT_ARGUMENTS["ncu"]
        assert run["export_sass"] is True
        assert request["gpu_uuid"] == "GPU-test-three"
        assert request["export_sass"] is True
        assert request["export_timeout"] == config.export_timeout
        assert request["max_report_bytes"] == config.max_report_bytes
        assert json.loads((config.storage / "runs" / run["id"] / "run.json").read_text()) == run
        manager._event(run["id"], {"kind": "result", "status": "failed", "exit_code": None})
    finally:
        manager.close()


def test_export_timeout_preserves_partial_text(tmp_path: Path, monkeypatch: Any) -> None:
    from euboulia.playground.worker import Stopped

    worker = Worker(worker_request(tmp_path, "unused"))
    events = []
    monkeypatch.setattr(worker, "emit", lambda k, **v: events.append({"kind": k, **v}))
    with pytest.raises(Stopped):
        worker.command(
            [sys.executable, "-c", "import time; print('saved',flush=True); time.sleep(30)"],
            time.monotonic() + 0.5,
            tmp_path,
            report="stats.txt",
        )
    assert events[-1]["kind"] == "report_end"
    assert events[-1]["status"] == "partial"
    assert (tmp_path / "stats.txt").read_text() == "saved\n"


def test_missing_profiler_does_not_execute_target(tmp_path: Path, monkeypatch: Any) -> None:
    req = worker_request(tmp_path, "unused")
    req.update(mode="ncu", profiler_args=[])
    worker = Worker(req)
    worker.env["PATH"] = str(tmp_path / "missing-tools")
    with pytest.raises(RuntimeError, match="not installed"):
        worker.profile_command([sys.executable, "-c", "raise AssertionError('must not run')"])
