import json
from pathlib import Path

import pytest
import yaml
from test_config import VALID_CONFIG, write_config
from test_optimization_config import v3_managed_document, write_input_template

import euboulia.cli as cli
from euboulia.cli import build_parser, main
from euboulia.control import ControlStore
from euboulia.optimization.config import load_optimization_config
from euboulia.optimization.events import EventLedger, EventType, OptimizationEvent
from euboulia.optimization.profiling import ProfileCaptureError
from euboulia.remote import OwnedPod


def test_plan_cli_renders_without_execution(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG)

    exit_code = main(["plan", "--recipe", str(path)])

    assert exit_code == 0
    assert not (tmp_path / "artifacts").exists()


def test_evaluate_cli_returns_rejection_status(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, VALID_CONFIG)
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(
        json.dumps({"completed": 16, "failed": 0, "output_throughput": 100.0}),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps({"completed": 16, "failed": 0, "output_throughput": 99.0}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "evaluate",
            "--config",
            str(config_path),
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
        ]
    )

    assert exit_code == 1


def test_optimize_plan_cli_runs_read_only_example() -> None:
    repository = Path(__file__).resolve().parents[1]
    config = repository / "examples/optimization-sglang.yaml"

    exit_code = main(["optimize", "plan", "--recipe", str(config)])

    assert exit_code == 0


def test_optimize_run_parser_exposes_independent_managed_capabilities() -> None:
    args = build_parser().parse_args(
        [
            "optimize",
            "run",
            "--recipe",
            "recipe.yaml",
            "--name",
            "baseline",
            "--apply-patches",
            "--run-profiles",
            "--run-evaluations",
            "--run-builds",
            "--manage-services",
        ]
    )

    assert args.apply_patches is True
    assert args.name == "baseline"
    assert not hasattr(args, "run_id")
    assert args.run_profiles is True
    assert args.run_evaluations is True
    assert args.run_builds is True
    assert args.manage_services is True


def test_target_plan_cli_inspects_dsv4_scenario_read_only() -> None:
    repository = Path(__file__).resolve().parents[1]
    config = repository / "examples/scenarios/dsv4-megamoe.yaml"

    exit_code = main(["target", "plan", "--recipe", str(config), "--json"])

    assert exit_code == 0


@pytest.mark.parametrize("json_output", [False, True])
def test_target_plan_describes_model_download_without_creating_cache(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    document = v3_managed_document(tmp_path)
    cache = tmp_path / "models" / "target"
    document["models"]["target"].update(
        path=str(cache), model_id="example/model", download={"provider": "hf_mirror"}
    )
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(document))
    args = ["target", "plan", "--recipe", str(recipe)]
    if json_output:
        args.append("--json")
    assert main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        preparation = json.loads(output)["model_preparation"][0]
        assert preparation["model_id"] == "example/model"
        assert preparation["provider"] == "hf_mirror"
    else:
        assert "example/model" in output and "download if missing" in output
    assert not cache.parent.exists()
    assert not (tmp_path / "artifacts").exists()


def test_target_plan_reports_unresolved_inputs_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template, _ = write_input_template(tmp_path)

    exit_code = main(["target", "plan", "--recipe", str(template), "--json"])

    assert exit_code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["resolved"] is False
    assert payload["missing_inputs"] == ["container_image", "sglang_revision"]
    assert not (tmp_path / "artifacts").exists()


def test_target_resolve_writes_lock_that_runs_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template, values = write_input_template(tmp_path)
    lock = tmp_path / "private-experiments" / "baseline" / "recipe.lock.yaml"
    expected = load_optimization_config(template, values)

    exit_code = main(
        [
            "target",
            "resolve",
            "--recipe",
            str(template),
            "--values",
            str(values),
            "--output",
            str(lock),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved"] is True
    assert payload["bound_inputs"] == ["container_image", "sglang_revision"]
    assert lock.is_file()
    assert lock.parent.stat().st_mode & 0o777 == 0o700
    assert lock.stat().st_mode & 0o777 == 0o600
    locked_config = load_optimization_config(lock)
    assert locked_config.baseline.source_revision == "b" * 40
    assert locked_config.input_bindings == {}
    assert (
        locked_config.optimization.planner.patch_catalog
        == expected.optimization.planner.patch_catalog
    )


def test_target_run_rejects_unresolved_template_before_artifact_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template, _ = write_input_template(tmp_path)

    exit_code = main(
        [
            "target",
            "run",
            "--recipe",
            str(template),
        ]
    )

    assert exit_code == 2
    assert "missing required input binding" in capsys.readouterr().err
    assert not (tmp_path / "artifacts").exists()


def test_target_cli_reports_profile_failure_as_expected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(args: object) -> int:
        raise ProfileCaptureError("SGLang profile control /start_profile failed: HTTP 500")

    monkeypatch.setattr(cli, "_target_run", fail)
    assert main(["target", "run", "--recipe", str(tmp_path / "unused.yaml"), "--json"]) == 2
    output = capsys.readouterr()
    assert "euboulia: error: SGLang profile control" in output.err
    assert "Traceback" not in output.err


def test_optimize_plan_allows_template_but_run_requires_bindings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template, _ = write_input_template(tmp_path)

    plan_exit = main(["optimize", "plan", "--recipe", str(template), "--json"])
    plan_payload = json.loads(capsys.readouterr().out)
    run_exit = main(["optimize", "run", "--recipe", str(template)])

    assert plan_exit == 0
    assert plan_payload["resolved"] is False
    assert run_exit == 2
    assert "missing required input binding" in capsys.readouterr().err
    assert not (tmp_path / "artifacts").exists()


def test_target_run_parser_accepts_optional_name() -> None:
    args = build_parser().parse_args(
        [
            "target",
            "run",
            "--recipe",
            "recipe.yaml",
            "--values",
            "host-values.yaml",
            "--name",
            "baseline",
            "--executor",
            "h20-pod",
            "--node",
            "10.0.0.8",
            "--runtime-config",
            "runtime.yaml",
        ]
    )

    assert args.values == Path("host-values.yaml")
    assert args.name == "baseline"
    assert args.executor == "h20-pod"
    assert args.node == "10.0.0.8"
    assert args.runtime_config == Path("runtime.yaml")


def test_serve_and_control_plane_commands_have_focused_interfaces() -> None:
    serve_args = build_parser().parse_args(
        ["serve", "--runtime-config", "runtime.yaml", "--port", "9000", "--max-parallel", "3"]
    )
    submit_args = build_parser().parse_args(
        [
            "target",
            "submit",
            "--recipe",
            "recipe.lock.yaml",
            "--executor",
            "gpu",
            "--node",
            "worker-8",
            "--name",
            "baseline",
        ]
    )
    list_args = build_parser().parse_args(["target", "list", "--limit", "12"])
    show_args = build_parser().parse_args(["target", "show", "run-01HF7YAT000000000000000000"])

    assert serve_args.runtime_config == Path("runtime.yaml")
    assert serve_args.port == 9000
    assert serve_args.max_parallel == 3
    assert submit_args.recipe == Path("recipe.lock.yaml")
    assert submit_args.executor == "gpu"
    assert submit_args.node == "worker-8"
    assert submit_args.name == "baseline"
    assert list_args.limit == 12
    assert show_args.run_uid == "run-01HF7YAT000000000000000000"


def test_target_artifact_pull_parser_requires_explicit_snapshot_destination() -> None:
    args = build_parser().parse_args(
        [
            "target",
            "artifacts",
            "pull",
            "--run-uid",
            "run-01HF7YAT000000000000000000",
            "--executor",
            "h20-pod",
            "--destination",
            "raw-snapshot",
        ]
    )

    assert args.run_uid == "run-01HF7YAT000000000000000000"
    assert args.destination == Path("raw-snapshot")


def test_target_cleanup_parser_identifies_one_owned_run() -> None:
    args = build_parser().parse_args(
        [
            "target",
            "cleanup",
            "--run-uid",
            "run-01HF7YAT000000000000000000",
            "--executor",
            "h20-pod",
        ]
    )

    assert args.run_uid == "run-01HF7YAT000000000000000000"
    assert args.executor == "h20-pod"


def test_target_cleanup_updates_control_plane_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_uid = "run-01HF7YAT000000000000000000"
    template = tmp_path / "pod-template.yaml"
    template.write_text(
        "apiVersion: v1\nkind: Pod\nmetadata: {}\nspec:\n"
        "  containers:\n    - name: runtime\n      image: placeholder.invalid/image\n",
        encoding="utf-8",
    )
    state_root = tmp_path / "local-state"
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "storage": {"root": str(state_root)},
                "executors": {
                    "gpu": {
                        "type": "kubernetes",
                        "namespace": "private-inference",
                        "pod_template": str(template),
                        "container": "runtime",
                        "project_dir": "/workspace/euboulia",
                        "scratch_dir": "/scratch/euboulia",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    store = ControlStore(state_root)
    store.create(
        run_uid=run_uid,
        name="baseline",
        recipe_name="test-recipe",
        recipe_path=tmp_path / "recipe.lock.yaml",
        recipe_sha256="a" * 64,
        executor="gpu",
        node="worker-8",
        runtime_config=runtime,
        metadata={},
    )
    store.update(
        run_uid,
        status="failed",
        phase="failed",
        infrastructure_state="pod_retained",
        artifact_state="partial",
        finished_at="2026-09-04T07:00:00+00:00",
    )
    pod = OwnedPod(
        name="euboulia-run-01hf7yat000000000000000000",
        uid="pod-uid",
        run_uid=run_uid,
        node_name="worker-8",
    )
    monkeypatch.setattr(
        cli.KubernetesTargetSupervisor,
        "cleanup",
        lambda self, selected_uid: pod,
    )

    exit_code = main(
        [
            "target",
            "cleanup",
            "--run-uid",
            run_uid,
            "--executor",
            "gpu",
            "--runtime-config",
            str(runtime),
        ]
    )

    assert exit_code == 0
    updated = ControlStore(state_root).get(run_uid)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.infrastructure_state == "pod_deleted"
    assert updated.detail == "owned Pod deleted after terminal run"


def test_config_remains_a_compatible_alias_for_recipe() -> None:
    args = build_parser().parse_args(["plan", "--config", "legacy.yaml"])

    assert args.recipe == Path("legacy.yaml")


def test_optimize_events_cli_filters_run(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
    ledger.append(OptimizationEvent.create(EventType.RUN_STARTED, "wanted"))
    ledger.append(OptimizationEvent.create(EventType.RUN_STARTED, "other"))

    exit_code = main(["optimize", "events", "--events", str(path), "--run-uid", "wanted"])

    assert exit_code == 0
