"""Framework-neutral, diagnostic-only profiler evidence models.

Profiler measurements are deliberately separated from unprofiled serving
measurements.  Instrumentation, kernel replay, shape/stack capture, and trace
collection can all perturb runtime, so no object in this module is eligible for
a performance gate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from numbers import Real

from euboulia.models import JsonModel, JSONValue


class ProfileSource(StrEnum):
    """Supported first-party profiler export formats."""

    TORCH_CHROME_TRACE = "torch_chrome_trace"
    NSYS_STATS_CSV = "nsys_stats_csv"
    NCU_CSV = "ncu_csv"


class MeasurementLane(StrEnum):
    """Measurement provenance used to enforce reward isolation."""

    SERVING_UNPROFILED = "serving_unprofiled"
    PROFILE_DIAGNOSTIC = "profile_diagnostic"


class SubjectKind(StrEnum):
    """Portable activity classes shared by the three parsers."""

    CPU_OP = "cpu_op"
    RUNTIME_API = "runtime_api"
    GPU_KERNEL = "gpu_kernel"
    MEMORY_TRANSFER = "memory_transfer"
    MEMORY_SET = "memory_set"
    NVTX_RANGE = "nvtx_range"
    COUNTER = "counter"
    UNKNOWN = "unknown"


class BottleneckKind(StrEnum):
    """Conservative, multi-label diagnostic hypotheses."""

    COMPUTE = "compute"
    MEMORY = "memory"
    OCCUPANCY = "occupancy"
    LAUNCH = "launch"
    SYNC = "sync"
    TRANSFER = "transfer"
    CPU_SUBMISSION = "cpu_submission"
    COMMUNICATION = "communication"
    UNDERUTILIZED = "underutilized"
    MIXED = "mixed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _enum(enum_type: type[StrEnum], value: object, name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        choices = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {choices}")
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {choices}") from exc


def _json_mapping(value: Mapping[str, object], name: str) -> dict[str, JSONValue]:
    try:
        payload = json.dumps(value, allow_nan=False)
        detached = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must contain only finite JSON-compatible values") from exc
    if not isinstance(detached, dict) or not all(isinstance(key, str) for key in detached):
        raise TypeError(f"{name} must be a string-keyed mapping")
    return detached


def _string_mapping(value: Mapping[str, object], name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"{name} must map strings to strings")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class MetricValue(JsonModel):
    """One normalized scalar while retaining its native profiler name."""

    value: float
    unit: str = ""
    native_name: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, Real):
            raise TypeError("value must be numeric")
        normalized = float(self.value)
        if not math.isfinite(normalized):
            raise ValueError("value must be finite")
        object.__setattr__(self, "value", normalized)
        if not isinstance(self.unit, str):
            raise TypeError("unit must be a string")
        if self.native_name is not None:
            object.__setattr__(self, "native_name", _text(self.native_name, "native_name"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"value": self.value, "unit": self.unit, "native_name": self.native_name}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MetricValue:
        return cls(
            value=value.get("value"),  # type: ignore[arg-type]
            unit=value.get("unit", ""),  # type: ignore[arg-type]
            native_name=value.get("native_name"),  # type: ignore[arg-type]
        )


def _metric_mapping(
    value: Mapping[str, MetricValue], name: str = "metrics"
) -> dict[str, MetricValue]:
    result: dict[str, MetricValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{name} keys must be non-empty strings")
        if not isinstance(item, MetricValue):
            raise TypeError(f"{name}.{key} must be a MetricValue")
        result[key] = item
    return result


def _metrics_from_object(value: object, name: str = "metrics") -> dict[str, MetricValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    result: dict[str, MetricValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            raise TypeError(f"{name} must map strings to metric objects")
        result[key] = MetricValue.from_dict(item)
    return result


@dataclass(frozen=True, slots=True)
class Observation(JsonModel):
    """A trace event, summary row, or NCU kernel-launch metric group."""

    observation_id: str
    source: ProfileSource
    subject_kind: SubjectKind
    name: str
    artifact: str
    start_ns: int | None = None
    duration_ns: int | None = None
    count: int | None = None
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    process_id: str | None = None
    thread_id: str | None = None
    device: str | None = None
    stream: str | None = None
    rank: str | None = None
    phase: str | None = None
    native_category: str | None = None
    correlation_ids: dict[str, str] = field(default_factory=dict)
    dimensions: dict[str, JSONValue] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    measurement_lane: MeasurementLane = MeasurementLane.PROFILE_DIAGNOSTIC

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        object.__setattr__(self, "source", _enum(ProfileSource, self.source, "source"))
        object.__setattr__(
            self, "subject_kind", _enum(SubjectKind, self.subject_kind, "subject_kind")
        )
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "artifact", _text(self.artifact, "artifact"))
        for field_name in ("start_ns", "duration_ns", "count"):
            item = getattr(self, field_name)
            if item is not None and (isinstance(item, bool) or not isinstance(item, int)):
                raise TypeError(f"{field_name} must be an integer or None")
            if item is not None and item < 0:
                raise ValueError(f"{field_name} must be non-negative")
        for field_name in (
            "process_id",
            "thread_id",
            "device",
            "stream",
            "rank",
            "phase",
            "native_category",
        ):
            object.__setattr__(
                self, field_name, _optional_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(self, "metrics", _metric_mapping(self.metrics))
        object.__setattr__(
            self, "correlation_ids", _string_mapping(self.correlation_ids, "correlation_ids")
        )
        object.__setattr__(self, "dimensions", _json_mapping(self.dimensions, "dimensions"))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        lane = _enum(MeasurementLane, self.measurement_lane, "measurement_lane")
        if lane is not MeasurementLane.PROFILE_DIAGNOSTIC:
            raise ValueError("profiler observations must remain in the profile_diagnostic lane")
        object.__setattr__(self, "measurement_lane", lane)

    @property
    def gate_eligible(self) -> bool:
        return False

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "observation_id": self.observation_id,
            "source": self.source.value,
            "subject_kind": self.subject_kind.value,
            "name": self.name,
            "artifact": self.artifact,
            "start_ns": self.start_ns,
            "duration_ns": self.duration_ns,
            "count": self.count,
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "process_id": self.process_id,
            "thread_id": self.thread_id,
            "device": self.device,
            "stream": self.stream,
            "rank": self.rank,
            "phase": self.phase,
            "native_category": self.native_category,
            "correlation_ids": dict(self.correlation_ids),
            "dimensions": _json_mapping(self.dimensions, "dimensions"),
            "warnings": list(self.warnings),
            "measurement_lane": self.measurement_lane.value,
            "gate_eligible": False,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Observation:
        if value.get("gate_eligible", False) is not False:
            raise ValueError("profiler observations cannot be gate eligible")
        return cls(
            observation_id=value.get("observation_id"),  # type: ignore[arg-type]
            source=value.get("source"),  # type: ignore[arg-type]
            subject_kind=value.get("subject_kind"),  # type: ignore[arg-type]
            name=value.get("name"),  # type: ignore[arg-type]
            artifact=value.get("artifact"),  # type: ignore[arg-type]
            start_ns=value.get("start_ns"),  # type: ignore[arg-type]
            duration_ns=value.get("duration_ns"),  # type: ignore[arg-type]
            count=value.get("count"),  # type: ignore[arg-type]
            metrics=_metrics_from_object(value.get("metrics", {})),
            process_id=value.get("process_id"),  # type: ignore[arg-type]
            thread_id=value.get("thread_id"),  # type: ignore[arg-type]
            device=value.get("device"),  # type: ignore[arg-type]
            stream=value.get("stream"),  # type: ignore[arg-type]
            rank=value.get("rank"),  # type: ignore[arg-type]
            phase=value.get("phase"),  # type: ignore[arg-type]
            native_category=value.get("native_category"),  # type: ignore[arg-type]
            correlation_ids=value.get("correlation_ids", {}),  # type: ignore[arg-type]
            dimensions=value.get("dimensions", {}),  # type: ignore[arg-type]
            warnings=tuple(value.get("warnings", ())),  # type: ignore[arg-type]
            measurement_lane=value.get(
                "measurement_lane", MeasurementLane.PROFILE_DIAGNOSTIC.value
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BottleneckFinding(JsonModel):
    """One versioned heuristic and the evidence that triggered it."""

    rule_id: str
    kind: BottleneckKind
    confidence: float
    evidence: tuple[str, ...]
    required_sources: tuple[ProfileSource, ...] = ()
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _text(self.rule_id, "rule_id"))
        object.__setattr__(self, "kind", _enum(BottleneckKind, self.kind, "kind"))
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, Real):
            raise TypeError("confidence must be numeric")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and in [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))
        object.__setattr__(
            self,
            "required_sources",
            tuple(_enum(ProfileSource, item, "required_sources") for item in self.required_sources),
        )
        object.__setattr__(self, "caveats", tuple(str(item) for item in self.caveats))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "required_sources": [item.value for item in self.required_sources],
            "caveats": list(self.caveats),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BottleneckFinding:
        return cls(
            rule_id=value.get("rule_id"),  # type: ignore[arg-type]
            kind=value.get("kind"),  # type: ignore[arg-type]
            confidence=value.get("confidence"),  # type: ignore[arg-type]
            evidence=tuple(value.get("evidence", ())),  # type: ignore[arg-type]
            required_sources=tuple(value.get("required_sources", ())),  # type: ignore[arg-type]
            caveats=tuple(value.get("caveats", ())),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Hotspot(JsonModel):
    """Aggregated activities with a denominator-scoped time share."""

    hotspot_id: str
    subject_kind: SubjectKind
    name: str
    total_duration_ns: int
    count: int
    share: float
    share_basis: str
    observation_ids: tuple[str, ...]
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    findings: tuple[BottleneckFinding, ...] = ()
    device: str | None = None
    rank: str | None = None
    phase: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hotspot_id", _text(self.hotspot_id, "hotspot_id"))
        object.__setattr__(
            self, "subject_kind", _enum(SubjectKind, self.subject_kind, "subject_kind")
        )
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "share_basis", _text(self.share_basis, "share_basis"))
        for field_name in ("total_duration_ns", "count"):
            item = getattr(self, field_name)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if isinstance(self.share, bool) or not isinstance(self.share, Real):
            raise TypeError("share must be numeric")
        share = float(self.share)
        if not math.isfinite(share) or not 0 <= share <= 1:
            raise ValueError("share must be finite and in [0, 1]")
        object.__setattr__(self, "share", share)
        object.__setattr__(self, "observation_ids", tuple(self.observation_ids))
        object.__setattr__(self, "metrics", _metric_mapping(self.metrics))
        if not all(isinstance(item, BottleneckFinding) for item in self.findings):
            raise TypeError("findings must contain BottleneckFinding objects")
        object.__setattr__(self, "findings", tuple(self.findings))
        for field_name in ("device", "rank", "phase"):
            object.__setattr__(
                self, field_name, _optional_text(getattr(self, field_name), field_name)
            )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "hotspot_id": self.hotspot_id,
            "subject_kind": self.subject_kind.value,
            "name": self.name,
            "total_duration_ns": self.total_duration_ns,
            "count": self.count,
            "share": self.share,
            "share_basis": self.share_basis,
            "observation_ids": list(self.observation_ids),
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "findings": [item.to_dict() for item in self.findings],
            "device": self.device,
            "rank": self.rank,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Hotspot:
        raw_findings = value.get("findings", ())
        if not isinstance(raw_findings, (list, tuple)):
            raise TypeError("findings must be an array")
        findings = tuple(
            BottleneckFinding.from_dict(item) for item in raw_findings if isinstance(item, Mapping)
        )
        if len(findings) != len(raw_findings):
            raise TypeError("findings must contain objects")
        return cls(
            hotspot_id=value.get("hotspot_id"),  # type: ignore[arg-type]
            subject_kind=value.get("subject_kind"),  # type: ignore[arg-type]
            name=value.get("name"),  # type: ignore[arg-type]
            total_duration_ns=value.get("total_duration_ns"),  # type: ignore[arg-type]
            count=value.get("count"),  # type: ignore[arg-type]
            share=value.get("share"),  # type: ignore[arg-type]
            share_basis=value.get("share_basis"),  # type: ignore[arg-type]
            observation_ids=tuple(value.get("observation_ids", ())),  # type: ignore[arg-type]
            metrics=_metrics_from_object(value.get("metrics", {})),
            findings=findings,
            device=value.get("device"),  # type: ignore[arg-type]
            rank=value.get("rank"),  # type: ignore[arg-type]
            phase=value.get("phase"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ProfileAnalysis(JsonModel):
    """Serializable diagnostic result that cannot be consumed as a reward."""

    profile_id: str
    sources: tuple[ProfileSource, ...]
    observation_count: int
    hotspots: tuple[Hotspot, ...] = ()
    findings: tuple[BottleneckFinding, ...] = ()
    warnings: tuple[str, ...] = ()
    measurement_lane: MeasurementLane = MeasurementLane.PROFILE_DIAGNOSTIC
    gate_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _text(self.profile_id, "profile_id"))
        object.__setattr__(
            self,
            "sources",
            tuple(_enum(ProfileSource, item, "sources") for item in self.sources),
        )
        if (
            isinstance(self.observation_count, bool)
            or not isinstance(self.observation_count, int)
            or self.observation_count < 0
        ):
            raise ValueError("observation_count must be a non-negative integer")
        if not all(isinstance(item, Hotspot) for item in self.hotspots):
            raise TypeError("hotspots must contain Hotspot objects")
        if not all(isinstance(item, BottleneckFinding) for item in self.findings):
            raise TypeError("findings must contain BottleneckFinding objects")
        object.__setattr__(self, "hotspots", tuple(self.hotspots))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        lane = _enum(MeasurementLane, self.measurement_lane, "measurement_lane")
        if lane is not MeasurementLane.PROFILE_DIAGNOSTIC or self.gate_eligible is not False:
            raise ValueError("profile analysis is diagnostic-only and cannot be gate eligible")
        object.__setattr__(self, "measurement_lane", lane)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "sources": [item.value for item in self.sources],
            "observation_count": self.observation_count,
            "hotspots": [item.to_dict() for item in self.hotspots],
            "findings": [item.to_dict() for item in self.findings],
            "warnings": list(self.warnings),
            "measurement_lane": self.measurement_lane.value,
            "gate_eligible": False,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProfileAnalysis:
        raw_hotspots = value.get("hotspots", ())
        raw_findings = value.get("findings", ())
        if not isinstance(raw_hotspots, (list, tuple)) or not isinstance(
            raw_findings, (list, tuple)
        ):
            raise TypeError("hotspots and findings must be arrays")
        hotspots = tuple(
            Hotspot.from_dict(item) for item in raw_hotspots if isinstance(item, Mapping)
        )
        findings = tuple(
            BottleneckFinding.from_dict(item) for item in raw_findings if isinstance(item, Mapping)
        )
        if len(hotspots) != len(raw_hotspots) or len(findings) != len(raw_findings):
            raise TypeError("hotspots and findings must contain objects")
        return cls(
            profile_id=value.get("profile_id"),  # type: ignore[arg-type]
            sources=tuple(value.get("sources", ())),  # type: ignore[arg-type]
            observation_count=value.get("observation_count"),  # type: ignore[arg-type]
            hotspots=hotspots,
            findings=findings,
            warnings=tuple(value.get("warnings", ())),  # type: ignore[arg-type]
            measurement_lane=value.get(
                "measurement_lane", MeasurementLane.PROFILE_DIAGNOSTIC.value
            ),  # type: ignore[arg-type]
            gate_eligible=value.get("gate_eligible", False),  # type: ignore[arg-type]
        )


def require_gate_eligible_lane(lane: MeasurementLane | str) -> None:
    """Fail closed unless a metric came from an unprofiled serving run."""

    normalized = _enum(MeasurementLane, lane, "measurement lane")
    if normalized is not MeasurementLane.SERVING_UNPROFILED:
        raise ValueError(
            "profile diagnostics are not reward metrics; rerun the candidate "
            "with unprofiled serving"
        )


__all__ = [
    "BottleneckFinding",
    "BottleneckKind",
    "Hotspot",
    "MeasurementLane",
    "MetricValue",
    "Observation",
    "ProfileAnalysis",
    "ProfileSource",
    "SubjectKind",
    "require_gate_eligible_lane",
]
