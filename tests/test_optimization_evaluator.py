from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

from euboulia.optimization.evaluator import (
    AccuracySpec,
    BenchmarkSpec,
    CommandSpec,
    EvaluationAuthorizationError,
    EvaluationOutcome,
    EvaluationPlan,
    EvaluationStage,
    MetricsError,
    ObjectiveDirection,
    ObjectiveSpec,
    StabilitySpec,
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


def test_adaptive_benchmark_stops_after_recent_windows_stabilize(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = (
        "import json,os,pathlib; "
        "counter=pathlib.Path('counter'); "
        "index=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(index+1)); "
        "values=[999,100,101,150]; "
        "pathlib.Path('metrics.json').write_text(json.dumps({'throughput':values[index]})); "
        "pathlib.Path('phases').open('a').write(os.environ['EUBOULIA_MEASUREMENT_PHASE']+'\\n')"
    )
    benchmark = CommandSpec("window", (sys.executable, "-c", source))
    plan = EvaluationPlan(
        trial_id="stable",
        workspace=workspace,
        preflight=(),
        correctness=(),
        benchmark=BenchmarkSpec(
            benchmark,
            Path("metrics.json"),
            required_metrics=("throughput",),
            stability=StabilitySpec(
                warmup_runs=1,
                min_windows=2,
                max_windows=4,
                stable_windows=2,
                relative_tolerance=0.02,
                max_seconds=30,
            ),
        ),
        objective=ObjectiveSpec("throughput", ObjectiveDirection.MAXIMIZE),
    )

    evaluator = TieredEvaluator(tmp_path / "artifacts")
    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.PASSED
    assert result.measurement_values == (100.0, 101.0)
    assert result.objective_value == 100.5
    assert result.measurement_stable is True
    assert len(result.stages[-1].executions) == 3
    assert (workspace / "phases").read_text().splitlines() == [
        "warmup",
        "measurement",
        "measurement",
    ]
    assert (result.artifact_dir / "benchmark-windows.json").is_file()


def test_external_accuracy_contract_rejects_score_below_threshold(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    accuracy = _write_command(
        "accuracy.json",
        {"results": {"gsm8k": {"exact_match,flexible-extract": 0.79}}},
        name="external-lm-eval",
    )
    plan = _plan(workspace)
    plan = EvaluationPlan(
        trial_id=plan.trial_id,
        workspace=plan.workspace,
        preflight=plan.preflight,
        correctness=plan.correctness,
        benchmark=plan.benchmark,
        objective=plan.objective,
        accuracy_check=AccuracySpec(
            command=accuracy,
            result_path=Path("accuracy.json"),
            metric="results.gsm8k.exact_match,flexible-extract",
            direction=ObjectiveDirection.MAXIMIZE,
            threshold=0.8,
        ),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.REJECTED
    assert result.failure_stage is None
    assert result.accuracy_value == 0.79
    assert result.accuracy_metrics == {
        "results.gsm8k.exact_match,flexible-extract": 0.79
    }
    assert result.reason == "accuracy gate failed: 0.79 is not >= 0.8"


def test_adaptive_benchmark_fails_closed_when_windows_do_not_stabilize(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = (
        "import json,pathlib; "
        "counter=pathlib.Path('counter'); "
        "index=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(index+1)); "
        "values=[100,130,100]; "
        "pathlib.Path('metrics.json').write_text(json.dumps({'throughput':values[index]}))"
    )
    plan = EvaluationPlan(
        trial_id="unstable",
        workspace=workspace,
        preflight=(),
        correctness=(),
        benchmark=BenchmarkSpec(
            CommandSpec("window", (sys.executable, "-c", source)),
            Path("metrics.json"),
            stability=StabilitySpec(
                warmup_runs=0,
                min_windows=2,
                max_windows=3,
                stable_windows=2,
                relative_tolerance=0.02,
                max_seconds=30,
            ),
        ),
        objective=ObjectiveSpec("throughput", ObjectiveDirection.MAXIMIZE),
    )
    evaluator = TieredEvaluator(tmp_path / "artifacts")

    result = evaluator.execute(evaluator.authorize(plan, approved=True))

    assert result.outcome is EvaluationOutcome.FAILED
    assert result.failure_stage is EvaluationStage.BENCHMARK
    assert result.measurement_values == (100.0, 130.0, 100.0)
    assert result.measurement_stable is False
    assert "did not stabilize" in (result.reason or "")


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
