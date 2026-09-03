import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from euboulia.optimization import (
    MemoryConflictError,
    MemoryEntry,
    MemoryQuery,
    OutcomeStatus,
    SQLiteMemoryStore,
)


def entry(
    memory_id: str,
    *,
    outcome: OutcomeStatus = OutcomeStatus.ACCEPTED,
    framework_revision: str = "vllm-a",
    created_at: str = "2026-08-31T00:00:00Z",
    spec_digest: str = "spec-a",
    compatibility_digest: str = "compat-a",
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
        spec_digest=spec_digest,
        run_uid="run-uid-1",
        compatibility_digest=compatibility_digest,
        compatibility_facets={
            "framework": "vllm",
            "hardware": "h100",
            "parallelism": {"tp": 8, "ep": 8},
        },
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


def test_exact_and_compatible_recall_are_separate(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    exact = entry("exact")
    compatible = entry("compatible", spec_digest="spec-b")
    unrelated = entry("unrelated", spec_digest="spec-c", compatibility_digest="compat-b")
    for item in (exact, compatible, unrelated):
        store.record(item)

    assert store.recall(MemoryQuery(spec_digest="spec-a")) == (exact,)
    assert {
        item.memory_id
        for item in store.recall(MemoryQuery(compatibility_digest="compat-a"))
    } == {
        "exact",
        "compatible",
    }
    assert {
        item.memory_id
        for item in store.recall(
            MemoryQuery(
                compatibility_facets={"parallelism": {"tp": 8}},
                limit=10,
            )
        )
    } == {"exact", "compatible", "unrelated"}


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


def test_unknown_sqlite_schema_is_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    store = SQLiteMemoryStore(path)

    assert len(store) == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_v1_memory_is_discarded_instead_of_migrated(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    legacy = entry("legacy")
    legacy_payload = legacy.to_dict()
    for field in (
        "spec_digest",
        "run_uid",
        "compatibility_digest",
        "compatibility_facets",
    ):
        legacy_payload.pop(field)
    encoded = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE memory_entries (
                memory_id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL, iteration_id TEXT NOT NULL,
                framework TEXT NOT NULL, framework_revision TEXT NOT NULL,
                hardware_fingerprint TEXT NOT NULL, model_revision TEXT NOT NULL,
                workload_digest TEXT NOT NULL, benchmark_policy_digest TEXT NOT NULL,
                proposal_id TEXT NOT NULL, outcome TEXT NOT NULL, patch_digest TEXT,
                relative_improvement REAL, created_at TEXT NOT NULL, entry_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO memory_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                legacy.memory_id,
                legacy.outcome_id,
                legacy.run_id,
                legacy.iteration_id,
                legacy.framework,
                legacy.framework_revision,
                legacy.hardware_fingerprint,
                legacy.model_revision,
                legacy.workload_digest,
                legacy.benchmark_policy_digest,
                legacy.proposal_id,
                legacy.outcome.value,
                legacy.patch_digest,
                legacy.relative_improvement,
                legacy.created_at,
                encoded,
            ),
        )
        connection.execute("PRAGMA user_version = 1")

    store = SQLiteMemoryStore(path)
    assert store.get("legacy") is None
    assert len(store) == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
