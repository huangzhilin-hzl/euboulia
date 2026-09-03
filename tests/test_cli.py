import json
from pathlib import Path

from test_config import VALID_CONFIG, write_config

from euboulia.cli import build_parser, main
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
    config = repository / "examples/optimization-vllm.yaml"

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
            "--run-evaluations",
            "--run-builds",
            "--manage-services",
        ]
    )

    assert args.apply_patches is True
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
