"""Rebuildable SQLite index over structured optimization outcomes."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from euboulia.optimization.contracts import MemoryEntry, MemoryQuery


class MemorySchemaError(RuntimeError):
    """Raised when an existing database has an unsupported schema."""


class MemoryConflictError(ValueError):
    """Raised when an immutable memory id is replayed with different content."""


class SQLiteMemoryStore:
    """A disposable query index whose canonical source is events/artifacts.

    ``record`` is idempotent for byte-equivalent entries. ``rebuild`` replaces
    the complete index in one transaction, making deletion of this database a
    recoverable operation when canonical outcomes remain available.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        create_parents: bool = True,
    ) -> None:
        self.path = Path(path)
        if create_parents:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            current = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, self.SCHEMA_VERSION}:
                raise MemorySchemaError(
                    f"unsupported memory schema version {current}; expected {self.SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    memory_id TEXT PRIMARY KEY,
                    outcome_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    iteration_id TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    framework_revision TEXT NOT NULL,
                    hardware_fingerprint TEXT NOT NULL,
                    model_revision TEXT NOT NULL,
                    workload_digest TEXT NOT NULL,
                    benchmark_policy_digest TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    patch_digest TEXT,
                    relative_improvement REAL,
                    created_at TEXT NOT NULL,
                    entry_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS memory_context_idx
                ON memory_entries (
                    framework,
                    framework_revision,
                    hardware_fingerprint,
                    model_revision,
                    workload_digest,
                    benchmark_policy_digest,
                    created_at DESC
                )
                """
            )
            if current == 0:
                connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def record(self, entry: MemoryEntry) -> MemoryEntry:
        if not isinstance(entry, MemoryEntry):
            raise TypeError("entry must be a MemoryEntry")
        encoded = entry.to_json()
        with self._connect() as connection:
            self._insert(connection, entry, encoded)
        return entry

    def _insert(
        self,
        connection: sqlite3.Connection,
        entry: MemoryEntry,
        encoded: str,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO memory_entries (
                memory_id,
                outcome_id,
                run_id,
                iteration_id,
                framework,
                framework_revision,
                hardware_fingerprint,
                model_revision,
                workload_digest,
                benchmark_policy_digest,
                proposal_id,
                outcome,
                patch_digest,
                relative_improvement,
                created_at,
                entry_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.memory_id,
                entry.outcome_id,
                entry.run_id,
                entry.iteration_id,
                entry.framework,
                entry.framework_revision,
                entry.hardware_fingerprint,
                entry.model_revision,
                entry.workload_digest,
                entry.benchmark_policy_digest,
                entry.proposal_id,
                entry.outcome.value,
                entry.patch_digest,
                entry.relative_improvement,
                entry.created_at,
                encoded,
            ),
        )
        if cursor.rowcount == 1:
            return
        row = connection.execute(
            "SELECT memory_id, entry_json FROM memory_entries "
            "WHERE memory_id = ? OR outcome_id = ?",
            (entry.memory_id, entry.outcome_id),
        ).fetchone()
        if row is None or cast(str, row[1]) != encoded:
            raise MemoryConflictError(
                f"memory id {entry.memory_id!r} or outcome id {entry.outcome_id!r} "
                "already indexes different content"
            )

    def get(self, memory_id: str) -> MemoryEntry | None:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id must be a non-empty string")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entry_json FROM memory_entries WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return None if row is None else MemoryEntry.from_json(cast(str, row[0]))

    def recall(self, query: MemoryQuery) -> tuple[MemoryEntry, ...]:
        if not isinstance(query, MemoryQuery):
            raise TypeError("query must be a MemoryQuery")
        clauses: list[str] = []
        parameters: list[object] = []
        for column in (
            "framework",
            "framework_revision",
            "hardware_fingerprint",
            "model_revision",
            "workload_digest",
            "benchmark_policy_digest",
        ):
            value = getattr(query, column)
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if query.outcomes:
            placeholders = ", ".join("?" for _ in query.outcomes)
            clauses.append(f"outcome IN ({placeholders})")
            parameters.extend(outcome.value for outcome in query.outcomes)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(query.limit)
        sql = (
            "SELECT entry_json FROM memory_entries"
            + where
            + " ORDER BY created_at DESC, memory_id DESC LIMIT ?"
        )
        with self._connect() as connection:
            raw_rows = connection.execute(sql, parameters).fetchall()
        rows = cast(list[tuple[str]], raw_rows)
        return tuple(MemoryEntry.from_json(row[0]) for row in rows)

    def rebuild(self, entries: Iterable[MemoryEntry]) -> int:
        if isinstance(entries, str | bytes):
            raise TypeError("entries must be an iterable of MemoryEntry values")
        normalized = tuple(entries)
        if not all(isinstance(entry, MemoryEntry) for entry in normalized):
            raise TypeError("entries must contain only MemoryEntry values")
        with self._connect() as connection:
            connection.execute("DELETE FROM memory_entries")
            for entry in normalized:
                self._insert(connection, entry, entry.to_json())
        return len(normalized)

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM memory_entries")

    def __len__(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM memory_entries").fetchone()
        if row is None:  # pragma: no cover - COUNT always returns one row
            return 0
        return cast(int, row[0])


__all__ = [
    "MemoryConflictError",
    "MemorySchemaError",
    "SQLiteMemoryStore",
]
