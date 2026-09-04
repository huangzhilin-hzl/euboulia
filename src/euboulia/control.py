"""Persistent local control plane for submitted target-validation runs."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from euboulia.models import JSONValue
from euboulia.optimization.config import (
    dump_resolved_optimization_config,
    load_optimization_config,
    require_optimization_execution_lock,
)
from euboulia.optimization.contracts import utc_now
from euboulia.progress import RUN_PHASES, read_run_progress
from euboulia.remote import (
    HostRuntimeConfig,
    default_runtime_config_path,
    load_host_runtime_config,
)
from euboulia.run_identity import new_run_uid, normalize_run_name, normalize_run_uid

CONTROL_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled", "disconnected"}
)
INFRASTRUCTURE_STATES = frozenset(
    {"not_created", "pod_pending", "pod_running", "pod_retained", "pod_deleted", "pod_lost"}
)
ARTIFACT_STATES = frozenset({"pending", "syncing", "partial", "verified", "unavailable"})

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_CONTROL_HEADER = "X-Euboulia-Control"
_UNSET = object()


class ControlError(RuntimeError):
    """Raised when a submitted run cannot be controlled safely."""


@dataclass(frozen=True, slots=True)
class ControlRun:
    run_uid: str
    name: str | None
    recipe_name: str
    recipe_path: Path
    recipe_sha256: str
    executor: str
    node: str
    runtime_config: Path
    status: str
    phase: str
    infrastructure_state: str
    artifact_state: str
    detail: str | None
    completed_units: int | None
    total_units: int | None
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    pid: int | None
    exit_code: int | None
    passed: bool | None
    error: str | None
    cancel_requested: bool
    metadata: Mapping[str, JSONValue]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ControlRun:
        raw_metadata: object = json.loads(cast(str, row["metadata_json"]))
        if not isinstance(raw_metadata, dict):
            raise ControlError(f"run {row['run_uid']} has invalid metadata")
        passed = row["passed"]
        return cls(
            run_uid=cast(str, row["run_uid"]),
            name=cast(str | None, row["name"]),
            recipe_name=cast(str, row["recipe_name"]),
            recipe_path=Path(cast(str, row["recipe_path"])),
            recipe_sha256=cast(str, row["recipe_sha256"]),
            executor=cast(str, row["executor"]),
            node=cast(str, row["node"]),
            runtime_config=Path(cast(str, row["runtime_config"])),
            status=cast(str, row["status"]),
            phase=cast(str, row["phase"]),
            infrastructure_state=cast(str, row["infrastructure_state"]),
            artifact_state=cast(str, row["artifact_state"]),
            detail=cast(str | None, row["detail"]),
            completed_units=cast(int | None, row["completed_units"]),
            total_units=cast(int | None, row["total_units"]),
            submitted_at=cast(str, row["submitted_at"]),
            started_at=cast(str | None, row["started_at"]),
            finished_at=cast(str | None, row["finished_at"]),
            updated_at=cast(str, row["updated_at"]),
            pid=cast(int | None, row["pid"]),
            exit_code=cast(int | None, row["exit_code"]),
            passed=None if passed is None else bool(passed),
            error=cast(str | None, row["error"]),
            cancel_requested=bool(row["cancel_requested"]),
            metadata=cast(Mapping[str, JSONValue], raw_metadata),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "run_uid": self.run_uid,
            "name": self.name,
            "recipe_name": self.recipe_name,
            "recipe_path": str(self.recipe_path),
            "recipe_sha256": self.recipe_sha256,
            "executor": self.executor,
            "node": self.node,
            "runtime_config": str(self.runtime_config),
            "status": self.status,
            "phase": self.phase,
            "infrastructure_state": self.infrastructure_state,
            "artifact_state": self.artifact_state,
            "detail": self.detail,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "metadata": dict(self.metadata),
        }


class ControlStore:
    """SQLite task index; immutable run artifacts remain the canonical evidence."""

    SCHEMA_VERSION = 1
    SCHEMA_COLUMNS = (
        "run_uid",
        "name",
        "recipe_name",
        "recipe_path",
        "recipe_sha256",
        "executor",
        "node",
        "runtime_config",
        "status",
        "phase",
        "infrastructure_state",
        "artifact_state",
        "detail",
        "completed_units",
        "total_units",
        "submitted_at",
        "started_at",
        "finished_at",
        "updated_at",
        "pid",
        "exit_code",
        "passed",
        "error",
        "cancel_requested",
        "metadata_json",
    )

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = self.root / "control.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            current = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            columns = tuple(
                cast(str, row[1])
                for row in connection.execute("PRAGMA table_info(control_runs)").fetchall()
            )
            if current != self.SCHEMA_VERSION or (columns and columns != self.SCHEMA_COLUMNS):
                connection.execute("DROP TABLE IF EXISTS control_events")
                connection.execute("DROP TABLE IF EXISTS control_runs")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_runs (
                    run_uid TEXT PRIMARY KEY,
                    name TEXT,
                    recipe_name TEXT NOT NULL,
                    recipe_path TEXT NOT NULL,
                    recipe_sha256 TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    node TEXT NOT NULL,
                    runtime_config TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    infrastructure_state TEXT NOT NULL,
                    artifact_state TEXT NOT NULL,
                    detail TEXT,
                    completed_units INTEGER,
                    total_units INTEGER,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    pid INTEGER,
                    exit_code INTEGER,
                    passed INTEGER,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_uid TEXT NOT NULL REFERENCES control_runs(run_uid) ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    infrastructure_state TEXT NOT NULL,
                    artifact_state TEXT NOT NULL,
                    detail TEXT,
                    completed_units INTEGER,
                    total_units INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS control_runs_updated_idx "
                "ON control_runs(updated_at DESC, run_uid DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS control_events_run_idx "
                "ON control_events(run_uid, sequence)"
            )
            if current != self.SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self.path.chmod(0o600)

    def create(
        self,
        *,
        run_uid: str,
        name: str | None,
        recipe_name: str,
        recipe_path: Path,
        recipe_sha256: str,
        executor: str,
        node: str,
        runtime_config: Path,
        metadata: Mapping[str, JSONValue],
    ) -> ControlRun:
        selected_uid = normalize_run_uid(run_uid)
        now = utc_now()
        encoded_metadata = json.dumps(
            dict(metadata), ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO control_runs (
                    run_uid, name, recipe_name, recipe_path, recipe_sha256,
                    executor, node, runtime_config, status, phase,
                    infrastructure_state, artifact_state, detail,
                    completed_units, total_units, submitted_at, started_at,
                    finished_at, updated_at, pid, exit_code, passed, error,
                    cancel_requested, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'submitted',
                          'not_created', 'pending', ?, NULL, NULL, ?, NULL,
                          NULL, ?, NULL, NULL, NULL, NULL, 0, ?)
                """,
                (
                    selected_uid,
                    name,
                    recipe_name,
                    str(recipe_path),
                    recipe_sha256,
                    executor,
                    node,
                    str(runtime_config),
                    "validated immutable recipe lock",
                    now,
                    now,
                    encoded_metadata,
                ),
            )
            self._append_event(connection, selected_uid)
        created = self.get(selected_uid)
        if created is None:  # pragma: no cover - insert and read are atomic for this process
            raise ControlError(f"submitted run disappeared: {selected_uid}")
        return created

    def get(self, run_uid: str) -> ControlRun | None:
        selected_uid = normalize_run_uid(run_uid)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control_runs WHERE run_uid = ?", (selected_uid,)
            ).fetchone()
        return None if row is None else ControlRun.from_row(row)

    def list(
        self,
        *,
        limit: int = 200,
        statuses: frozenset[str] | None = None,
    ) -> tuple[ControlRun, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        parameters: list[object] = []
        where = ""
        if statuses:
            invalid = statuses - CONTROL_STATUSES
            if invalid:
                raise ValueError(f"unknown control status: {sorted(invalid)[0]}")
            placeholders = ", ".join("?" for _ in statuses)
            where = f" WHERE status IN ({placeholders})"
            parameters.extend(sorted(statuses))
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_runs"
                + where
                + " ORDER BY submitted_at DESC, run_uid DESC LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(ControlRun.from_row(row) for row in rows)

    def claim_next(self) -> ControlRun | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_uid FROM control_runs WHERE status = 'queued' "
                "ORDER BY submitted_at, run_uid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run_uid = cast(str, row["run_uid"])
            now = utc_now()
            connection.execute(
                "UPDATE control_runs SET status = 'running', phase = 'allocating', "
                "detail = ?, started_at = ?, updated_at = ? WHERE run_uid = ?",
                ("starting the local run controller", now, now, run_uid),
            )
            self._append_event(connection, run_uid)
            claimed = connection.execute(
                "SELECT * FROM control_runs WHERE run_uid = ?", (run_uid,)
            ).fetchone()
        if claimed is None:  # pragma: no cover - row remains in the same transaction
            raise ControlError(f"claimed run disappeared: {run_uid}")
        return ControlRun.from_row(claimed)

    def update(
        self,
        run_uid: str,
        *,
        status: object = _UNSET,
        phase: object = _UNSET,
        infrastructure_state: object = _UNSET,
        artifact_state: object = _UNSET,
        detail: object = _UNSET,
        completed_units: object = _UNSET,
        total_units: object = _UNSET,
        pid: object = _UNSET,
        exit_code: object = _UNSET,
        passed: object = _UNSET,
        error: object = _UNSET,
        cancel_requested: object = _UNSET,
        finished_at: object = _UNSET,
    ) -> ControlRun:
        selected_uid = normalize_run_uid(run_uid)
        changes = {
            "status": status,
            "phase": phase,
            "infrastructure_state": infrastructure_state,
            "artifact_state": artifact_state,
            "detail": detail,
            "completed_units": completed_units,
            "total_units": total_units,
            "pid": pid,
            "exit_code": exit_code,
            "passed": passed,
            "error": error,
            "cancel_requested": cancel_requested,
            "finished_at": finished_at,
        }
        selected = {key: value for key, value in changes.items() if value is not _UNSET}
        if not selected:
            current = self.get(selected_uid)
            if current is None:
                raise ControlError(f"unknown run: {selected_uid}")
            return current
        self._validate_update(selected)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM control_runs WHERE run_uid = ?", (selected_uid,)
            ).fetchone()
            if current_row is None:
                raise ControlError(f"unknown run: {selected_uid}")
            current = ControlRun.from_row(current_row)
            self._validate_transition(
                current.status,
                cast(str, selected.get("status", current.status)),
            )
            assignments: list[str] = []
            parameters: list[object] = []
            for key, value in selected.items():
                normalized = (
                    int(cast(bool, value))
                    if key in {"passed", "cancel_requested"} and value is not None
                    else value
                )
                if current_row[key] == normalized:
                    continue
                assignments.append(f"{key} = ?")
                parameters.append(normalized)
            if assignments:
                assignments.append("updated_at = ?")
                parameters.extend((now, selected_uid))
                connection.execute(
                    f"UPDATE control_runs SET {', '.join(assignments)} WHERE run_uid = ?",
                    parameters,
                )
                self._append_event(connection, selected_uid)
            updated_row = connection.execute(
                "SELECT * FROM control_runs WHERE run_uid = ?", (selected_uid,)
            ).fetchone()
        if updated_row is None:  # pragma: no cover - row remains in the same transaction
            raise ControlError(f"updated run disappeared: {selected_uid}")
        return ControlRun.from_row(updated_row)

    def events(self, run_uid: str, *, limit: int = 500) -> tuple[dict[str, JSONValue], ...]:
        selected_uid = normalize_run_uid(run_uid)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 5000:
            raise ValueError("limit must be an integer from 1 to 5000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_events WHERE run_uid = ? "
                "ORDER BY sequence DESC LIMIT ?",
                (selected_uid, limit),
            ).fetchall()
        return tuple(
            {
                "sequence": cast(int, row["sequence"]),
                "run_uid": cast(str, row["run_uid"]),
                "occurred_at": cast(str, row["occurred_at"]),
                "status": cast(str, row["status"]),
                "phase": cast(str, row["phase"]),
                "infrastructure_state": cast(str, row["infrastructure_state"]),
                "artifact_state": cast(str, row["artifact_state"]),
                "detail": cast(str | None, row["detail"]),
                "completed_units": cast(int | None, row["completed_units"]),
                "total_units": cast(int | None, row["total_units"]),
            }
            for row in reversed(rows)
        )

    def _append_event(self, connection: sqlite3.Connection, run_uid: str) -> None:
        connection.execute(
            """
            INSERT INTO control_events (
                run_uid, occurred_at, status, phase, infrastructure_state,
                artifact_state, detail, completed_units, total_units
            )
            SELECT run_uid, updated_at, status, phase, infrastructure_state,
                   artifact_state, detail, completed_units, total_units
            FROM control_runs WHERE run_uid = ?
            """,
            (run_uid,),
        )

    @staticmethod
    def _validate_update(changes: Mapping[str, object]) -> None:
        status = changes.get("status")
        phase = changes.get("phase")
        infrastructure = changes.get("infrastructure_state")
        artifact = changes.get("artifact_state")
        if status is not None and status not in CONTROL_STATUSES:
            raise ValueError(f"unsupported control status: {status!r}")
        if phase is not None and phase not in RUN_PHASES:
            raise ValueError(f"unsupported run phase: {phase!r}")
        if infrastructure is not None and infrastructure not in INFRASTRUCTURE_STATES:
            raise ValueError(f"unsupported infrastructure state: {infrastructure!r}")
        if artifact is not None and artifact not in ARTIFACT_STATES:
            raise ValueError(f"unsupported artifact state: {artifact!r}")
        for key in ("completed_units", "total_units", "pid", "exit_code"):
            value = changes.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{key} must be an integer or None")
        for key in ("passed", "cancel_requested"):
            value = changes.get(key)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{key} must be a boolean or None")
        for key in ("detail", "error", "finished_at"):
            value = changes.get(key)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{key} must be a string or None")

    @staticmethod
    def _validate_transition(current: str, target: str) -> None:
        allowed = {
            "queued": {"queued", "running", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled", "disconnected"},
            "disconnected": {"disconnected", "running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if target not in allowed[current]:
            raise ControlError(f"illegal control transition: {current} -> {target}")


class TaskManager:
    """Submit and reconcile independent local controller processes."""

    def __init__(
        self,
        runtime_config: str | os.PathLike[str] | None = None,
        *,
        max_parallel: int = 2,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel <= 0:
            raise ValueError("max_parallel must be a positive integer")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.runtime_config_path = (
            default_runtime_config_path()
            if runtime_config is None
            else Path(runtime_config).expanduser().resolve()
        )
        self.runtime: HostRuntimeConfig = load_host_runtime_config(self.runtime_config_path)
        self.store = ControlStore(self.runtime.storage.root)
        self.max_parallel = max_parallel
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._children_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="euboulia-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop scheduling only; independently running controllers are not terminated."""

        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_seconds * 2))

    def submit(
        self,
        *,
        recipe: str | os.PathLike[str],
        executor: str,
        node: str,
        name: str | None = None,
    ) -> ControlRun:
        selected_node = _safe_text(node, "node", maximum=512)
        selected_executor = _safe_text(executor, "executor", maximum=128)
        selected_name = normalize_run_name(name)
        self.runtime.executor(selected_executor)
        source = Path(recipe).expanduser().resolve()
        config = load_optimization_config(source)
        require_optimization_execution_lock(config)
        run_uid = new_run_uid()
        submission_dir = self.runtime.storage.root / "submissions" / run_uid
        submission_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        lock_path = submission_dir / "recipe.lock.yaml"
        _write_private_text(
            lock_path,
            dump_resolved_optimization_config(config, destination=lock_path),
        )
        recipe_sha256 = _sha256(lock_path)
        hardware: dict[str, JSONValue] = {}
        if config.target is not None:
            hardware = dict(config.target.hardware)
        metadata: dict[str, JSONValue] = {
            "framework": config.framework.value,
            "model": config.models.target.name,
            "model_revision": config.models.target.revision,
            "source_revision": config.baseline.source_revision,
            "hardware": hardware,
            "workload_points": len(config.workload_suite.points),
            "profile_provider": config.optimization.profiling.provider.value,
        }
        try:
            created = self.store.create(
                run_uid=run_uid,
                name=selected_name,
                recipe_name=config.name,
                recipe_path=lock_path,
                recipe_sha256=recipe_sha256,
                executor=selected_executor,
                node=selected_node,
                runtime_config=self.runtime_config_path,
                metadata=metadata,
            )
        except BaseException:
            lock_path.unlink(missing_ok=True)
            submission_dir.rmdir()
            raise
        self._wake.set()
        return created

    def cancel(self, run_uid: str) -> ControlRun:
        selected_uid = normalize_run_uid(run_uid)
        task = self.store.get(selected_uid)
        if task is None:
            raise ControlError(f"unknown run: {selected_uid}")
        if task.status == "queued":
            return self.store.update(
                selected_uid,
                status="cancelled",
                phase="cancelled",
                artifact_state="unavailable",
                detail="cancelled before execution",
                cancel_requested=True,
                finished_at=utc_now(),
            )
        if task.status not in {"running", "disconnected"}:
            raise ControlError(f"run {selected_uid} is already {task.status}")
        task = self.store.update(
            selected_uid,
            detail="cancellation requested; owned Pod will be retained for recovery",
            cancel_requested=True,
        )
        if task.pid is None or not self._signal_owned_controller(task):
            raise ControlError(
                "the owning controller process cannot be verified; no signal was sent"
            )
        self._wake.set()
        return task

    def reconcile(self) -> None:
        for task in self.store.list(limit=1000, statuses=frozenset({"running", "disconnected"})):
            self._reconcile_task(task)
        for task in self.store.list(limit=1000, statuses=_TERMINAL_STATUSES):
            self._reconcile_terminal_state(task)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
                running = self.store.list(
                    limit=1000, statuses=frozenset({"running", "disconnected"})
                )
                live = sum(self._pid_alive(run.pid) for run in running)
                capacity = max(0, self.max_parallel - live)
                for _ in range(capacity):
                    claimed = self.store.claim_next()
                    if claimed is None:
                        break
                    self._spawn(claimed)
            except Exception:
                # One malformed or externally modified record must not kill the local control plane.
                pass
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()

    def _spawn(self, task: ControlRun) -> None:
        argv = [
            sys.executable,
            "-m",
            "euboulia",
            "target",
            "run",
            "--recipe",
            str(task.recipe_path),
            "--executor",
            task.executor,
            "--node",
            task.node,
            "--runtime-config",
            str(task.runtime_config),
            "--controller-run-uid",
            task.run_uid,
            "--json",
        ]
        if task.name is not None:
            argv.extend(("--name", task.name))
        log_dir = self.runtime.storage.root / "controller-logs"
        log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        log_path = log_dir / f"{task.run_uid}.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        executor = self.runtime.executor(task.executor)
        cwd = executor.local_project_dir
        try:
            with os.fdopen(descriptor, "ab") as log:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    shell=False,
                )
        except OSError as exc:
            self.store.update(
                task.run_uid,
                status="failed",
                phase="failed",
                artifact_state="unavailable",
                detail="local controller failed to start",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
            return
        with self._children_lock:
            self._children[task.run_uid] = process
        self.store.update(
            task.run_uid,
            pid=process.pid,
            detail="local controller started",
        )

    def _reconcile_task(self, task: ControlRun) -> None:
        run_dir = self.runtime.storage.runs_dir / task.run_uid
        record = _read_json(run_dir / "run.json")
        progress = _read_progress_safely(run_dir / "worker-progress.json")
        changes: dict[str, object] = {}
        if progress is not None:
            changes.update(
                {
                    "phase": progress.get("phase"),
                    "detail": progress.get("detail"),
                    "completed_units": progress.get("completed_units"),
                    "total_units": progress.get("total_units"),
                }
            )
        if record is not None:
            phase = record.get("phase")
            if (
                isinstance(phase, str)
                and phase in RUN_PHASES
                and (progress is None or phase in {"syncing", "completed", "failed", "cancelled"})
            ):
                changes["phase"] = phase
            infrastructure = _infrastructure_state(record)
            if infrastructure is not None:
                changes["infrastructure_state"] = infrastructure
            artifact = record.get("artifact_state")
            if isinstance(artifact, str) and artifact in ARTIFACT_STATES:
                changes["artifact_state"] = artifact
            status = record.get("status")
            finished_at = record.get("finished_at")
            if status in _TERMINAL_STATUSES and isinstance(finished_at, str):
                changes.update(
                    {
                        "status": status,
                        "phase": status,
                        "finished_at": finished_at,
                        "passed": (
                            record.get("passed")
                            if isinstance(record.get("passed"), bool)
                            else None
                        ),
                        "error": (
                            record.get("error")
                            if isinstance(record.get("error"), str)
                            else None
                        ),
                    }
                )
                manifest = _read_json(run_dir / "artifact-manifest.json")
                if manifest is not None:
                    changes["artifact_state"] = (
                        "verified" if manifest.get("verified") is not False else "partial"
                    )
        exit_code = self._child_exit_code(task.run_uid, task.pid)
        if exit_code is not None:
            changes["exit_code"] = exit_code
            if changes.get("status") not in _TERMINAL_STATUSES:
                if task.cancel_requested:
                    changes.update(
                        {
                            "status": "cancelled",
                            "phase": "cancelled",
                            "finished_at": utc_now(),
                            "detail": "controller stopped after cancellation request",
                        }
                    )
                elif record is None:
                    changes.update(
                        {
                            "status": "failed",
                            "phase": "failed",
                            "finished_at": utc_now(),
                            "error": f"local controller exited with status {exit_code}",
                        }
                    )
                else:
                    changes.update(
                        {
                            "status": "disconnected",
                            "phase": "disconnected",
                            "detail": "controller exited before a terminal run record was written",
                        }
                    )
        elif task.status == "disconnected" and self._pid_alive(task.pid):
            changes["status"] = "running"
        if changes:
            self.store.update(task.run_uid, **changes)

    def _reconcile_terminal_state(self, task: ControlRun) -> None:
        """Refresh mutable infrastructure state without reopening a terminal task."""

        record = _read_json(self.runtime.storage.runs_dir / task.run_uid / "run.json")
        if record is None:
            return
        infrastructure = _infrastructure_state(record)
        if infrastructure is None or infrastructure == task.infrastructure_state:
            return
        changes: dict[str, object] = {"infrastructure_state": infrastructure}
        if record.get("cleanup") == "deleted":
            changes["detail"] = "owned Pod deleted after terminal run"
        self.store.update(task.run_uid, **changes)

    def _child_exit_code(self, run_uid: str, pid: int | None) -> int | None:
        with self._children_lock:
            process = self._children.get(run_uid)
            if process is not None:
                code = process.poll()
                if code is not None:
                    self._children.pop(run_uid, None)
                return code
        return None if self._pid_alive(pid) else -1

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_owned_controller(task: ControlRun) -> bool:
        if task.pid is None or task.pid <= 0:
            return False
        inspected = subprocess.run(
            ["ps", "-p", str(task.pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
        )
        command = inspected.stdout.strip()
        if inspected.returncode != 0 or task.run_uid not in command:
            return False
        if "euboulia target run" not in command and "euboulia target run" not in command.replace(
            "  ", " "
        ):
            return False
        try:
            process_group = os.getpgid(task.pid)
        except ProcessLookupError:
            return False
        if process_group != task.pid:
            return False
        os.killpg(process_group, signal.SIGINT)
        return True


def controller_log_path(root: Path, run_uid: str) -> Path:
    return root / "controller-logs" / f"{normalize_run_uid(run_uid)}.log"


def read_text_tail(path: Path, *, max_bytes: int = 128 * 1024) -> str:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except FileNotFoundError:
        return ""
    return data.decode("utf-8", errors="replace")


def read_memory_entries(path: Path, *, limit: int = 200) -> tuple[Mapping[str, JSONValue], ...]:
    if not path.is_file():
        return ()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_entries'"
        ).fetchone()
        if table is None:
            return ()
        rows = connection.execute(
            "SELECT entry_json FROM memory_entries ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    entries: list[Mapping[str, JSONValue]] = []
    for row in rows:
        loaded: object = json.loads(cast(str, row[0]))
        if isinstance(loaded, dict):
            entries.append(cast(Mapping[str, JSONValue], loaded))
    return tuple(entries)


def _read_json(path: Path) -> Mapping[str, JSONValue] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return cast(Mapping[str, JSONValue], loaded) if isinstance(loaded, dict) else None


def _read_progress_safely(path: Path) -> Mapping[str, JSONValue] | None:
    try:
        return read_run_progress(path)
    except (OSError, ValueError):
        return None


def _infrastructure_state(record: Mapping[str, JSONValue]) -> str | None:
    cleanup = record.get("cleanup")
    if cleanup == "deleted":
        return "pod_deleted"
    explicit = record.get("infrastructure_state")
    if isinstance(explicit, str) and explicit in INFRASTRUCTURE_STATES:
        return explicit
    if cleanup in {"retained", "retained_for_on_demand_artifacts"}:
        return "pod_retained" if record.get("status") in _TERMINAL_STATUSES else "pod_running"
    if record.get("pod") is not None:
        return "pod_pending"
    return "not_created"


def _safe_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    selected = value.strip()
    if (
        selected.startswith("-")
        or "\x00" in selected
        or any(character.isspace() for character in selected)
        or len(selected) > maximum
    ):
        raise ValueError(f"{name} is not a safe value")
    return selected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


__all__ = [
    "ARTIFACT_STATES",
    "CONTROL_STATUSES",
    "INFRASTRUCTURE_STATES",
    "ControlError",
    "ControlRun",
    "ControlStore",
    "TaskManager",
    "controller_log_path",
    "read_memory_entries",
    "read_text_tail",
]
