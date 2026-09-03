"""Append-only event stream for iterative optimization runs."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from euboulia.models import JSONValue
from euboulia.optimization.contracts import (
    ArtifactRef,
    _json_mapping,
    _nonempty,
    _optional_nonempty,
    utc_now,
)

try:  # pragma: no cover - exercised on Unix; fallback keeps Windows usable
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class EventType(StrEnum):
    RUN_PLANNED = "run.planned"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_STOP_REQUESTED = "run.stop_requested"
    RUN_STOPPED = "run.stopped"
    RUN_FAILED = "run.failed"
    BASELINE_STARTED = "baseline.started"
    BASELINE_ESTABLISHED = "baseline.established"
    BASELINE_INVALID = "baseline.invalid"
    ITERATION_STARTED = "iteration.started"
    BUDGET_RESERVED = "budget.reserved"
    ITERATION_COMPLETED = "iteration.completed"
    ITERATION_FAILED = "iteration.failed"
    PROFILE_STARTED = "profile.started"
    PROFILE_COMPLETED = "profile.completed"
    PROFILE_FAILED = "profile.failed"
    ANALYSIS_COMPLETED = "analysis.completed"
    ANALYSIS_FAILED = "analysis.failed"
    PROPOSAL_CREATED = "proposal.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_EXPIRED = "approval.expired"
    RUNTIME_PROVENANCE_CAPTURED = "runtime_provenance.captured"
    TARGET_MATERIALIZED = "target.materialized"
    WORKSPACE_PREPARED = "workspace.prepared"
    PATCH_APPLIED = "patch.applied"
    PATCH_REJECTED = "patch.rejected"
    SERVER_ARGUMENTS_APPLIED = "server_arguments.applied"
    WORKSPACE_DISCARDED = "workspace.discarded"
    BUILD_STARTED = "build.started"
    BUILD_COMPLETED = "build.completed"
    BUILD_FAILED = "build.failed"
    SERVICE_STARTING = "service.starting"
    SERVICE_STARTED = "service.started"
    SERVICE_READY = "service.ready"
    SERVICE_STOPPING = "service.stopping"
    SERVICE_STOPPED = "service.stopped"
    SERVICE_FAILED = "service.failed"
    EVALUATION_STARTED = "evaluation.started"
    EVALUATION_COMPLETED = "evaluation.completed"
    EVALUATION_INVALID = "evaluation.invalid"
    VERDICT_RECORDED = "verdict.recorded"
    CHAMPION_UPDATED = "champion.updated"
    MEMORY_RECORDED = "memory.recorded"


@dataclass(frozen=True, slots=True)
class OptimizationEvent:
    """One replayable state transition with content-addressed evidence refs."""

    event_id: str
    event_type: EventType
    run_id: str
    occurred_at: str = field(default_factory=utc_now)
    iteration_id: str | None = None
    causation_id: str | None = None
    entity_id: str | None = None
    input_digest: str | None = None
    payload: Mapping[str, JSONValue] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _nonempty(self.event_id, "event_id"))
        if not isinstance(self.event_type, EventType):
            object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(self, "run_id", _nonempty(self.run_id, "run_id"))
        object.__setattr__(self, "occurred_at", _nonempty(self.occurred_at, "occurred_at"))
        for name in ("iteration_id", "causation_id", "entity_id", "input_digest"):
            object.__setattr__(self, name, _optional_nonempty(getattr(self, name), name))
        object.__setattr__(self, "payload", _json_mapping(self.payload, "payload"))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if not all(isinstance(artifact, ArtifactRef) for artifact in self.artifacts):
            raise TypeError("artifacts must contain only ArtifactRef values")

    @classmethod
    def create(
        cls,
        event_type: EventType,
        run_id: str,
        *,
        iteration_id: str | None = None,
        causation_id: str | None = None,
        entity_id: str | None = None,
        input_digest: str | None = None,
        payload: Mapping[str, JSONValue] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> OptimizationEvent:
        return cls(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            run_id=run_id,
            iteration_id=iteration_id,
            causation_id=causation_id,
            entity_id=entity_id,
            input_digest=input_digest,
            payload={} if payload is None else payload,
            artifacts=artifacts,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "run_id": self.run_id,
            "occurred_at": self.occurred_at,
            "iteration_id": self.iteration_id,
            "causation_id": self.causation_id,
            "entity_id": self.entity_id,
            "input_digest": self.input_digest,
            "payload": dict(self.payload),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OptimizationEvent:
        schema_version = value.get("schema_version")
        if schema_version != cls.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported optimization event schema_version {schema_version!r}; "
                f"expected {cls.SCHEMA_VERSION}"
            )
        raw_payload = value.get("payload", {})
        if not isinstance(raw_payload, Mapping):
            raise TypeError("payload must be a mapping")
        raw_artifacts = value.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise TypeError("artifacts must be a list")
        artifacts: list[ArtifactRef] = []
        for index, raw_artifact in enumerate(raw_artifacts):
            if not isinstance(raw_artifact, Mapping):
                raise TypeError(f"artifacts[{index}] must be a mapping")
            artifacts.append(ArtifactRef.from_dict(raw_artifact))
        return cls(
            event_id=_nonempty(value.get("event_id"), "event_id"),
            event_type=EventType(_nonempty(value.get("event_type"), "event_type")),
            run_id=_nonempty(value.get("run_id"), "run_id"),
            occurred_at=_nonempty(value.get("occurred_at"), "occurred_at"),
            iteration_id=_optional_nonempty(value.get("iteration_id"), "iteration_id"),
            causation_id=_optional_nonempty(value.get("causation_id"), "causation_id"),
            entity_id=_optional_nonempty(value.get("entity_id"), "entity_id"),
            input_digest=_optional_nonempty(value.get("input_digest"), "input_digest"),
            payload=_json_mapping(raw_payload, "payload"),
            artifacts=tuple(artifacts),
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> OptimizationEvent:
        raw: object = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("optimization event JSON must be an object")
        return cls.from_dict(raw)


class EventLedgerCorruptionError(ValueError):
    def __init__(self, path: Path, line_number: int, message: str) -> None:
        self.path = path
        self.line_number = line_number
        super().__init__(f"{path}:{line_number}: {message}")


class EventLedger:
    """Durable append-only JSONL stream, separate from ExperimentLedger."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        create_parents: bool = True,
        fsync: bool = False,
    ) -> None:
        self.path = Path(path)
        self.fsync = fsync
        if create_parents:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: OptimizationEvent) -> OptimizationEvent:
        if not isinstance(event, OptimizationEvent):
            raise TypeError("event must be an OptimizationEvent")
        payload = (event.to_json() + "\n").encode("utf-8")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:  # pragma: no cover - os.write normally raises
                    raise OSError("short write while appending optimization event")
                written += count
            if self.fsync:
                os.fsync(fd)
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return event

    def iter_events(self) -> Iterator[OptimizationEvent]:
        if not self.path.exists():
            return
        if not self.path.is_file():
            raise IsADirectoryError(self.path)
        with self.path.open("r", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        yield OptimizationEvent.from_json(line)
                    except (TypeError, ValueError, KeyError) as exc:
                        raise EventLedgerCorruptionError(self.path, line_number, str(exc)) from exc
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> list[OptimizationEvent]:
        return list(self.iter_events())

    def by_run(self, run_id: str) -> list[OptimizationEvent]:
        selected_run_id = _nonempty(run_id, "run_id")
        return [event for event in self.iter_events() if event.run_id == selected_run_id]

    def by_iteration(self, run_id: str, iteration_id: str) -> list[OptimizationEvent]:
        selected_iteration_id = _nonempty(iteration_id, "iteration_id")
        return [
            event for event in self.by_run(run_id) if event.iteration_id == selected_iteration_id
        ]

    def latest(self, run_id: str | None = None) -> OptimizationEvent | None:
        latest: OptimizationEvent | None = None
        for event in self.iter_events():
            if run_id is None or event.run_id == run_id:
                latest = event
        return latest

    def __iter__(self) -> Iterator[OptimizationEvent]:
        return self.iter_events()

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_events())


__all__ = [
    "EventLedger",
    "EventLedgerCorruptionError",
    "EventType",
    "OptimizationEvent",
]
