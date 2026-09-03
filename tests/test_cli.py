import json
from pathlib import Path

import pytest
from test_config import VALID_CONFIG, write_config
from test_optimization_config import write_input_template

from euboulia.cli import build_parser, main
from euboulia.optimization.config import load_optimization_config
from euboulia.optimization.events import EventLedger, EventType, OptimizationEvent


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
            "--apply-patches",
            "--run-profiles",
            "--run-evaluations",
            "--run-builds",
            "--manage-services",
        ]
    )

    assert args.apply_patches is True
    assert args.run_profiles is True
    assert args.run_evaluations is True
    assert args.run_builds is True
    assert args.manage_services is True


def test_target_plan_cli_inspects_dsv4_scenario_read_only() -> None:
    repository = Path(__file__).resolve().parents[1]
    config = repository / "examples/scenarios/dsv4-megamoe.yaml"

    exit_code = main(["target", "plan", "--recipe", str(config), "--json"])

    assert exit_code == 0


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
    lock = tmp_path / "template.lock.yaml"

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
    locked_config = load_optimization_config(lock)
    assert locked_config.baseline.source_revision == "b" * 40
    assert locked_config.input_bindings == {}


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
            "--prepare-workspace",
            "--run-profiles",
            "--run-evaluations",
            "--run-builds",
            "--manage-services",
        ]
    )

    assert exit_code == 2
    assert "missing required input binding" in capsys.readouterr().err
    assert not (tmp_path / "artifacts").exists()


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


def test_target_run_parser_exposes_baseline_capabilities() -> None:
    args = build_parser().parse_args(
        [
            "target",
            "run",
            "--recipe",
            "recipe.yaml",
            "--values",
            "host-values.yaml",
            "--prepare-workspace",
            "--run-profiles",
            "--run-evaluations",
            "--run-builds",
            "--manage-services",
        ]
    )

    assert args.prepare_workspace is True
    assert args.run_profiles is True
    assert args.values == Path("host-values.yaml")
    assert args.run_evaluations is True
    assert args.run_builds is True
    assert args.manage_services is True


def test_config_remains_a_compatible_alias_for_recipe() -> None:
    args = build_parser().parse_args(["plan", "--config", "legacy.yaml"])

    assert args.recipe == Path("legacy.yaml")


def test_optimize_events_cli_filters_run(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
    ledger.append(OptimizationEvent.create(EventType.RUN_STARTED, "wanted"))
    ledger.append(OptimizationEvent.create(EventType.RUN_STARTED, "other"))

    exit_code = main(["optimize", "events", "--events", str(path), "--run-id", "wanted"])

    assert exit_code == 0
