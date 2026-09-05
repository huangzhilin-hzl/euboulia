"""Small, durable progress records shared by workers and the local control plane."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from euboulia.models import JSONValue
from euboulia.run_identity import normalize_run_uid

RUN_PHASES = (
    "submitted",
    "allocating",
    "pod_ready",
    "staging",
    "preparing_models",
    "preparing_sources",
    "capturing_provenance",
    "building",
    "launching",
    "waiting_ready",
    "profiling",
    "evaluating",
    "stopping",
    "syncing",
    "completed",
    "failed",
    "cancelled",
    "disconnected",
)

RUN_PROGRESS_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled", "disconnected"}
)


def progress_path(artifacts_root: str | os.PathLike[str], run_uid: str) -> Path:
    """Return the canonical worker progress path for one run."""

    return Path(artifacts_root).resolve() / normalize_run_uid(run_uid) / "progress.json"


def write_run_progress(
    artifacts_root: str | os.PathLike[str],
    run_uid: str,
    *,
    status: str,
    phase: str,
    detail: str | None = None,
    completed_units: int | None = None,
    total_units: int | None = None,
) -> Path:
    """Atomically expose bounded run progress without copying logs or profiler payloads."""

    selected_uid = normalize_run_uid(run_uid)
    if status not in RUN_PROGRESS_STATUSES:
        raise ValueError(f"unsupported run progress status: {status!r}")
    if phase not in RUN_PHASES:
        raise ValueError(f"unsupported run phase: {phase!r}")
    if detail is not None and (not isinstance(detail, str) or len(detail) > 512):
        raise ValueError("progress detail must be a string of at most 512 characters")
    if completed_units is not None and (
        isinstance(completed_units, bool)
        or not isinstance(completed_units, int)
        or completed_units < 0
    ):
        raise ValueError("completed_units must be a non-negative integer or None")
    if total_units is not None and (
        isinstance(total_units, bool)
        or not isinstance(total_units, int)
        or total_units <= 0
    ):
        raise ValueError("total_units must be a positive integer or None")
    if completed_units is not None and total_units is not None and completed_units > total_units:
        raise ValueError("completed_units must not exceed total_units")

    destination = progress_path(artifacts_root, selected_uid)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload: dict[str, JSONValue] = {
        "schema_version": 1,
        "run_uid": selected_uid,
        "status": status,
        "phase": phase,
        "detail": detail,
        "completed_units": completed_units,
        "total_units": total_units,
        "updated_at": _utc_now(),
    }
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def read_run_progress(path: str | os.PathLike[str]) -> Mapping[str, JSONValue] | None:
    """Read one progress record, returning ``None`` for a missing file."""

    source = Path(path)
    try:
        raw: object = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"run progress must be a JSON object: {source}")
    if raw.get("schema_version") != 1:
        raise ValueError(f"unsupported run progress schema in {source}")
    run_uid = raw.get("run_uid")
    status = raw.get("status")
    phase = raw.get("phase")
    if not isinstance(run_uid, str):
        raise ValueError(f"run progress has no valid run_uid: {source}")
    normalize_run_uid(run_uid)
    if status not in RUN_PROGRESS_STATUSES or phase not in RUN_PHASES:
        raise ValueError(f"run progress has an invalid status or phase: {source}")
    return raw


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "RUN_PHASES",
    "RUN_PROGRESS_STATUSES",
    "progress_path",
    "read_run_progress",
    "write_run_progress",
]
