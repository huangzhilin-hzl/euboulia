"""Authorized, fail-fast evaluation of optimization candidates.

The evaluator deliberately knows nothing about SGLang, vLLM, or optimization
contracts.  It runs already-approved argv command specifications in three fixed
tiers and emits a small, serializable result that an orchestrator can adapt to its
own ledger types.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from euboulia.execution import CommandExecutor, ExecutionResult


class EvaluationError(RuntimeError):
    """Base class for evaluator policy and execution errors."""


class EvaluationAuthorizationError(EvaluationError):
    """Raised for missing, forged, stale, or already-consumed authorization."""


class MetricsError(EvaluationError):
    """Raised when benchmark metrics are absent, malformed, or unsafe to read."""


class EvaluationStage(StrEnum):
    PREFLIGHT = "preflight"
    CORRECTNESS = "correctness"
    BENCHMARK = "benchmark"


class ObjectiveDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class EvaluationOutcome(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    FAILED = "failed"
    PROFILE_ONLY = "profile_only"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One command.  A shell command string is intentionally not representable."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float | None = None
    env_overrides: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "command name"))
        object.__setattr__(self, "argv", _argv(self.argv))
        object.__setattr__(
            self,
            "timeout_seconds",
            _optional_positive_number(self.timeout_seconds, "timeout_seconds"),
        )
        object.__setattr__(self, "env_overrides", _environment(self.env_overrides))


@dataclass(frozen=True, slots=True)
class ObjectiveSpec:
    """Promotion gate for one numeric metric.

    ``absolute_threshold`` is interpreted according to ``direction``.  Relative
    improvement is positive when a candidate is better than ``baseline``.
    """

    metric: str
    direction: ObjectiveDirection
    baseline: float | None = None
    minimum_relative_improvement: float = 0.0
    absolute_threshold: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _nonempty(self.metric, "objective metric"))
        if not isinstance(self.direction, ObjectiveDirection):
            object.__setattr__(self, "direction", ObjectiveDirection(self.direction))
        baseline = _optional_finite_number(self.baseline, "baseline")
        minimum = _finite_number(self.minimum_relative_improvement, "minimum_relative_improvement")
        absolute = _optional_finite_number(self.absolute_threshold, "absolute_threshold")
        if baseline is None and minimum != 0.0:
            raise ValueError("a baseline is required for a relative-improvement gate")
        if baseline == 0.0 and minimum != 0.0:
            raise ValueError("a non-zero baseline is required for a relative-improvement gate")
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "minimum_relative_improvement", minimum)
        object.__setattr__(self, "absolute_threshold", absolute)


@dataclass(frozen=True, slots=True)
class BenchmarkSpec:
    command: CommandSpec
    metrics_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics_path", Path(self.metrics_path))
        if not self.metrics_path.name:
            raise ValueError("metrics_path must name a file")


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    """Complete immutable intent submitted for explicit authorization."""

    trial_id: str
    workspace: Path
    preflight: tuple[CommandSpec, ...]
    correctness: tuple[CommandSpec, ...]
    benchmark: BenchmarkSpec
    objective: ObjectiveSpec
    profiler_trial: bool = False

    def __post_init__(self) -> None:
        trial_id = _nonempty(self.trial_id, "trial_id")
        if _SAFE_ID.fullmatch(trial_id) is None or trial_id in {".", ".."}:
            raise ValueError("trial_id contains unsafe path characters")
        object.__setattr__(self, "trial_id", trial_id)
        object.__setattr__(self, "workspace", Path(self.workspace))
        object.__setattr__(self, "preflight", _commands(self.preflight, "preflight"))
        object.__setattr__(self, "correctness", _commands(self.correctness, "correctness"))
        if not isinstance(self.benchmark, BenchmarkSpec):
            raise TypeError("benchmark must be a BenchmarkSpec")
        if not isinstance(self.objective, ObjectiveSpec):
            raise TypeError("objective must be an ObjectiveSpec")
        if not isinstance(self.profiler_trial, bool):
            raise TypeError("profiler_trial must be a bool")


@dataclass(frozen=True, slots=True)
class AuthorizedEvaluation:
    """Single-use capability returned by :meth:`TieredEvaluator.authorize`."""

    authorization_id: str
    plan: EvaluationPlan
    plan_digest: str


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: EvaluationStage
    executions: tuple[ExecutionResult, ...]
    succeeded: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "executions": [execution.to_dict() for execution in self.executions],
            "succeeded": self.succeeded,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TieredEvaluationResult:
    trial_id: str
    outcome: EvaluationOutcome
    stages: tuple[StageResult, ...]
    metrics: Mapping[str, float]
    objective_value: float | None
    relative_improvement: float | None
    gate_passed: bool
    profiler_trial: bool
    promotable: bool
    artifact_dir: Path
    failure_stage: EvaluationStage | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", dict(self.metrics))

    def to_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "outcome": self.outcome.value,
            "stages": [stage.to_dict() for stage in self.stages],
            "metrics": dict(self.metrics),
            "objective_value": self.objective_value,
            "relative_improvement": self.relative_improvement,
            "gate_passed": self.gate_passed,
            "profiler_trial": self.profiler_trial,
            "promotable": self.promotable,
            "artifact_dir": str(self.artifact_dir),
            "failure_stage": (self.failure_stage.value if self.failure_stage is not None else None),
            "reason": self.reason,
        }


class TieredEvaluator:
    """Run preflight, correctness, and benchmark tiers with fail-fast semantics."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        default_timeout_seconds: float = 300.0,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        timeout = _optional_positive_number(default_timeout_seconds, "default_timeout_seconds")
        if timeout is None:  # pragma: no cover - the annotation excludes None
            raise ValueError("default_timeout_seconds is required")
        self.default_timeout_seconds = timeout
        self._authorizations: dict[str, str] = {}

    def authorize(self, plan: EvaluationPlan, *, approved: bool) -> AuthorizedEvaluation:
        """Validate intent and mint a single-use execution capability."""

        if not approved:
            raise EvaluationAuthorizationError("evaluation requires approved=True")
        if not isinstance(plan, EvaluationPlan):
            raise TypeError("plan must be an EvaluationPlan")
        workspace = plan.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        _resolve_metrics_path(plan.benchmark.metrics_path, workspace)
        digest = _plan_digest(plan)
        authorization_id = uuid.uuid4().hex
        self._authorizations[authorization_id] = digest
        return AuthorizedEvaluation(
            authorization_id=authorization_id,
            plan=plan,
            plan_digest=digest,
        )

    def execute(self, authorization: AuthorizedEvaluation) -> TieredEvaluationResult:
        """Consume an authorization and execute its plan exactly once."""

        if not isinstance(authorization, AuthorizedEvaluation):
            raise TypeError("authorization must be an AuthorizedEvaluation")
        expected = self._authorizations.pop(authorization.authorization_id, None)
        actual = _plan_digest(authorization.plan)
        if expected is None:
            raise EvaluationAuthorizationError("authorization is unknown or already consumed")
        if expected != authorization.plan_digest or expected != actual:
            raise EvaluationAuthorizationError("evaluation plan changed after authorization")

        plan = authorization.plan
        workspace = plan.workspace.resolve(strict=True)
        artifact_dir = self.artifact_root / plan.trial_id / authorization.authorization_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        stages: list[StageResult] = []

        for stage, commands in (
            (EvaluationStage.PREFLIGHT, plan.preflight),
            (EvaluationStage.CORRECTNESS, plan.correctness),
        ):
            stage_result = self._run_commands(stage, commands, workspace, artifact_dir)
            stages.append(stage_result)
            if not stage_result.succeeded:
                result = self._failed_result(plan, artifact_dir, stages, stage_result)
                self._write_summary(result)
                return result

        benchmark_result = self._run_commands(
            EvaluationStage.BENCHMARK,
            (plan.benchmark.command,),
            workspace,
            artifact_dir,
        )
        if not benchmark_result.succeeded:
            stages.append(benchmark_result)
            result = self._failed_result(plan, artifact_dir, stages, benchmark_result)
            self._write_summary(result)
            return result

        try:
            metrics_path = _resolve_metrics_path(plan.benchmark.metrics_path, workspace)
            metrics = parse_metrics(metrics_path)
            objective_value = metrics[plan.objective.metric]
        except (KeyError, MetricsError, OSError) as exc:
            parse_reason = (
                f"objective metric {plan.objective.metric!r} is missing"
                if isinstance(exc, KeyError)
                else str(exc)
            )
            failed_benchmark = StageResult(
                stage=EvaluationStage.BENCHMARK,
                executions=benchmark_result.executions,
                succeeded=False,
                reason=parse_reason,
            )
            stages.append(failed_benchmark)
            result = self._failed_result(plan, artifact_dir, stages, failed_benchmark)
            self._write_summary(result)
            return result

        stages.append(benchmark_result)
        relative = _relative_improvement(objective_value, plan.objective)
        gate_passed, gate_reason = _gate(objective_value, relative, plan.objective)
        if plan.profiler_trial:
            outcome = EvaluationOutcome.PROFILE_ONLY
            promotable = False
            if gate_reason is None:
                gate_reason = "profiler trials are diagnostic and cannot be promoted"
        elif gate_passed:
            outcome = EvaluationOutcome.PASSED
            promotable = True
        else:
            outcome = EvaluationOutcome.REJECTED
            promotable = False
        result = TieredEvaluationResult(
            trial_id=plan.trial_id,
            outcome=outcome,
            stages=tuple(stages),
            metrics=metrics,
            objective_value=objective_value,
            relative_improvement=relative,
            gate_passed=gate_passed,
            profiler_trial=plan.profiler_trial,
            promotable=promotable,
            artifact_dir=artifact_dir,
            reason=gate_reason,
        )
        self._write_summary(result)
        return result

    def _run_commands(
        self,
        stage: EvaluationStage,
        commands: Sequence[CommandSpec],
        workspace: Path,
        artifact_dir: Path,
    ) -> StageResult:
        executor = CommandExecutor(
            artifact_dir / stage.value,
            default_timeout_seconds=self.default_timeout_seconds,
        )
        executions: list[ExecutionResult] = []
        for command in commands:
            execution = executor.run(
                command.argv,
                cwd=workspace,
                timeout_seconds=command.timeout_seconds,
                env_overrides=command.env_overrides,
                artifact_prefix=command.name,
            )
            executions.append(execution)
            if not execution.succeeded:
                reason = _execution_failure(command.name, execution)
                return StageResult(stage, tuple(executions), False, reason)
        return StageResult(stage, tuple(executions), True)

    @staticmethod
    def _failed_result(
        plan: EvaluationPlan,
        artifact_dir: Path,
        stages: Sequence[StageResult],
        failed_stage: StageResult,
    ) -> TieredEvaluationResult:
        return TieredEvaluationResult(
            trial_id=plan.trial_id,
            outcome=EvaluationOutcome.FAILED,
            stages=tuple(stages),
            metrics={},
            objective_value=None,
            relative_improvement=None,
            gate_passed=False,
            profiler_trial=plan.profiler_trial,
            promotable=False,
            artifact_dir=artifact_dir,
            failure_stage=failed_stage.stage,
            reason=failed_stage.reason,
        )

    @staticmethod
    def _write_summary(result: TieredEvaluationResult) -> None:
        (result.artifact_dir / "evaluation.json").write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def parse_metrics(path: str | Path) -> dict[str, float]:
    """Parse numeric metrics from a JSON object or the last JSONL object.

    A nested ``metrics`` object is treated as the canonical namespace, while other
    nested numeric values are exposed with dotted keys.
    """

    metrics_path = Path(path)
    try:
        text = metrics_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MetricsError("metrics must be UTF-8 JSON") from exc
    if not text.strip():
        raise MetricsError("metrics file is empty")
    try:
        if metrics_path.suffix.casefold() == ".jsonl":
            lines = [line for line in text.splitlines() if line.strip()]
            raw: object = json.loads(lines[-1])
        else:
            raw = json.loads(text)
    except (json.JSONDecodeError, IndexError) as exc:
        raise MetricsError(f"invalid metrics JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MetricsError("metrics payload must be a JSON object")

    payload = cast(dict[object, object], raw)
    metrics: dict[str, float] = {}
    nested = payload.get("metrics")
    if isinstance(nested, dict):
        _collect_metrics(cast(dict[object, object], nested), "", metrics)
    for key, value in payload.items():
        if key == "metrics":
            continue
        if not isinstance(key, str) or not key:
            raise MetricsError("metrics object keys must be non-empty strings")
        _collect_value(value, key, metrics)
    if not metrics:
        raise MetricsError("metrics payload contains no finite numeric values")
    return metrics


def _collect_metrics(value: Mapping[object, object], prefix: str, output: dict[str, float]) -> None:
    for raw_key, child in value.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise MetricsError("metrics object keys must be non-empty strings")
        key = f"{prefix}.{raw_key}" if prefix else raw_key
        _collect_value(child, key, output)


def _collect_value(value: object, key: str, output: dict[str, float]) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str | list):
        return
    if isinstance(value, int | float):
        number = float(value)
        if not math.isfinite(number):
            raise MetricsError(f"metric {key!r} is not finite")
        output[key] = number
        return
    if isinstance(value, dict):
        _collect_metrics(cast(dict[object, object], value), key, output)


def _gate(
    value: float, relative: float | None, objective: ObjectiveSpec
) -> tuple[bool, str | None]:
    threshold = objective.absolute_threshold
    if threshold is not None:
        absolute_passed = (
            value >= threshold
            if objective.direction is ObjectiveDirection.MAXIMIZE
            else value <= threshold
        )
        if not absolute_passed:
            comparison = ">=" if objective.direction is ObjectiveDirection.MAXIMIZE else "<="
            return False, f"objective gate failed: {value:g} is not {comparison} {threshold:g}"
    if relative is not None and relative < objective.minimum_relative_improvement:
        return (
            False,
            "relative-improvement gate failed: "
            f"{relative:g} < {objective.minimum_relative_improvement:g}",
        )
    return True, None


def _relative_improvement(value: float, objective: ObjectiveSpec) -> float | None:
    baseline = objective.baseline
    if baseline is None or baseline == 0.0:
        return None
    if objective.direction is ObjectiveDirection.MAXIMIZE:
        return (value - baseline) / abs(baseline)
    return (baseline - value) / abs(baseline)


def _resolve_metrics_path(path: Path, workspace: Path) -> Path:
    candidate = path if path.is_absolute() else workspace / path
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(workspace):
        raise MetricsError("metrics_path must remain inside the evaluation workspace")
    return resolved


def _plan_digest(plan: EvaluationPlan) -> str:
    def command_payload(command: CommandSpec) -> dict[str, object]:
        return {
            "name": command.name,
            "argv": list(command.argv),
            "timeout_seconds": command.timeout_seconds,
            "env_overrides": dict(sorted(command.env_overrides.items())),
        }

    payload = {
        "trial_id": plan.trial_id,
        "workspace": str(plan.workspace.resolve(strict=False)),
        "preflight": [command_payload(command) for command in plan.preflight],
        "correctness": [command_payload(command) for command in plan.correctness],
        "benchmark": {
            "command": command_payload(plan.benchmark.command),
            "metrics_path": str(plan.benchmark.metrics_path),
        },
        "objective": {
            "metric": plan.objective.metric,
            "direction": plan.objective.direction.value,
            "baseline": plan.objective.baseline,
            "minimum_relative_improvement": plan.objective.minimum_relative_improvement,
            "absolute_threshold": plan.objective.absolute_threshold,
        },
        "profiler_trial": plan.profiler_trial,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _execution_failure(name: str, result: ExecutionResult) -> str:
    detail = result.error or f"exit status {result.returncode}"
    return f"command {name!r} failed: {detail}; stderr artifact: {result.stderr_path}"


def _commands(value: Sequence[CommandSpec], name: str) -> tuple[CommandSpec, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of CommandSpec")
    commands = tuple(value)
    if any(not isinstance(command, CommandSpec) for command in commands):
        raise TypeError(f"{name} must contain only CommandSpec values")
    return commands


def _argv(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError("argv must be a sequence of strings, not a command string")
    argv = tuple(value)
    if not argv:
        raise ValueError("argv must not be empty")
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            raise TypeError(f"argv[{index}] must be a string")
        if "\x00" in item:
            raise ValueError(f"argv[{index}] contains a NUL byte")
    if not argv[0]:
        raise ValueError("argv[0] must name an executable")
    return argv


def _environment(value: Mapping[str, str | None]) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise TypeError("env_overrides must be a mapping")
    result: dict[str, str | None] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ValueError(f"invalid environment variable name: {name!r}")
        if item is not None and (not isinstance(item, str) or "\x00" in item):
            raise ValueError(f"invalid environment value for {name!r}")
        result[name] = item
    return result


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _optional_finite_number(value: object | None, name: str) -> float | None:
    return None if value is None else _finite_number(value, name)


def _optional_positive_number(value: object | None, name: str) -> float | None:
    number = _optional_finite_number(value, name)
    if number is not None and number <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return number


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


__all__ = [
    "AuthorizedEvaluation",
    "BenchmarkSpec",
    "CommandSpec",
    "EvaluationAuthorizationError",
    "EvaluationError",
    "EvaluationOutcome",
    "EvaluationPlan",
    "EvaluationStage",
    "MetricsError",
    "ObjectiveDirection",
    "ObjectiveSpec",
    "StageResult",
    "TieredEvaluationResult",
    "TieredEvaluator",
    "parse_metrics",
]
