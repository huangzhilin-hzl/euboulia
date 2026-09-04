"""Local supervision for target validation executed in a Kubernetes pod."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import IO, cast

import yaml

from euboulia.execution import CommandExecutor, ExecutionResult
from euboulia.models import JSONValue
from euboulia.optimization.config import (
    OptimizationConfig,
    OptimizationExecutionConfig,
)
from euboulia.optimization.contracts import utc_now
from euboulia.optimization.events import EventLedger, EventType, OptimizationEvent
from euboulia.optimization.memory import SQLiteMemoryStore
from euboulia.run_identity import new_run_uid, normalize_run_name, normalize_run_uid


class RemoteConfigError(ValueError):
    """Raised when the host-only runtime configuration is invalid."""


class RemoteExecutionError(RuntimeError):
    """Raised when a remote run cannot be supervised safely."""


@dataclass(frozen=True, slots=True)
class ArtifactSyncPolicy:
    raw_profiles: str = "on_demand"

    def __post_init__(self) -> None:
        if self.raw_profiles not in {"on_demand", "always"}:
            raise RemoteConfigError(
                "storage.sync.raw_profiles must be 'on_demand' or 'always'"
            )


@dataclass(frozen=True, slots=True)
class LocalStorageConfig:
    root: Path
    sync: ArtifactSyncPolicy = ArtifactSyncPolicy()

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        if root in {Path(root.anchor), Path.home().resolve()}:
            raise RemoteConfigError("storage.root must not be the filesystem root or home")
        object.__setattr__(self, "root", root)

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def memory_path(self) -> Path:
        return self.root / "memory.sqlite3"


@dataclass(frozen=True, slots=True)
class KubernetesExecutorConfig:
    name: str
    namespace: str
    pod: str
    project_dir: PurePosixPath
    scratch_dir: PurePosixPath
    container: str | None = None
    context: str | None = None
    python: str = "python3"
    kubectl: str = "kubectl"
    local_project_dir: Path | None = None

    def __post_init__(self) -> None:
        for field_name in ("name", "namespace", "pod", "python", "kubectl"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value.startswith("-"):
                raise RemoteConfigError(f"executor.{field_name} must be a safe non-empty string")
            object.__setattr__(self, field_name, value.strip())
        for field_name in ("container", "context"):
            value = getattr(self, field_name)
            if value is not None:
                if not value.strip() or value.startswith("-"):
                    raise RemoteConfigError(
                        f"executor.{field_name} must be a safe non-empty string"
                    )
                object.__setattr__(self, field_name, value.strip())
        for field_name in ("project_dir", "scratch_dir"):
            value = PurePosixPath(getattr(self, field_name))
            if not value.is_absolute() or ".." in value.parts:
                raise RemoteConfigError(f"executor.{field_name} must be an absolute Pod path")
            object.__setattr__(self, field_name, value)
        if self.local_project_dir is not None:
            object.__setattr__(
                self,
                "local_project_dir",
                self.local_project_dir.expanduser().resolve(),
            )


@dataclass(frozen=True, slots=True)
class HostRuntimeConfig:
    storage: LocalStorageConfig
    executors: Mapping[str, KubernetesExecutorConfig]

    def executor(self, name: str) -> KubernetesExecutorConfig:
        try:
            return self.executors[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.executors)) or "none"
            raise RemoteConfigError(
                f"unknown executor {name!r}; configured executors: {available}"
            ) from exc


@dataclass(frozen=True, slots=True)
class SyncSummary:
    local_dir: Path
    remote_dir: PurePosixPath
    added: int
    modified: int = 0
    deleted: int = 0
    preserved_destination_only: int = 0

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "local_dir": str(self.local_dir),
            "remote_dir": str(self.remote_dir),
            "added": self.added,
            "modified": self.modified,
            "deleted": self.deleted,
            "preserved_destination_only": self.preserved_destination_only,
        }


@dataclass(frozen=True, slots=True)
class RemoteTargetRunResult:
    name: str | None
    run_uid: str
    status: str
    passed: bool
    executor: str
    local_run_dir: Path
    remote_run_dir: PurePosixPath
    summary_path: Path
    artifact_manifest_path: Path
    events_path: Path
    memory_path: Path
    sync: SyncSummary | None
    error: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "run_uid": self.run_uid,
            "status": self.status,
            "passed": self.passed,
            "executor": self.executor,
            "local_run_dir": str(self.local_run_dir),
            "remote_run_dir": str(self.remote_run_dir),
            "summary_path": str(self.summary_path),
            "artifact_manifest_path": str(self.artifact_manifest_path),
            "events_path": str(self.events_path),
            "memory_path": str(self.memory_path),
            "sync": None if self.sync is None else self.sync.to_dict(),
            "error": self.error,
        }


def default_runtime_config_path() -> Path:
    return Path.home() / ".config" / "euboulia" / "config.yaml"


def load_host_runtime_config(path: str | Path | None = None) -> HostRuntimeConfig:
    """Load machine-specific executors and canonical local storage."""

    source = default_runtime_config_path() if path is None else Path(path).expanduser()
    source = source.resolve()
    try:
        loaded: object = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RemoteConfigError(f"runtime config does not exist: {source}") from exc
    raw = _mapping(loaded, "runtime config")
    _reject_unknown(raw, {"storage", "executors"}, "runtime config")
    storage = _parse_storage(raw.get("storage"), source)
    executors_raw = _mapping(raw.get("executors"), "executors")
    executors: dict[str, KubernetesExecutorConfig] = {}
    for name, value in executors_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise RemoteConfigError("executor names must be non-empty strings")
        executors[name] = _parse_executor(name, value, source)
    return HostRuntimeConfig(storage=storage, executors=executors)


def with_worker_storage(
    config: OptimizationConfig,
    *,
    artifacts_root: Path,
    workspace_root: Path,
) -> OptimizationConfig:
    """Bind host-independent recipe storage to paths visible inside one worker."""

    artifacts = artifacts_root.resolve()
    execution = OptimizationExecutionConfig(
        artifacts_dir=artifacts,
        experiment_ledger=artifacts / "experiments.jsonl",
        event_ledger=artifacts / "events.jsonl",
        memory=artifacts / "memory.sqlite3",
    )
    workspace = config.optimization.workspace
    optimization = config.optimization
    if workspace is not None:
        optimization = replace(
            optimization,
            workspace=replace(workspace, root_dir=workspace_root.resolve()),
        )
    return replace(config, execution=execution, optimization=optimization)


class KubernetesTargetSupervisor:
    """Run one target worker in a Pod and retain canonical records locally."""

    def __init__(
        self,
        executor: KubernetesExecutorConfig,
        storage: LocalStorageConfig,
    ) -> None:
        self.executor = executor
        self.storage = storage

    def run(
        self,
        recipe: Path,
        *,
        values: Path | None,
        name: str | None,
    ) -> RemoteTargetRunResult:
        selected_name = normalize_run_name(name)
        run_uid = new_run_uid()
        local_run_dir = self.storage.runs_dir / run_uid
        local_run_dir.mkdir(parents=True, exist_ok=False)
        SQLiteMemoryStore(self.storage.memory_path)
        events_path = local_run_dir / "events.jsonl"
        ledger = EventLedger(events_path, fsync=True)
        remote_runs_root = self.executor.scratch_dir / "runs"
        remote_run_dir = remote_runs_root / run_uid
        remote_workspace_root = self.executor.scratch_dir / "worktrees"
        summary_path = local_run_dir / "summary.json"
        manifest_path = local_run_dir / "artifact-manifest.json"
        started_at = utc_now()
        base_record: dict[str, JSONValue] = {
            "schema_version": 1,
            "name": selected_name,
            "run_uid": run_uid,
            "executor": self.executor.name,
            "executor_type": "kubernetes",
            "namespace": self.executor.namespace,
            "pod": self.executor.pod,
            "container": self.executor.container,
            "remote_run_dir": str(remote_run_dir),
            "started_at": started_at,
            "status": "running",
        }
        _write_json(local_run_dir / "run.json", base_record)
        ledger.append(
            OptimizationEvent.create(
                EventType.RUN_STARTED,
                run_uid,
                payload={
                    "name": selected_name,
                    "executor": self.executor.name,
                    "namespace": self.executor.namespace,
                    "pod": self.executor.pod,
                    "remote_run_dir": str(remote_run_dir),
                },
            )
        )

        worker: ExecutionResult | None = None
        sync: SyncSummary | None = None
        worker_payload: Mapping[str, JSONValue] | None = None
        error: str | None = None
        try:
            worker = self._run_worker(
                recipe.resolve(),
                values=None if values is None else values.resolve(),
                name=selected_name,
                run_uid=run_uid,
                remote_runs_root=remote_runs_root,
                remote_workspace_root=remote_workspace_root,
                local_run_dir=local_run_dir,
            )
            if (
                worker.error is None
                and not worker.timed_out
                and worker.returncode in {0, 1}
            ):
                worker_payload = _read_last_json_object(worker.stdout_path)
                if worker_payload.get("run_uid") != run_uid:
                    raise RemoteExecutionError("remote worker returned a different run_uid")
                expected_returncode = 0 if worker_payload.get("passed") is True else 1
                if worker.returncode != expected_returncode:
                    raise RemoteExecutionError(
                        "remote worker result disagrees with its exit status"
                    )
            else:
                error = worker.error or f"remote worker exited with status {worker.returncode}"
        except (OSError, RemoteExecutionError, ValueError) as exc:
            error = str(exc)
        finally:
            try:
                sync = self._pull_artifacts(remote_run_dir, local_run_dir / "artifacts")
            except (OSError, RemoteExecutionError, tarfile.TarError) as exc:
                sync_error = f"artifact sync failed: {exc}"
                error = sync_error if error is None else f"{error}; {sync_error}"

        manifest_error: str | None = None
        if sync is None:
            manifest_error = "artifact verification skipped because artifact sync failed"
        else:
            try:
                _write_local_artifact_manifest(
                    local_run_dir / "artifacts",
                    manifest_path,
                    executor=self.executor,
                    remote_run_dir=remote_run_dir,
                    run_uid=run_uid,
                )
            except (OSError, RemoteExecutionError, ValueError) as exc:
                manifest_error = f"artifact verification failed: {exc}"
                error = manifest_error if error is None else f"{error}; {manifest_error}"
        if manifest_error is not None:
            _write_json(
                manifest_path,
                {
                    "schema_version": 1,
                    "run_uid": run_uid,
                    "executor": self.executor.name,
                    "verified": False,
                    "remote_uri": _kubernetes_uri(self.executor, remote_run_dir),
                    "artifacts": [],
                    "error": manifest_error,
                },
            )

        completed = worker_payload is not None and error is None and sync is not None
        passed = completed and worker_payload is not None and worker_payload.get("passed") is True
        status = "completed" if completed else "failed"
        finished_at = utc_now()
        summary: dict[str, JSONValue] = {
            **base_record,
            "status": status,
            "passed": passed,
            "finished_at": finished_at,
            "worker": None if worker is None else cast(JSONValue, worker.to_dict()),
            "result": None if worker_payload is None else dict(worker_payload),
            "sync": None if sync is None else sync.to_dict(),
            "error": error,
        }
        _write_json(summary_path, summary)
        _write_json(local_run_dir / "run.json", summary)
        ledger.append(
            OptimizationEvent.create(
                EventType.RUN_COMPLETED if completed else EventType.RUN_FAILED,
                run_uid,
                payload={
                    "status": status,
                    "passed": passed,
                    "summary_path": str(summary_path),
                    "artifact_manifest_path": str(manifest_path),
                    "error": error,
                },
            )
        )
        return RemoteTargetRunResult(
            name=selected_name,
            run_uid=run_uid,
            status=status,
            passed=passed,
            executor=self.executor.name,
            local_run_dir=local_run_dir,
            remote_run_dir=remote_run_dir,
            summary_path=summary_path,
            artifact_manifest_path=manifest_path,
            events_path=events_path,
            memory_path=self.storage.memory_path,
            sync=sync,
            error=error,
        )

    def pull_snapshot(self, run_uid: str, destination: Path) -> SyncSummary:
        """Pull a complete immutable snapshot, including raw profiles, on demand."""

        selected_uid = normalize_run_uid(run_uid)
        remote_run_dir = self.executor.scratch_dir / "runs" / selected_uid
        return self._pull_artifacts(
            remote_run_dir,
            destination.expanduser().resolve(),
            include_raw_profiles=True,
        )

    def _run_worker(
        self,
        recipe: Path,
        *,
        values: Path | None,
        name: str | None,
        run_uid: str,
        remote_runs_root: PurePosixPath,
        remote_workspace_root: PurePosixPath,
        local_run_dir: Path,
    ) -> ExecutionResult:
        remote_recipe = self._remote_project_path(recipe)
        argv = [
            *self._kubectl_exec_prefix(),
            "/usr/bin/env",
            f"PYTHONPATH={self.executor.project_dir / 'src'}",
            self.executor.python,
            "-m",
            "euboulia",
            "target",
            "run",
            "--recipe",
            str(remote_recipe),
            "--json",
            "--internal-run-uid",
            run_uid,
            "--internal-artifacts-root",
            str(remote_runs_root),
            "--internal-workspace-root",
            str(remote_workspace_root),
        ]
        if values is not None:
            argv.extend(("--values", str(self._remote_project_path(values))))
        if name is not None:
            argv.extend(("--name", name))
        return CommandExecutor(local_run_dir / "control").run(
            argv,
            artifact_prefix="pod-worker",
        )

    def _pull_artifacts(
        self,
        remote_run_dir: PurePosixPath,
        local_artifacts_dir: Path,
        *,
        include_raw_profiles: bool | None = None,
    ) -> SyncSummary:
        if local_artifacts_dir.exists() or local_artifacts_dir.is_symlink():
            raise RemoteExecutionError(
                f"local artifact snapshot already exists: {local_artifacts_dir}"
            )
        local_artifacts_dir.mkdir(parents=True)
        stderr_path = local_artifacts_dir.parent / "control" / "artifact-sync.stderr.log"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        tar_argv = [*self._kubectl_exec_prefix(), "tar"]
        include_raw = (
            self.storage.sync.raw_profiles == "always"
            if include_raw_profiles is None
            else include_raw_profiles
        )
        if not include_raw:
            tar_argv.extend(
                (
                    "--exclude=./target-validation/profile/raw",
                    "--exclude=./target-validation/profile/raw/*",
                )
            )
        tar_argv.extend(("-C", str(remote_run_dir), "-cf", "-", "."))
        added = 0
        with stderr_path.open("xb") as stderr_file:
            process = subprocess.Popen(
                tar_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                shell=False,
            )
            if process.stdout is None:  # pragma: no cover - PIPE guarantees this
                raise RemoteExecutionError("kubectl artifact stream has no stdout")
            try:
                added = _extract_safe_tar(process.stdout, local_artifacts_dir)
            except BaseException:
                process.kill()
                process.wait()
                raise
            returncode = process.wait()
        if returncode != 0:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RemoteExecutionError(
                f"kubectl artifact stream exited with status {returncode}: {detail}"
            )
        return SyncSummary(
            local_dir=local_artifacts_dir,
            remote_dir=remote_run_dir,
            added=added,
        )

    def _kubectl_exec_prefix(self) -> tuple[str, ...]:
        argv = [self.executor.kubectl]
        if self.executor.context is not None:
            argv.extend(("--context", self.executor.context))
        argv.extend(("--namespace", self.executor.namespace, "exec", self.executor.pod))
        if self.executor.container is not None:
            argv.extend(("--container", self.executor.container))
        argv.append("--")
        return tuple(argv)

    def _remote_project_path(self, path: Path) -> PurePosixPath:
        project = self.executor.local_project_dir or _discover_project_root(path)
        try:
            relative = path.resolve().relative_to(project)
        except ValueError as exc:
            raise RemoteConfigError(
                f"{path} is outside local project directory {project}"
            ) from exc
        return self.executor.project_dir.joinpath(*relative.parts)


def write_worker_artifact_index(run_dir: Path, run_uid: str) -> Path:
    """Index all worker files before selective transfer to the local controller."""

    normalized_uid = normalize_run_uid(run_uid)
    entries: list[dict[str, JSONValue]] = []
    if run_dir.is_dir():
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == "artifact-index.json":
                continue
            relative = path.relative_to(run_dir).as_posix()
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                    "retention": (
                        "remote_only" if _is_raw_profile_path(relative) else "local"
                    ),
                }
            )
    destination = run_dir / "artifact-index.json"
    _write_json(
        destination,
        {
            "schema_version": 1,
            "run_uid": normalized_uid,
            "artifacts": cast(JSONValue, entries),
        },
    )
    return destination


def _parse_storage(value: object, source: Path) -> LocalStorageConfig:
    raw = _mapping(value, "storage")
    _reject_unknown(raw, {"root", "sync"}, "storage")
    root = _local_path(raw.get("root"), "storage.root", source)
    sync_raw = _mapping(raw.get("sync", {}), "storage.sync")
    _reject_unknown(sync_raw, {"raw_profiles"}, "storage.sync")
    sync = ArtifactSyncPolicy(
        raw_profiles=_string(
            sync_raw.get("raw_profiles", "on_demand"),
            "storage.sync.raw_profiles",
        ),
    )
    return LocalStorageConfig(root=root, sync=sync)


def _parse_executor(
    name: str,
    value: object,
    source: Path,
) -> KubernetesExecutorConfig:
    raw = _mapping(value, f"executors.{name}")
    allowed = {
        "type",
        "namespace",
        "pod",
        "container",
        "context",
        "project_dir",
        "scratch_dir",
        "local_project_dir",
        "python",
        "kubectl",
    }
    _reject_unknown(raw, allowed, f"executors.{name}")
    kind = _string(raw.get("type"), f"executors.{name}.type")
    if kind != "kubernetes":
        raise RemoteConfigError(f"executors.{name}.type must be 'kubernetes'")
    local_project_value = raw.get("local_project_dir")
    return KubernetesExecutorConfig(
        name=name,
        namespace=_string(raw.get("namespace"), f"executors.{name}.namespace"),
        pod=_string(raw.get("pod"), f"executors.{name}.pod"),
        container=_optional_string(raw.get("container"), f"executors.{name}.container"),
        context=_optional_string(raw.get("context"), f"executors.{name}.context"),
        project_dir=PurePosixPath(
            _string(raw.get("project_dir"), f"executors.{name}.project_dir")
        ),
        scratch_dir=PurePosixPath(
            _string(raw.get("scratch_dir"), f"executors.{name}.scratch_dir")
        ),
        local_project_dir=(
            None
            if local_project_value is None
            else _local_path(
                local_project_value,
                f"executors.{name}.local_project_dir",
                source,
            )
        ),
        python=_string(raw.get("python", "python3"), f"executors.{name}.python"),
        kubectl=_string(raw.get("kubectl", "kubectl"), f"executors.{name}.kubectl"),
    )


def _extract_safe_tar(stream: IO[bytes], destination: Path) -> int:
    added = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            relative = _safe_relative_path(member.name, "Pod archive")
            normalized_parts = tuple(part for part in relative.parts if part not in {"", "."})
            if not normalized_parts:
                continue
            target = destination.joinpath(*normalized_parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RemoteExecutionError(
                    f"unsupported artifact type in Pod archive: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RemoteExecutionError(f"missing artifact body in Pod archive: {member.name}")
            with source, target.open("xb") as destination_file:
                shutil.copyfileobj(source, destination_file)
            added += 1
    return added


def _safe_relative_path(value: str, source: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RemoteExecutionError(f"unsafe artifact path in {source}: {value}")
    return relative


def _write_local_artifact_manifest(
    local_artifacts_dir: Path,
    destination: Path,
    *,
    executor: KubernetesExecutorConfig,
    remote_run_dir: PurePosixPath,
    run_uid: str,
) -> None:
    index_path = local_artifacts_dir / "artifact-index.json"
    if not index_path.is_file():
        raise RemoteExecutionError("artifact snapshot has no artifact-index.json")
    parsed = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RemoteExecutionError("artifact index must be a JSON object")
    normalized_uid = normalize_run_uid(run_uid)
    if parsed.get("run_uid") != normalized_uid:
        raise RemoteExecutionError("artifact index belongs to a different run_uid")
    raw_entries = parsed.get("artifacts")
    if not isinstance(raw_entries, list):
        raise RemoteExecutionError("artifact index artifacts must be a list")
    artifacts: list[dict[str, JSONValue]] = []
    indexed_paths: set[PurePosixPath] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("path"), str):
            raise RemoteExecutionError("artifact index entries must contain a path")
        relative = cast(str, raw_entry["path"])
        safe_relative = _safe_relative_path(relative, "artifact index")
        if safe_relative in indexed_paths:
            raise RemoteExecutionError(f"duplicate artifact index path: {relative}")
        indexed_paths.add(safe_relative)
        local_path = local_artifacts_dir.joinpath(*safe_relative.parts)
        synced = local_path.is_file()
        expected_sha256 = raw_entry.get("sha256")
        expected_size = raw_entry.get("size_bytes")
        retention = raw_entry.get("retention")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or retention not in {"local", "remote_only"}
        ):
            raise RemoteExecutionError(
                f"artifact index has invalid metadata for {relative}"
            )
        if retention == "local" and not synced:
            raise RemoteExecutionError(f"required artifact was not synchronized: {relative}")
        if synced:
            actual_size = local_path.stat().st_size
            actual_sha256 = _sha256(local_path)
            if actual_size != expected_size or actual_sha256 != expected_sha256:
                raise RemoteExecutionError(
                    f"artifact digest mismatch for {relative}"
                )
        artifacts.append(
            {
                "path": relative,
                "sha256": expected_sha256,
                "size_bytes": expected_size,
                "retention": cast(str, retention),
                "synced": synced,
                "local_path": str(local_path) if synced else None,
                "remote_uri": _kubernetes_uri(executor, remote_run_dir / safe_relative),
            }
        )
    actual_paths = {
        PurePosixPath(path.relative_to(local_artifacts_dir).as_posix())
        for path in local_artifacts_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path != index_path
    }
    unexpected_paths = actual_paths - indexed_paths
    if unexpected_paths:
        unexpected = ", ".join(path.as_posix() for path in sorted(unexpected_paths))
        raise RemoteExecutionError(f"artifact snapshot contains unindexed files: {unexpected}")
    _write_json(
        destination,
        {
            "schema_version": 1,
            "run_uid": normalized_uid,
            "executor": executor.name,
            "verified": True,
            "remote_uri": _kubernetes_uri(executor, remote_run_dir),
            "artifacts": cast(JSONValue, artifacts),
        },
    )


def _kubernetes_uri(
    executor: KubernetesExecutorConfig,
    path: PurePosixPath,
) -> str:
    container = "" if executor.container is None else f"/{executor.container}"
    return (
        f"kubernetes://{executor.namespace}/{executor.pod}{container}"
        f"{path.as_posix()}"
    )


def _read_last_json_object(path: Path) -> Mapping[str, JSONValue]:
    text = path.read_text(encoding="utf-8", errors="strict").strip()
    if not text:
        raise RemoteExecutionError("remote worker produced no JSON result")
    decoder = json.JSONDecoder()
    candidates = [text]
    candidates.extend(line for line in reversed(text.splitlines()) if line.strip())
    for candidate in candidates:
        try:
            value, end = decoder.raw_decode(candidate.lstrip())
        except json.JSONDecodeError:
            continue
        if candidate.lstrip()[end:].strip() or not isinstance(value, dict):
            continue
        return cast(dict[str, JSONValue], value)
    raise RemoteExecutionError("remote worker stdout did not contain a JSON object")


def _discover_project_root(path: Path) -> Path:
    selected = path.resolve()
    current = selected if selected.is_dir() else selected.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise RemoteConfigError(
        f"cannot discover local project root for {path}; configure local_project_dir"
    )


def _write_json(path: Path, value: Mapping[str, JSONValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_raw_profile_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(
        parts[index : index + 2] == ("profile", "raw")
        for index in range(max(0, len(parts) - 1))
    )


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RemoteConfigError(f"{path} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise RemoteConfigError(f"{path} keys must be strings")
    return cast(Mapping[str, object], value)


def _reject_unknown(raw: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RemoteConfigError(f"{path} contains unknown field(s): {', '.join(unknown)}")


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, path: str) -> str | None:
    return None if value is None else _string(value, path)


def _local_path(value: object, path: str, source: Path) -> Path:
    candidate = Path(_string(value, path)).expanduser()
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    return candidate.resolve()


__all__ = [
    "ArtifactSyncPolicy",
    "HostRuntimeConfig",
    "KubernetesExecutorConfig",
    "KubernetesTargetSupervisor",
    "LocalStorageConfig",
    "RemoteConfigError",
    "RemoteExecutionError",
    "RemoteTargetRunResult",
    "SyncSummary",
    "default_runtime_config_path",
    "load_host_runtime_config",
    "with_worker_storage",
    "write_worker_artifact_index",
]
