import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from euboulia.optimization import (
    MemoryConflictError,
    MemoryEntry,
    MemoryQuery,
    MemorySchemaError,
    OutcomeStatus,
    SQLiteMemoryStore,
)


def entry(
    memory_id: str,
    *,
    outcome: OutcomeStatus = OutcomeStatus.ACCEPTED,
    framework_revision: str = "vllm-a",
    created_at: str = "2026-08-31T00:00:00Z",
) -> MemoryEntry:
    return MemoryEntry(
        memory_id=memory_id,
        outcome_id=f"outcome-{memory_id}",
        run_id="run-1",
        iteration_id=f"iteration-{memory_id}",
        framework="vllm",
        framework_revision=framework_revision,
        hardware_fingerprint="h100-cuda-12",
        model_revision="model-a",
        workload_digest="workload-a",
        benchmark_policy_digest="policy-a",
        proposal_id=f"proposal-{memory_id}",
        outcome=outcome,
        summary=f"summary {memory_id}",
        patch_digest=f"patch-{memory_id}",
        relative_improvement=0.05,
        created_at=created_at,
        details={"source": "event-replay"},
    )


def test_record_is_idempotent_and_recall_is_context_scoped(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    accepted = entry("a", created_at="2026-08-31T00:00:01Z")
    rejected = entry("b", outcome=OutcomeStatus.REJECTED)
    other_revision = entry("c", framework_revision="vllm-b")

    assert store.record(accepted) == accepted
    store.record(accepted)
    store.record(rejected)
    store.record(other_revision)

    recalled = store.recall(
        MemoryQuery(
            framework="vllm",
            framework_revision="vllm-a",
            outcomes=(OutcomeStatus.ACCEPTED,),
            limit=10,
        )
    )
    assert recalled == (accepted,)
    assert store.get("a") == accepted
    assert len(store) == 3


def test_conflicting_replay_fails_closed(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    original = entry("a")
    store.record(original)

    with pytest.raises(MemoryConflictError):
        store.record(replace(original, summary="different immutable content"))


def test_rebuild_atomically_replaces_derived_index(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    store.record(entry("stale"))
    replacement = (entry("new-a"), entry("new-b", outcome=OutcomeStatus.REJECTED))

    count = store.rebuild(iter(replacement))

    assert count == 2
    assert store.get("stale") is None
    assert {item.memory_id for item in store.recall(MemoryQuery(limit=10))} == {
        item.memory_id for item in replacement
    }


def test_memory_entry_json_round_trip_and_nonfinite_rejection() -> None:
    original = entry("a")
    assert MemoryEntry.from_json(original.to_json()) == original

    with pytest.raises(ValueError, match="finite"):
        replace(original, relative_improvement=float("nan"))


def test_unknown_sqlite_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(MemorySchemaError, match="version 99"):
        SQLiteMemoryStore(path)
