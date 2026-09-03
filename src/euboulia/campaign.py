"""Static planning and evidence collection for optimization campaigns.

The MVP deliberately separates deliberation from mutation: it can plan and run
benchmark clients, but it cannot apply patches or manage inference services.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from euboulia.adapters import (
    AdapterCommand,
    AdapterError,
    BaseAdapter,
    ParsedBenchmark,
    SGLangAdapter,
    VLLMAdapter,
)
from euboulia.config import CampaignConfig, CandidateConfig
from euboulia.execution import CommandExecutor, ExecutionResult
from euboulia.gates import CompositeGate, CorrectnessGate, PerformanceGate
from euboulia.ledger import ExperimentLedger
from euboulia.models import (
    Candidate,
    Experiment,
    ExperimentStatus,
    Framework,
    JSONValue,
    MetricDirection,
    Metrics,
    Verdict,
    Workload,
    utc_now,
)


class CampaignSafetyError(RuntimeError):
    """Raised when a plan crosses the MVP execution boundary."""


@dataclass(frozen=True, slots=True)
class PlannedExperiment:
    """A side-effect-free benchmark plan for one candidate."""

    run_id: str
    ordinal: int
    workload: Workload
    candidate: Candidate
    command: AdapterCommand
    artifact_dir: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "ordinal": self.ordinal,
            "workload": self.workload.to_dict(),
            "candidate": self.candidate.to_dict(),
            "command": self.command.to_dict(),
            "artifact_dir": str(self.artifact_dir),
        }


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    run_id: str
    executed: bool
    plans: tuple[PlannedExperiment, ...]
    experiments: tuple[Experiment, ...] = ()
    stopped_reason: str | None = None

    @property
    def accepted(self) -> int:
        return sum(
            experiment.verdict is not None and experiment.verdict.accepted
            for experiment in self.experiments[1:]
        )

    @property
    def rejected(self) -> int:
        return sum(
            experiment.verdict is not None and not experiment.verdict.accepted
            for experiment in self.experiments[1:]
        )

    @property
    def failed(self) -> int:
        return sum(
            experiment.status in {ExperimentStatus.FAILED, ExperimentStatus.CANCELLED}
            for experiment in self.experiments
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "executed": self.executed,
            "plans": [plan.to_dict() for plan in self.plans],
            "experiments": [experiment.to_dict() for experiment in self.experiments],
            "accepted": self.accepted,
            "rejected": self.rejected,
            "failed": self.failed,
            "stopped_reason": self.stopped_reason,
        }


@dataclass(frozen=True, slots=True)
class ExistingResultEvaluation:
    baseline: ParsedBenchmark
    candidate: ParsedBenchmark
    verdict: Verdict

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "verdict": self.verdict.to_dict(),
        }


def adapter_for(framework: str) -> BaseAdapter:
    """Return an adapter without importing the target framework."""

    if framework == "sglang":
        return SGLangAdapter()
    if framework == "vllm":
        return VLLMAdapter()
    raise ValueError(f"unsupported framework: {framework}")


def plan_campaign(
    config: CampaignConfig,
    *,
    run_id: str = "preview",
    adapter: BaseAdapter | None = None,
) -> tuple[PlannedExperiment, ...]:
    """Render every candidate command without creating directories or processes."""

    selected_adapter = adapter or adapter_for(config.framework)
    _validate_base_args(config.benchmark.base_args)
    safe_run_id = _safe_component(run_id)
    run_dir = config.execution.artifacts_dir / safe_run_id
    plans: list[PlannedExperiment] = []
    for ordinal, candidate_config in enumerate(config.candidates):
        candidate_dir = run_dir / _safe_component(candidate_config.candidate_id)
        result_path = candidate_dir / config.benchmark.result_filename
        workload_mapping, benchmark_parameters = _candidate_inputs(config, candidate_config)
        command = selected_adapter.build_command(
            config.benchmark.mode,
            workload_mapping,
            benchmark_parameters,
            config.benchmark.base_args,
            result_path,
        )
        plans.append(
            PlannedExperiment(
                run_id=safe_run_id,
                ordinal=ordinal,
                workload=_workload_model(config, workload_mapping),
                candidate=_candidate_model(config, candidate_config),
                command=command,
                artifact_dir=candidate_dir,
            )
        )
    return tuple(plans)


def run_campaign(
    config: CampaignConfig,
    *,
    execute: bool = False,
    adapter: BaseAdapter | None = None,
    run_id: str | None = None,
) -> CampaignRunResult:
    """Plan a campaign, and optionally execute only its benchmark clients."""

    effective_run_id = run_id or _new_run_id()
    selected_adapter = adapter or adapter_for(config.framework)
    plans = plan_campaign(config, run_id=effective_run_id, adapter=selected_adapter)
    if not execute:
        return CampaignRunResult(run_id=effective_run_id, executed=False, plans=plans)

    lifecycle_commands = [plan for plan in plans if plan.command.manages_service_lifecycle]
    if lifecycle_commands:
        raise CampaignSafetyError(
            "this plan manages a service lifecycle; the MVP can render it but will not execute it"
        )

    run_dir = config.execution.artifacts_dir / _safe_component(effective_run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "plan.json", [plan.to_dict() for plan in plans])
    _write_json(run_dir / "config.snapshot.json", _redacted_config(config))

    ledger = ExperimentLedger(config.execution.ledger, fsync=True)
    correctness_gate, composite_gate = _gates(config)
    completed: list[Experiment] = []
    baseline_metrics: Metrics | None = None
    stopped_reason: str | None = None

    for index, plan in enumerate(plans):
        experiment_id = f"{effective_run_id}:{plan.candidate.candidate_id}"
        running = Experiment(
            experiment_id=experiment_id,
            workload=plan.workload,
            candidate=plan.candidate,
            status=ExperimentStatus.RUNNING,
            artifacts=(str(plan.artifact_dir),),
            metadata={
                "run_id": effective_run_id,
                "ordinal": index,
                "command": _as_json_value(plan.command.to_dict()),
            },
        )
        ledger.append(running)

        plan.artifact_dir.mkdir(parents=True, exist_ok=True)
        executor = CommandExecutor(
            plan.artifact_dir,
            default_timeout_seconds=config.execution.timeout_seconds,
            env_overrides=config.execution.env,
        )
        candidate_config = config.candidates[index]
        execution = executor.run(
            plan.command.as_argv(),
            env_overrides=candidate_config.env,
            artifact_prefix=plan.candidate.candidate_id,
        )
        artifacts = (
            str(plan.command.result_path),
            str(execution.stdout_path),
            str(execution.stderr_path),
        )
        if not execution.succeeded:
            failed = _failed_experiment(running, execution, artifacts)
            ledger.append(failed)
            completed.append(failed)
            if index == 0:
                stopped_reason = "baseline benchmark failed"
                completed.extend(
                    _append_cancelled(plans[index + 1 :], ledger, effective_run_id, stopped_reason)
                )
                break
            continue

        try:
            parsed = selected_adapter.parse_result(
                plan.command.result_path, plan.command.benchmark_type
            )
            metrics = Metrics(
                values=parsed.metrics,
                metadata={
                    "framework": parsed.framework,
                    "benchmark_type": parsed.benchmark_type.value,
                    "source_path": str(parsed.source_path),
                },
            )
            _write_json(plan.artifact_dir / "metrics.normalized.json", metrics.to_dict())
        except (AdapterError, OSError, TypeError, ValueError) as exc:
            failed = Experiment(
                experiment_id=running.experiment_id,
                workload=running.workload,
                candidate=running.candidate,
                status=ExperimentStatus.FAILED,
                created_at=running.created_at,
                finished_at=utc_now(),
                artifacts=artifacts,
                error=f"result parsing failed: {exc}",
                metadata={
                    **running.metadata,
                    "execution": _as_json_value(execution.to_dict()),
                },
            )
            ledger.append(failed)
            completed.append(failed)
            if index == 0:
                stopped_reason = "baseline result was invalid"
                completed.extend(
                    _append_cancelled(plans[index + 1 :], ledger, effective_run_id, stopped_reason)
                )
                break
            continue

        if index == 0:
            verdict = correctness_gate.evaluate(metrics)
            baseline_metrics = metrics if verdict.accepted else None
        else:
            if baseline_metrics is None:  # defensive; baseline failures stop above
                raise RuntimeError("candidate evaluation reached without a valid baseline")
            verdict = composite_gate.evaluate(baseline_metrics, metrics)

        finished = Experiment(
            experiment_id=running.experiment_id,
            workload=running.workload,
            candidate=running.candidate,
            status=ExperimentStatus.SUCCEEDED,
            metrics=metrics,
            baseline_metrics=baseline_metrics if index > 0 else None,
            verdict=verdict,
            created_at=running.created_at,
            finished_at=utc_now(),
            artifacts=artifacts,
            metadata={
                **running.metadata,
                "execution": _as_json_value(execution.to_dict()),
            },
        )
        ledger.append(finished)
        completed.append(finished)
        _write_json(plan.artifact_dir / "experiment.json", finished.to_dict())

        if index == 0 and not verdict.accepted:
            stopped_reason = "baseline correctness gate failed"
            completed.extend(
                _append_cancelled(plans[index + 1 :], ledger, effective_run_id, stopped_reason)
            )
            break

    return CampaignRunResult(
        run_id=effective_run_id,
        executed=True,
        plans=plans,
        experiments=tuple(completed),
        stopped_reason=stopped_reason,
    )


def evaluate_existing_results(
    config: CampaignConfig,
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    adapter: BaseAdapter | None = None,
) -> ExistingResultEvaluation:
    """Apply the campaign gates to two already-produced benchmark results."""

    selected_adapter = adapter or adapter_for(config.framework)
    baseline = selected_adapter.parse_result(baseline_path, config.benchmark.mode)
    candidate = selected_adapter.parse_result(candidate_path, config.benchmark.mode)
    baseline_metrics = Metrics(values=baseline.metrics)
    candidate_metrics = Metrics(values=candidate.metrics)
    correctness_gate, composite_gate = _gates(config)
    baseline_correctness = correctness_gate.evaluate(baseline_metrics)
    if not baseline_correctness.accepted:
        return ExistingResultEvaluation(
            baseline=baseline,
            candidate=candidate,
            verdict=Verdict(
                status=baseline_correctness.status,
                reasons=("baseline correctness gate failed", *baseline_correctness.reasons),
                correctness_passed=False,
                performance_passed=None,
                metric=config.gates.performance.metric,
                direction=composite_gate.performance.direction,
                details={"baseline_correctness": baseline_correctness.to_dict()},
            ),
        )
    return ExistingResultEvaluation(
        baseline=baseline,
        candidate=candidate,
        verdict=composite_gate.evaluate(baseline_metrics, candidate_metrics),
    )


def _candidate_inputs(
    config: CampaignConfig, candidate: CandidateConfig
) -> tuple[dict[str, object], dict[str, object]]:
    workload: dict[str, object] = {
        "name": config.workload.name,
        "model": config.workload.model,
        "input_tokens": config.workload.input_tokens,
        "output_tokens": config.workload.output_tokens,
        "concurrency": config.workload.concurrency,
        "num_prompts": config.workload.num_prompts,
        "endpoint": config.workload.endpoint,
        "dataset": config.workload.dataset,
    }
    parameters = dict(candidate.parameters)
    aliases = {
        "model": "model",
        "input_tokens": "input_tokens",
        "random_input_len": "input_tokens",
        "input_len": "input_tokens",
        "output_tokens": "output_tokens",
        "random_output_len": "output_tokens",
        "output_len": "output_tokens",
        "concurrency": "concurrency",
        "max_concurrency": "concurrency",
        "batch_size": "concurrency",
        "num_prompts": "num_prompts",
        "num_requests": "num_prompts",
        "endpoint": "endpoint",
        "base_url": "endpoint",
        "dataset": "dataset",
        "dataset_name": "dataset",
    }
    for parameter_name, workload_name in aliases.items():
        if parameter_name in parameters:
            workload[workload_name] = parameters.pop(parameter_name)
    return workload, parameters


def _workload_model(config: CampaignConfig, values: dict[str, object]) -> Workload:
    return Workload(
        name=str(values["name"]),
        model=str(values["model"]),
        input_tokens=_positive_int(values["input_tokens"], "input_tokens"),
        output_tokens=_positive_int(values["output_tokens"], "output_tokens"),
        concurrency=_positive_int(values["concurrency"], "concurrency"),
        num_requests=_positive_int(values["num_prompts"], "num_prompts"),
        dataset=str(values["dataset"]),
        parameters={
            "endpoint": str(values["endpoint"]),
            "benchmark_mode": config.benchmark.mode,
        },
        metadata={"recipe": config.name},
    )


def _candidate_model(config: CampaignConfig, candidate: CandidateConfig) -> Candidate:
    framework = Framework.SGLANG if config.framework == "sglang" else Framework.VLLM
    return Candidate(
        candidate_id=candidate.candidate_id,
        framework=framework,
        name=candidate.name,
        parameters=candidate.parameters,
        environment={name: "<redacted>" for name in candidate.env},
        patch=candidate.patch,
        metadata=_as_json_mapping({"environment_keys": sorted(candidate.env)}),
    )


def _gates(config: CampaignConfig) -> tuple[CorrectnessGate, CompositeGate]:
    direction = (
        MetricDirection.MAXIMIZE
        if config.gates.performance.direction == "maximize"
        else MetricDirection.MINIMIZE
    )
    correctness = CorrectnessGate(
        metric=config.gates.correctness.metric,
        direction=MetricDirection.MAXIMIZE,
        threshold=config.gates.correctness.minimum,
    )
    performance = PerformanceGate(
        metric=config.gates.performance.metric,
        direction=direction,
        min_relative_improvement=config.gates.performance.min_relative_improvement,
        allowed_relative_regression=config.gates.performance.max_regression,
        noise_tolerance=config.gates.performance.noise_tolerance,
    )
    return correctness, CompositeGate(performance=performance, correctness=correctness)


def _failed_experiment(
    running: Experiment, execution: ExecutionResult, artifacts: tuple[str, ...]
) -> Experiment:
    error = execution.error or f"benchmark exited with status {execution.returncode}"
    return Experiment(
        experiment_id=running.experiment_id,
        workload=running.workload,
        candidate=running.candidate,
        status=ExperimentStatus.FAILED,
        created_at=running.created_at,
        finished_at=utc_now(),
        artifacts=artifacts,
        error=error,
        metadata={
            **running.metadata,
            "execution": _as_json_value(execution.to_dict()),
        },
    )


def _append_cancelled(
    plans: tuple[PlannedExperiment, ...],
    ledger: ExperimentLedger,
    run_id: str,
    reason: str,
) -> tuple[Experiment, ...]:
    cancelled: list[Experiment] = []
    for plan in plans:
        experiment = Experiment(
            experiment_id=f"{run_id}:{plan.candidate.candidate_id}",
            workload=plan.workload,
            candidate=plan.candidate,
            status=ExperimentStatus.CANCELLED,
            finished_at=utc_now(),
            artifacts=(str(plan.artifact_dir),),
            error=reason,
            metadata={"run_id": run_id, "not_executed": True},
        )
        ledger.append(experiment)
        cancelled.append(experiment)
    return tuple(cancelled)


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_component(value: str) -> str:
    normalized = _UNSAFE_COMPONENT.sub("-", value.strip()).strip(".-")[:72]
    if not normalized:
        normalized = "item"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{normalized}-{digest}" if normalized != value else normalized


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"candidate workload override {name!r} must be a positive integer")
    return value


def _redacted_config(config: CampaignConfig) -> dict[str, object]:
    return {
        "schema_version": config.schema_version,
        "name": config.name,
        "framework": config.framework,
        "workload": {
            "name": config.workload.name,
            "model": config.workload.model,
            "input_tokens": config.workload.input_tokens,
            "output_tokens": config.workload.output_tokens,
            "concurrency": config.workload.concurrency,
            "num_prompts": config.workload.num_prompts,
            "endpoint": config.workload.endpoint,
            "dataset": config.workload.dataset,
        },
        "benchmark": {
            "mode": config.benchmark.mode,
            "base_args": list(config.benchmark.base_args),
            "result_filename": config.benchmark.result_filename,
        },
        "candidates": [
            {
                "id": item.candidate_id,
                "name": item.name,
                "parameters": item.parameters,
                "env": {name: "<redacted>" for name in item.env},
                "patch": item.patch,
            }
            for item in config.candidates
        ],
        "gates": {
            "correctness": {
                "metric": config.gates.correctness.metric,
                "minimum": config.gates.correctness.minimum,
            },
            "performance": {
                "metric": config.gates.performance.metric,
                "direction": config.gates.performance.direction,
                "min_relative_improvement": (config.gates.performance.min_relative_improvement),
                "max_regression": config.gates.performance.max_regression,
                "noise_tolerance": config.gates.performance.noise_tolerance,
            },
        },
        "execution": {
            "artifacts_dir": str(config.execution.artifacts_dir),
            "ledger": str(config.execution.ledger),
            "timeout_seconds": config.execution.timeout_seconds,
            "env": {name: "<redacted>" for name in config.execution.env},
        },
    }


def _as_json_value(value: object) -> JSONValue:
    """Detach a structure through strict JSON to satisfy model metadata typing."""

    encoded = json.dumps(value, allow_nan=False)
    decoded: Any = json.loads(encoded)
    return cast(JSONValue, decoded)


_MANAGED_OUTPUT_FLAGS = frozenset(
    {
        "--experiment-name",
        "--output-dir",
        "--output-file",
        "--output-json",
        "--result-dir",
        "--result-filename",
    }
)


def _validate_base_args(base_args: tuple[str, ...]) -> None:
    for token in base_args:
        option = token.split("=", 1)[0]
        if option in _MANAGED_OUTPUT_FLAGS:
            raise CampaignSafetyError(
                f"benchmark.base_args cannot override adapter-managed option {option}"
            )


def _as_json_mapping(value: object) -> dict[str, JSONValue]:
    decoded = _as_json_value(value)
    if not isinstance(decoded, dict):  # pragma: no cover - defensive helper contract
        raise TypeError("expected a JSON object")
    return decoded


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CampaignRunResult",
    "CampaignSafetyError",
    "ExistingResultEvaluation",
    "PlannedExperiment",
    "adapter_for",
    "evaluate_existing_results",
    "plan_campaign",
    "run_campaign",
]
