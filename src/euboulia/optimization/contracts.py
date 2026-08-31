"""Typed contracts shared by the iterative optimization pipeline.

This module is deliberately framework-neutral.  Components exchange immutable
records and artifact references instead of sharing implementation objects or
large profiler payloads in memory.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from numbers import Real
from pathlib import Path
from typing import Protocol, runtime_checkable

from euboulia.models import JSONValue, Numeric


class Capability(StrEnum):
    """A separately authorized class of side effect."""

    BENCHMARK_EXECUTION = "benchmark_execution"
    PROFILE_EXECUTION = "profile_execution"
    WORKSPACE_WRITE = "workspace_write"
    OWNED_SERVICE_LIFECYCLE = "owned_service_lifecycle"
    EXTERNAL_MODEL_OR_NETWORK = "external_model_or_network"


class RunState(StrEnum):
    PLANNED = "planned"
    BASELINING = "baselining"
    ITERATING = "iterating"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IterationState(StrEnum):
    CREATED = "created"
    PROFILING = "profiling"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PREPARING_WORKSPACE = "preparing_workspace"
    APPLYING_PATCH = "applying_patch"
    EVALUATING = "evaluating"
    RECORDING_MEMORY = "recording_memory"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INVALID = "invalid"
    FAILED = "failed"


class OutcomeStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INVALID = "invalid"
    FAILED = "failed"


class ChangeKind(StrEnum):
    PATCH_CATALOG = "patch_catalog"
    SERVER_PARAMETER = "server_parameter"
    PYTHON = "python"
    TRITON = "triton"
    CUDA = "cuda"


class EvaluationTierKind(StrEnum):
    SMOKE = "smoke"
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_nonempty(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, name)


def _finite_optional(value: object | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be numeric or None")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite or None")
    return result


def _json_value(value: object, path: str = "value") -> JSONValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            result[raw_key] = _json_value(item, f"{path}.{raw_key}")
        return result
    if isinstance(value, list | tuple):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} is not JSON-compatible: {type(value).__name__}")


def _json_mapping(value: Mapping[str, object] | None, name: str) -> dict[str, JSONValue]:
    normalized = _json_value({} if value is None else value, name)
    if not isinstance(normalized, dict):  # pragma: no cover - construction guarantees this
        raise TypeError(f"{name} must be a mapping")
    return normalized


def _numeric_mapping(value: Mapping[str, object], name: str) -> dict[str, Numeric]:
    result: dict[str, Numeric] = {}
    for raw_key, raw_value in value.items():
        key = _nonempty(raw_key, f"{name} key")
        if isinstance(raw_value, bool) or not isinstance(raw_value, Real):
            raise TypeError(f"{name}.{key} must be numeric")
        number: Numeric = raw_value if isinstance(raw_value, int | float) else float(raw_value)
        if not math.isfinite(float(number)):
            raise ValueError(f"{name}.{key} must be finite")
        result[key] = number
    return result


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Content-addressed reference to an immutable artifact."""

    artifact_id: str
    path: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _nonempty(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "path", _nonempty(self.path, "path"))
        digest = _nonempty(self.sha256, "sha256").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        object.__setattr__(self, "sha256", digest)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        object.__setattr__(self, "media_type", _nonempty(self.media_type, "media_type"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ArtifactRef:
        raw_metadata = value.get("metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        return cls(
            artifact_id=_nonempty(value.get("artifact_id"), "artifact_id"),
            path=_nonempty(value.get("path"), "path"),
            sha256=_nonempty(value.get("sha256"), "sha256"),
            size_bytes=_required_integer(value.get("size_bytes"), "size_bytes"),
            media_type=_nonempty(value.get("media_type", "application/octet-stream"), "media_type"),
            metadata=_json_mapping(raw_metadata, "metadata"),
        )


def _required_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class StageContext:
    run_id: str
    iteration_id: str
    artifact_dir: Path
    authorizations: frozenset[Capability] = frozenset()
    input_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _nonempty(self.run_id, "run_id"))
        object.__setattr__(self, "iteration_id", _nonempty(self.iteration_id, "iteration_id"))
        object.__setattr__(self, "artifact_dir", Path(self.artifact_dir))
        object.__setattr__(self, "authorizations", frozenset(self.authorizations))
        object.__setattr__(
            self, "input_digest", _optional_nonempty(self.input_digest, "input_digest")
        )


@dataclass(frozen=True, slots=True)
class ProfileRequest:
    candidate_id: str
    source_revision: str
    workload_digest: str
    max_bytes: int

    def __post_init__(self) -> None:
        for name in ("candidate_id", "source_revision", "workload_digest"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise TypeError("max_bytes must be an integer")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ProfileResult:
    profile_id: str
    provider: str
    candidate_id: str
    artifacts: tuple[ArtifactRef, ...]
    metrics: Mapping[str, Numeric] = field(default_factory=dict)
    complete: bool = True
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("profile_id", "provider", "candidate_id"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metrics", _numeric_mapping(self.metrics, "metrics"))
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    category: str
    summary: str
    confidence: float
    evidence_artifact_ids: tuple[str, ...]
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("finding_id", "category", "summary"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        confidence = _finite_optional(self.confidence, "confidence")
        if confidence is None or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            tuple(_nonempty(item, "evidence artifact id") for item in self.evidence_artifact_ids),
        )
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    analysis_id: str
    profile_id: str
    summary: str
    findings: tuple[Finding, ...]
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("analysis_id", "profile_id", "summary"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    proposal_id: str
    analysis_id: str
    title: str
    rationale: str
    change_kind: ChangeKind
    catalog_entry_id: str | None = None
    predicted_metric: str | None = None
    risk: str = "medium"
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("proposal_id", "analysis_id", "title", "rationale", "risk"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if not isinstance(self.change_kind, ChangeKind):
            object.__setattr__(self, "change_kind", ChangeKind(self.change_kind))
        object.__setattr__(
            self,
            "catalog_entry_id",
            _optional_nonempty(self.catalog_entry_id, "catalog_entry_id"),
        )
        object.__setattr__(
            self,
            "predicted_metric",
            _optional_nonempty(self.predicted_metric, "predicted_metric"),
        )
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    run_id: str
    iteration_id: str
    base_revision: str
    proposal: ChangeProposal

    def __post_init__(self) -> None:
        for name in ("run_id", "iteration_id", "base_revision"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    workspace_id: str
    path: Path
    base_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _nonempty(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "base_revision", _nonempty(self.base_revision, "base_revision"))


@dataclass(frozen=True, slots=True)
class PatchResult:
    workspace_id: str
    applied: bool
    patch_digest: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _nonempty(self.workspace_id, "workspace_id"))
        if not isinstance(self.applied, bool):
            raise TypeError("applied must be a bool")
        object.__setattr__(
            self, "patch_digest", _optional_nonempty(self.patch_digest, "patch_digest")
        )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or None")


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    workspace_id: str
    base_revision: str
    patch_digest: str
    changed_files: tuple[str, ...]
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("workspace_id", "base_revision", "patch_digest"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        object.__setattr__(
            self,
            "changed_files",
            tuple(_nonempty(item, "changed file") for item in self.changed_files),
        )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    candidate_id: str
    champion_id: str
    reference_baseline_id: str
    workspace: WorkspaceSnapshot
    tier_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("candidate_id", "champion_id", "reference_baseline_id"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        object.__setattr__(
            self, "tier_names", tuple(_nonempty(item, "tier name") for item in self.tier_names)
        )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluation_id: str
    candidate_id: str
    status: OutcomeStatus
    experiment_ids: tuple[str, ...]
    metrics: Mapping[str, Numeric] = field(default_factory=dict)
    relative_improvement: float | None = None
    reason: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("evaluation_id", "candidate_id"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if not isinstance(self.status, OutcomeStatus):
            object.__setattr__(self, "status", OutcomeStatus(self.status))
        object.__setattr__(
            self,
            "experiment_ids",
            tuple(_nonempty(item, "experiment id") for item in self.experiment_ids),
        )
        object.__setattr__(self, "metrics", _numeric_mapping(self.metrics, "metrics"))
        object.__setattr__(
            self,
            "relative_improvement",
            _finite_optional(self.relative_improvement, "relative_improvement"),
        )
        object.__setattr__(self, "reason", _optional_nonempty(self.reason, "reason"))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


@dataclass(frozen=True, slots=True)
class IterationOutcome:
    outcome_id: str
    run_id: str
    iteration_id: str
    proposal_id: str
    status: OutcomeStatus
    summary: str
    champion_before: str
    champion_after: str
    relative_improvement: float | None = None
    patch_digest: str | None = None
    experiment_ids: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "outcome_id",
            "run_id",
            "iteration_id",
            "proposal_id",
            "summary",
            "champion_before",
            "champion_after",
        ):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if not isinstance(self.status, OutcomeStatus):
            object.__setattr__(self, "status", OutcomeStatus(self.status))
        object.__setattr__(
            self,
            "relative_improvement",
            _finite_optional(self.relative_improvement, "relative_improvement"),
        )
        object.__setattr__(
            self, "patch_digest", _optional_nonempty(self.patch_digest, "patch_digest")
        )
        object.__setattr__(
            self,
            "experiment_ids",
            tuple(_nonempty(item, "experiment id") for item in self.experiment_ids),
        )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))

    @property
    def accepted(self) -> bool:
        return self.status is OutcomeStatus.ACCEPTED

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "iteration_id": self.iteration_id,
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "summary": self.summary,
            "champion_before": self.champion_before,
            "champion_after": self.champion_after,
            "relative_improvement": self.relative_improvement,
            "patch_digest": self.patch_digest,
            "experiment_ids": list(self.experiment_ids),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    memory_id: str
    outcome_id: str
    run_id: str
    iteration_id: str
    framework: str
    framework_revision: str
    hardware_fingerprint: str
    model_revision: str
    workload_digest: str
    benchmark_policy_digest: str
    proposal_id: str
    outcome: OutcomeStatus
    summary: str
    patch_digest: str | None = None
    relative_improvement: float | None = None
    created_at: str = field(default_factory=utc_now)
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "memory_id",
            "outcome_id",
            "run_id",
            "iteration_id",
            "framework",
            "framework_revision",
            "hardware_fingerprint",
            "model_revision",
            "workload_digest",
            "benchmark_policy_digest",
            "proposal_id",
            "summary",
            "created_at",
        ):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if not isinstance(self.outcome, OutcomeStatus):
            object.__setattr__(self, "outcome", OutcomeStatus(self.outcome))
        object.__setattr__(
            self, "patch_digest", _optional_nonempty(self.patch_digest, "patch_digest")
        )
        object.__setattr__(
            self,
            "relative_improvement",
            _finite_optional(self.relative_improvement, "relative_improvement"),
        )
        object.__setattr__(self, "details", _json_mapping(self.details, "details"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "memory_id": self.memory_id,
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "iteration_id": self.iteration_id,
            "framework": self.framework,
            "framework_revision": self.framework_revision,
            "hardware_fingerprint": self.hardware_fingerprint,
            "model_revision": self.model_revision,
            "workload_digest": self.workload_digest,
            "benchmark_policy_digest": self.benchmark_policy_digest,
            "proposal_id": self.proposal_id,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "patch_digest": self.patch_digest,
            "relative_improvement": self.relative_improvement,
            "created_at": self.created_at,
            "details": dict(self.details),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MemoryEntry:
        raw_details = value.get("details", {})
        if not isinstance(raw_details, Mapping):
            raise TypeError("details must be a mapping")
        raw_improvement = value.get("relative_improvement")
        return cls(
            memory_id=_nonempty(value.get("memory_id"), "memory_id"),
            outcome_id=_nonempty(value.get("outcome_id"), "outcome_id"),
            run_id=_nonempty(value.get("run_id"), "run_id"),
            iteration_id=_nonempty(value.get("iteration_id"), "iteration_id"),
            framework=_nonempty(value.get("framework"), "framework"),
            framework_revision=_nonempty(value.get("framework_revision"), "framework_revision"),
            hardware_fingerprint=_nonempty(
                value.get("hardware_fingerprint"), "hardware_fingerprint"
            ),
            model_revision=_nonempty(value.get("model_revision"), "model_revision"),
            workload_digest=_nonempty(value.get("workload_digest"), "workload_digest"),
            benchmark_policy_digest=_nonempty(
                value.get("benchmark_policy_digest"), "benchmark_policy_digest"
            ),
            proposal_id=_nonempty(value.get("proposal_id"), "proposal_id"),
            outcome=OutcomeStatus(_nonempty(value.get("outcome"), "outcome")),
            summary=_nonempty(value.get("summary"), "summary"),
            patch_digest=_optional_nonempty(value.get("patch_digest"), "patch_digest"),
            relative_improvement=_finite_optional(raw_improvement, "relative_improvement"),
            created_at=_nonempty(value.get("created_at"), "created_at"),
            details=_json_mapping(raw_details, "details"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> MemoryEntry:
        raw: object = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("memory entry JSON must be an object")
        return cls.from_dict(raw)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    framework: str | None = None
    framework_revision: str | None = None
    hardware_fingerprint: str | None = None
    model_revision: str | None = None
    workload_digest: str | None = None
    benchmark_policy_digest: str | None = None
    outcomes: tuple[OutcomeStatus, ...] = ()
    limit: int = 20

    def __post_init__(self) -> None:
        for name in (
            "framework",
            "framework_revision",
            "hardware_fingerprint",
            "model_revision",
            "workload_digest",
            "benchmark_policy_digest",
        ):
            object.__setattr__(self, name, _optional_nonempty(getattr(self, name), name))
        object.__setattr__(
            self,
            "outcomes",
            tuple(
                outcome if isinstance(outcome, OutcomeStatus) else OutcomeStatus(outcome)
                for outcome in self.outcomes
            ),
        )
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if self.limit <= 0:
            raise ValueError("limit must be positive")


@runtime_checkable
class Profiler(Protocol):
    def capture(self, request: ProfileRequest, context: StageContext) -> ProfileResult: ...


@runtime_checkable
class Analyzer(Protocol):
    def analyze(
        self,
        profile: ProfileResult,
        recalled: tuple[MemoryEntry, ...],
        context: StageContext,
    ) -> AnalysisReport: ...


@runtime_checkable
class Planner(Protocol):
    def propose(
        self,
        report: AnalysisReport,
        recalled: tuple[MemoryEntry, ...],
        context: StageContext,
    ) -> tuple[ChangeProposal, ...]: ...


@runtime_checkable
class PatchWorkspace(Protocol):
    def prepare(self, request: WorkspaceRequest) -> WorkspaceHandle: ...

    def apply(self, handle: WorkspaceHandle, proposal: ChangeProposal) -> PatchResult: ...

    def snapshot(self, handle: WorkspaceHandle) -> WorkspaceSnapshot: ...

    def discard(self, handle: WorkspaceHandle) -> None: ...


@runtime_checkable
class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest, context: StageContext) -> EvaluationResult: ...


@runtime_checkable
class MemoryStore(Protocol):
    def record(self, entry: MemoryEntry) -> MemoryEntry: ...

    def recall(self, query: MemoryQuery) -> tuple[MemoryEntry, ...]: ...

    def rebuild(self, entries: Iterable[MemoryEntry]) -> int: ...


__all__ = [
    "AnalysisReport",
    "Analyzer",
    "ArtifactRef",
    "Capability",
    "ChangeKind",
    "ChangeProposal",
    "EvaluationRequest",
    "EvaluationResult",
    "EvaluationTierKind",
    "Evaluator",
    "Finding",
    "IterationOutcome",
    "IterationState",
    "MemoryEntry",
    "MemoryQuery",
    "MemoryStore",
    "OutcomeStatus",
    "PatchResult",
    "PatchWorkspace",
    "Planner",
    "ProfileRequest",
    "ProfileResult",
    "Profiler",
    "RunState",
    "StageContext",
    "WorkspaceHandle",
    "WorkspaceRequest",
    "WorkspaceSnapshot",
    "utc_now",
]
