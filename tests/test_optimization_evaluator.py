from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

from euboulia.optimization.evaluator import (
    BenchmarkSpec,
    CommandSpec,
    EvaluationAuthorizationError,
    EvaluationOutcome,
    EvaluationPlan,
    EvaluationStage,
    MetricsError,
    ObjectiveDirection,
    ObjectiveSpec,
    TieredEvaluator,
    parse_metrics,
)

_SUCCESS = CommandSpec("success", (sys.executable, "-c", "print('ok')"))


def _write_command(path: str, payload: object, *, name: str = "benchmark") -> CommandSpec:
    source = (
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
    )
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CommandSpec(name, (sys.executable, "-c", source, path, text))


def _touch_command(path: Path, *, name: str) -> CommandSpec:
    source = "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()"
    return CommandSpec(name, (sys.executable, "-c", source, str(path)))


def _plan(
    workspace: Path,
    *,
    preflight: tuple[CommandSpec, ...] = (_SUCCESS,),
    correctness: tuple[CommandSpec, ...] = (_SUCCESS,),
    benchmark: CommandSpec | None = None,
    objective: ObjectiveSpec | None = None,
    accuracy: tuple[CommandSpec, ...] = (),
    profiler_trial: bool = False,
    trial_id: str = "trial-1",
) -> EvaluationPlan:
    benchmark_command = benchmark or _write_command(
        "metrics.json", {"metrics": {"throughput": 110.0}}
    )
    objective_spec = objective or ObjectiveSpec(
        "throughput",
        ObjectiveDirection.MAXIMIZE,
        baseline=100.0,
        minimum_relative_improvement=0.05,
    )
    return EvaluationPlan(
        trial_id=trial_id,
        workspace=workspace,
        preflight=preflight,
        correctness=correctness,
        benchmark=BenchmarkSpec(benchmark_command, Path("metrics.json")),
        objective=objective_spec,
        accuracy=accuracy,
        profiler_trial=profiler_trial,
    )


def test_evaluator_runs_tiers_in_order_and_promotes_passing_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(_plan(workspace), approved=True))

    assert result.outcome is EvaluationOutcome.PASSED
    assert result.promotable is True
    assert result.gate_passed is True
    assert result.objective_value == 110.0
    assert result.relative_improvement == pytest.approx(0.1)
    assert [stage.stage for stage in result.stages] == [
        EvaluationStage.PREFLIGHT,
        EvaluationStage.CORRECTNESS,
        EvaluationStage.BENCHMARK,
    ]
    assert (result.artifact_dir / "evaluation.json").is_file()
    assert all(
        execution.stdout_path.is_file() for stage in result.stages for execution in stage.executions
    )


def test_evaluator_runs_accuracy_after_benchmark(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(
        evaluator.authorize(_plan(workspace, accuracy=(_SUCCESS,)), approved=True)
    )

    assert result.outcome is EvaluationOutcome.PASSED
    assert [stage.stage for stage in result.stages] == [
        EvaluationStage.PREFLIGHT,
        EvaluationStage.CORRECTNESS,
        EvaluationStage.BENCHMARK,
        EvaluationStage.ACCURACY,
    ]


def test_authorization_is_explicit_single_use_and_binds_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evaluator = TieredEvaluator(tmp_path / "artifacts")
    plan = _plan(workspace)

    with pytest.raises(EvaluationAuthorizationError, match="approved=True"):
        evaluator.authorize(plan, approved=False)
    assert not (tmp_path / "artifacts").exists()

    authorization = evaluator.authorize(plan, approved=True)
    mutable_environment = cast(dict[str, str | None], plan.preflight[0].env_overrides)
    mutable_environment["EUBOULIA_MUTATION"] = "after-approval"
    with pytest.raises(EvaluationAuthorizationError, match="changed after authorization"):
        evaluator.execute(authorization)
    with pytest.raises(EvaluationAuthorizationError, match="already consumed"):
        evaluator.execute(authorization)


def test_command_spec_rejects_shell_command_strings() -> None:
    command_string = cast(tuple[str, ...], "python -c 'print(1)'")
    with pytest.raises(TypeError, match="command string"):
        CommandSpec("unsafe", command_string)


def test_preflight_failure_stops_all_later_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    second_preflight = tmp_path / "second-preflight"
    correctness = tmp_path / "correctness"
    benchmark = tmp_path / "benchmark"
    failing = CommandSpec("fail", (sys.executable, "-c", "raise SystemExit(7)"))
    plan = _plan(
        workspace,
        preflight=(failing, _touch_command(second_preflight, name="must-not-run")),
        correctness=(_touch_command(correctness, name="correctness"),),
        benchmark=_touch_command(benchmark, name="benchmark"),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.FAILED
    assert result.failure_stage is EvaluationStage.PREFLIGHT
    assert len(result.stages) == 1
    assert len(result.stages[0].executions) == 1
    assert not second_preflight.exists()
    assert not correctness.exists()
    assert not benchmark.exists()


def test_correctness_failure_stops_benchmark(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    benchmark = tmp_path / "benchmark"
    failing = CommandSpec("wrong", (sys.executable, "-c", "raise SystemExit(1)"))
    plan = _plan(
        workspace,
        correctness=(failing,),
        benchmark=_touch_command(benchmark, name="benchmark"),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.failure_stage is EvaluationStage.CORRECTNESS
    assert [stage.stage for stage in result.stages] == [
        EvaluationStage.PREFLIGHT,
        EvaluationStage.CORRECTNESS,
    ]
    assert not benchmark.exists()


@pytest.mark.parametrize("payload", ["not json", {}, {"throughput": float("nan")}])
def test_invalid_metrics_fail_benchmark_without_promotion(tmp_path: Path, payload: object) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = _plan(workspace, benchmark=_write_command("metrics.json", payload))
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.FAILED
    assert result.failure_stage is EvaluationStage.BENCHMARK
    assert result.promotable is False
    assert result.stages[-1].succeeded is False


def test_minimize_objective_and_absolute_gate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    objective = ObjectiveSpec(
        "latency_ms",
        ObjectiveDirection.MINIMIZE,
        baseline=100.0,
        minimum_relative_improvement=0.05,
        absolute_threshold=95.0,
    )
    plan = _plan(
        workspace,
        benchmark=_write_command("metrics.json", {"latency_ms": 90}),
        objective=objective,
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.PASSED
    assert result.relative_improvement == pytest.approx(0.1)


def test_gate_miss_rejects_but_is_not_execution_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = _plan(
        workspace,
        benchmark=_write_command("metrics.json", {"throughput": 104.0}),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.REJECTED
    assert result.failure_stage is None
    assert result.stages[-1].succeeded is True
    assert result.gate_passed is False
    assert result.promotable is False


def test_profiler_trial_can_never_be_promoted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evaluator = TieredEvaluator(tmp_path / "artifacts")
    result = evaluator.execute(
        evaluator.authorize(_plan(workspace, profiler_trial=True), approved=True)
    )

    assert result.outcome is EvaluationOutcome.PROFILE_ONLY
    assert result.gate_passed is True
    assert result.promotable is False
    assert "cannot be promoted" in (result.reason or "")


def test_metrics_path_must_stay_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = EvaluationPlan(
        trial_id="escape",
        workspace=workspace,
        preflight=(),
        correctness=(),
        benchmark=BenchmarkSpec(_SUCCESS, Path("../metrics.json")),
        objective=ObjectiveSpec("value", ObjectiveDirection.MAXIMIZE),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    with pytest.raises(MetricsError, match="inside"):
        evaluator.authorize(plan, approved=True)


def test_parse_metrics_supports_nested_json_and_last_jsonl_record(tmp_path: Path) -> None:
    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps({"metrics": {"throughput": 12}, "timing": {"seconds": 3.5}}),
        encoding="utf-8",
    )
    stream = tmp_path / "stream.jsonl"
    stream.write_text('{"throughput": 1}\n{"throughput": 2}\n', encoding="utf-8")

    assert parse_metrics(nested) == {"throughput": 12.0, "timing.seconds": 3.5}
    assert parse_metrics(stream) == {"throughput": 2.0}
