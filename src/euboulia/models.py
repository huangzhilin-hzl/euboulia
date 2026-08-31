"""Framework-neutral domain models for performance experiments.

The models in this module deliberately avoid dependencies on SGLang, vLLM, or
any validation/serialization framework.  Every public model has a stable JSON
round-trip and stores extension fields in plain JSON-compatible mappings.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from numbers import Real
from typing import Any, ClassVar, TypeVar

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
Numeric = int | float

_T = TypeVar("_T", bound="JsonModel")


class Framework(StrEnum):
    """Serving framework targeted by a candidate."""

    SGLANG = "sglang"
    VLLM = "vllm"
    CUSTOM = "custom"


class MetricDirection(StrEnum):
    """Whether lower or higher values are better for a metric."""

    MINIMIZE = "min"
    MIN = "min"  # convenient alias
    MAXIMIZE = "max"
    MAX = "max"  # convenient alias


class ExperimentStatus(StrEnum):
    """Lifecycle state represented by an experiment ledger snapshot."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    COMPLETED = "succeeded"  # convenient alias
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerdictStatus(StrEnum):
    """Fail-closed decision emitted by a gate."""

    ACCEPT = "accept"
    PASS = "accept"  # convenient alias
    REJECT = "reject"
    FAIL = "reject"  # convenient alias


def utc_now() -> str:
    """Return an RFC 3339-style UTC timestamp without fractional seconds."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _enum_value(enum_type: type[Enum], value: object, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {choices}") from exc


def _json_value(value: object, path: str = "value") -> JSONValue:
    """Validate and detach a JSON-compatible value.

    Non-finite floats are rejected here because they are not valid JSON. Metric
    values are intentionally validated later, when serializing, so gates can
    still receive and safely reject a NaN/Inf produced by a failed benchmark.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} is not JSON-compatible: {type(value).__name__}")


def _json_mapping(value: Mapping[str, object] | None, field_name: str) -> dict[str, JSONValue]:
    normalized = _json_value({} if value is None else value, field_name)
    assert isinstance(normalized, dict)
    return normalized


def _string_mapping(value: Mapping[str, object] | None, field_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in ({} if value is None else value).items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"{field_name} must map strings to strings")
        result[key] = item
    return result


def _numeric_mapping(value: Mapping[str, object] | None, field_name: str) -> dict[str, Numeric]:
    result: dict[str, Numeric] = {}
    for key, item in ({} if value is None else value).items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if isinstance(item, bool) or not isinstance(item, Real):
            raise TypeError(f"{field_name}.{key} must be numeric")
        # Preserve integer counters while normalizing other Real implementations.
        result[key] = item if isinstance(item, (int, float)) else float(item)
    return result


def _sample_mapping(
    value: Mapping[str, object] | None, field_name: str
) -> dict[str, tuple[float, ...]]:
    result: dict[str, tuple[float, ...]] = {}
    for key, raw_samples in ({} if value is None else value).items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if not isinstance(raw_samples, (list, tuple)):
            raise TypeError(f"{field_name}.{key} must be a sequence of numbers")
        samples: list[float] = []
        for item in raw_samples:
            if isinstance(item, bool) or not isinstance(item, Real):
                raise TypeError(f"{field_name}.{key} must contain only numbers")
            samples.append(float(item))
        result[key] = tuple(samples)
    return result


class JsonModel:
    """Small serialization mixin shared by all domain models."""

    SCHEMA_VERSION: ClassVar[int] = 1

    def to_dict(self) -> dict[str, JSONValue]:  # pragma: no cover - abstract contract
        raise NotImplementedError

    @classmethod
    def from_dict(cls: type[_T], value: Mapping[str, object]) -> _T:  # pragma: no cover
        raise NotImplementedError

    def to_json(self, *, indent: int | None = None) -> str:
        # allow_nan=False is a second line of defense for metric values, which
        # may legitimately contain NaN/Inf before a gate rejects them.
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )

    @classmethod
    def from_json(cls: type[_T], payload: str | bytes | bytearray) -> _T:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError(f"{cls.__name__} JSON payload must be an object")
        return cls.from_dict(value)


@dataclass(frozen=True, slots=True)
class Workload(JsonModel):
    """A serving workload that can be run against either framework."""

    name: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    concurrency: int = 1
    num_requests: int = 1
    request_rate: float | None = None
    dataset: str | None = None
    seed: int = 0
    parameters: dict[str, JSONValue] = field(default_factory=dict)
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "name"))
        object.__setattr__(self, "model", _nonempty(self.model, "model"))
        for field_name in ("input_tokens", "output_tokens", "seed"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("input_tokens and output_tokens must be non-negative")
        for field_name in ("concurrency", "num_requests"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.request_rate is not None:
            if isinstance(self.request_rate, bool) or not isinstance(self.request_rate, Real):
                raise TypeError("request_rate must be numeric or None")
            if not math.isfinite(float(self.request_rate)) or self.request_rate <= 0:
                raise ValueError("request_rate must be finite and positive")
            object.__setattr__(self, "request_rate", float(self.request_rate))
        if self.dataset is not None and not isinstance(self.dataset, str):
            raise TypeError("dataset must be a string or None")
        object.__setattr__(self, "parameters", _json_mapping(self.parameters, "parameters"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "concurrency": self.concurrency,
            "num_requests": self.num_requests,
            "request_rate": self.request_rate,
            "dataset": self.dataset,
            "seed": self.seed,
            "parameters": _json_mapping(self.parameters, "parameters"),
            "metadata": _json_mapping(self.metadata, "metadata"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Workload:
        return cls(
            name=value.get("name"),  # type: ignore[arg-type]
            model=value.get("model"),  # type: ignore[arg-type]
            input_tokens=value.get("input_tokens", 0),  # type: ignore[arg-type]
            output_tokens=value.get("output_tokens", 0),  # type: ignore[arg-type]
            concurrency=value.get("concurrency", 1),  # type: ignore[arg-type]
            num_requests=value.get("num_requests", 1),  # type: ignore[arg-type]
            request_rate=value.get("request_rate"),  # type: ignore[arg-type]
            dataset=value.get("dataset"),  # type: ignore[arg-type]
            seed=value.get("seed", 0),  # type: ignore[arg-type]
            parameters=value.get("parameters", {}),  # type: ignore[arg-type]
            metadata=value.get("metadata", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Candidate(JsonModel):
    """A concrete framework/configuration/code candidate to evaluate."""

    candidate_id: str
    framework: Framework
    name: str = ""
    source_revision: str | None = None
    parameters: dict[str, JSONValue] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    patch: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        candidate_id = _nonempty(self.candidate_id, "candidate_id")
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "framework", _enum_value(Framework, self.framework, "framework"))
        object.__setattr__(self, "name", self.name.strip() if self.name else candidate_id)
        for field_name in ("source_revision", "patch"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        object.__setattr__(self, "parameters", _json_mapping(self.parameters, "parameters"))
        object.__setattr__(self, "environment", _string_mapping(self.environment, "environment"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "candidate_id": self.candidate_id,
            "framework": self.framework.value,
            "name": self.name,
            "source_revision": self.source_revision,
            "parameters": _json_mapping(self.parameters, "parameters"),
            "environment": dict(self.environment),
            "patch": self.patch,
            "metadata": _json_mapping(self.metadata, "metadata"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Candidate:
        return cls(
            candidate_id=value.get("candidate_id"),  # type: ignore[arg-type]
            framework=value.get("framework"),  # type: ignore[arg-type]
            name=value.get("name", ""),  # type: ignore[arg-type]
            source_revision=value.get("source_revision"),  # type: ignore[arg-type]
            parameters=value.get("parameters", {}),  # type: ignore[arg-type]
            environment=value.get("environment", {}),  # type: ignore[arg-type]
            patch=value.get("patch"),  # type: ignore[arg-type]
            metadata=value.get("metadata", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Metrics(JsonModel):
    """Dynamic scalar metrics and optional raw samples from one benchmark.

    Values are not restricted to a fixed vocabulary; adapters can report TTFT,
    TPOT, ITL, request throughput, token throughput, latency, and counters in the
    same object. NaN/Inf may be constructed so a gate can reject them, but valid
    JSON serialization intentionally refuses non-finite values.
    """

    values: dict[str, Numeric] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    samples: dict[str, tuple[float, ...]] = field(default_factory=dict)
    correctness_passed: bool | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _numeric_mapping(self.values, "values"))
        object.__setattr__(self, "units", _string_mapping(self.units, "units"))
        object.__setattr__(self, "samples", _sample_mapping(self.samples, "samples"))
        if self.correctness_passed is not None and not isinstance(self.correctness_passed, bool):
            raise TypeError("correctness_passed must be a bool or None")
        unknown_units = set(self.units) - set(self.values)
        if unknown_units:
            raise ValueError(f"units reference unknown metrics: {sorted(unknown_units)}")
        unknown_samples = set(self.samples) - set(self.values)
        if unknown_samples:
            raise ValueError(f"samples reference unknown metrics: {sorted(unknown_samples)}")
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    def get(self, name: str, default: Numeric | None = None) -> Numeric | None:
        return self.values.get(name, default)

    def __getitem__(self, name: str) -> Numeric:
        return self.values[name]

    def is_finite(self, name: str) -> bool:
        value = self.values.get(name)
        return value is not None and math.isfinite(float(value))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "values": dict(self.values),
            "units": dict(self.units),
            "samples": {key: list(samples) for key, samples in self.samples.items()},
            "correctness_passed": self.correctness_passed,
            "metadata": _json_mapping(self.metadata, "metadata"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Metrics:
        return cls(
            values=value.get("values", {}),  # type: ignore[arg-type]
            units=value.get("units", {}),  # type: ignore[arg-type]
            samples=value.get("samples", {}),  # type: ignore[arg-type]
            correctness_passed=value.get("correctness_passed"),  # type: ignore[arg-type]
            metadata=value.get("metadata", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Verdict(JsonModel):
    """Auditable gate decision for a candidate."""

    status: VerdictStatus
    reasons: tuple[str, ...] = ()
    correctness_passed: bool | None = None
    performance_passed: bool | None = None
    metric: str | None = None
    direction: MetricDirection | None = None
    baseline_value: float | None = None
    candidate_value: float | None = None
    relative_improvement: float | None = None
    allowed_relative_regression: float | None = None
    min_relative_improvement: float | None = None
    noise_tolerance: float | None = None
    details: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum_value(VerdictStatus, self.status, "status"))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        if self.direction is not None:
            object.__setattr__(
                self,
                "direction",
                _enum_value(MetricDirection, self.direction, "direction"),
            )
        for field_name in ("correctness_passed", "performance_passed"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{field_name} must be a bool or None")
        for field_name in (
            "baseline_value",
            "candidate_value",
            "relative_improvement",
            "allowed_relative_regression",
            "min_relative_improvement",
            "noise_tolerance",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise TypeError(f"{field_name} must be numeric or None")
                if not math.isfinite(float(value)):
                    raise ValueError(f"{field_name} must be finite or None")
                object.__setattr__(self, field_name, float(value))
        object.__setattr__(self, "details", _json_mapping(self.details, "details"))

    @property
    def accepted(self) -> bool:
        return self.status is VerdictStatus.ACCEPT

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "status": self.status.value,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "correctness_passed": self.correctness_passed,
            "performance_passed": self.performance_passed,
            "metric": self.metric,
            "direction": self.direction.value if self.direction is not None else None,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "relative_improvement": self.relative_improvement,
            "allowed_relative_regression": self.allowed_relative_regression,
            "min_relative_improvement": self.min_relative_improvement,
            "noise_tolerance": self.noise_tolerance,
            "details": _json_mapping(self.details, "details"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Verdict:
        return cls(
            status=value.get("status"),  # type: ignore[arg-type]
            reasons=tuple(value.get("reasons", ())),  # type: ignore[arg-type]
            correctness_passed=value.get("correctness_passed"),  # type: ignore[arg-type]
            performance_passed=value.get("performance_passed"),  # type: ignore[arg-type]
            metric=value.get("metric"),  # type: ignore[arg-type]
            direction=value.get("direction"),  # type: ignore[arg-type]
            baseline_value=value.get("baseline_value"),  # type: ignore[arg-type]
            candidate_value=value.get("candidate_value"),  # type: ignore[arg-type]
            relative_improvement=value.get("relative_improvement"),  # type: ignore[arg-type]
            allowed_relative_regression=value.get("allowed_relative_regression"),  # type: ignore[arg-type]
            min_relative_improvement=value.get("min_relative_improvement"),  # type: ignore[arg-type]
            noise_tolerance=value.get("noise_tolerance"),  # type: ignore[arg-type]
            details=value.get("details", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Experiment(JsonModel):
    """One immutable experiment snapshot written to the append-only ledger."""

    experiment_id: str
    workload: Workload
    candidate: Candidate
    status: ExperimentStatus = ExperimentStatus.PENDING
    metrics: Metrics | None = None
    baseline_metrics: Metrics | None = None
    verdict: Verdict | None = None
    created_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    artifacts: tuple[str, ...] = ()
    error: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", _nonempty(self.experiment_id, "experiment_id"))
        if not isinstance(self.workload, Workload):
            raise TypeError("workload must be a Workload")
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be a Candidate")
        object.__setattr__(self, "status", _enum_value(ExperimentStatus, self.status, "status"))
        for field_name, expected in (
            ("metrics", Metrics),
            ("baseline_metrics", Metrics),
            ("verdict", Verdict),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, expected):
                raise TypeError(f"{field_name} must be a {expected.__name__} or None")
        object.__setattr__(self, "created_at", _nonempty(self.created_at, "created_at"))
        for field_name in ("finished_at", "error"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        object.__setattr__(self, "artifacts", tuple(str(item) for item in self.artifacts))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "workload": self.workload.to_dict(),
            "candidate": self.candidate.to_dict(),
            "status": self.status.value,
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
            "baseline_metrics": (
                self.baseline_metrics.to_dict() if self.baseline_metrics is not None else None
            ),
            "verdict": self.verdict.to_dict() if self.verdict is not None else None,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "artifacts": list(self.artifacts),
            "error": self.error,
            "metadata": _json_mapping(self.metadata, "metadata"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Experiment:
        workload = value.get("workload")
        candidate = value.get("candidate")
        metrics = value.get("metrics")
        baseline_metrics = value.get("baseline_metrics")
        verdict = value.get("verdict")
        if not isinstance(workload, Mapping):
            raise TypeError("workload must be an object")
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be an object")
        for field_name, nested in (
            ("metrics", metrics),
            ("baseline_metrics", baseline_metrics),
            ("verdict", verdict),
        ):
            if nested is not None and not isinstance(nested, Mapping):
                raise TypeError(f"{field_name} must be an object or null")
        return cls(
            experiment_id=value.get("experiment_id"),  # type: ignore[arg-type]
            workload=Workload.from_dict(workload),
            candidate=Candidate.from_dict(candidate),
            status=value.get("status", ExperimentStatus.PENDING.value),  # type: ignore[arg-type]
            metrics=Metrics.from_dict(metrics) if isinstance(metrics, Mapping) else None,
            baseline_metrics=(
                Metrics.from_dict(baseline_metrics)
                if isinstance(baseline_metrics, Mapping)
                else None
            ),
            verdict=Verdict.from_dict(verdict) if isinstance(verdict, Mapping) else None,
            created_at=value.get("created_at", utc_now()),  # type: ignore[arg-type]
            finished_at=value.get("finished_at"),  # type: ignore[arg-type]
            artifacts=tuple(value.get("artifacts", ())),  # type: ignore[arg-type]
            error=value.get("error"),  # type: ignore[arg-type]
            metadata=value.get("metadata", {}),  # type: ignore[arg-type]
        )


__all__ = [
    "Candidate",
    "Experiment",
    "ExperimentStatus",
    "Framework",
    "JSONValue",
    "JsonModel",
    "MetricDirection",
    "Metrics",
    "Numeric",
    "Verdict",
    "VerdictStatus",
    "Workload",
    "utc_now",
]
