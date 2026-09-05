"""Isolated, evidence-preserving git workspaces for candidate patches.

The implementation intentionally has no automatic cleanup.  A rejected patch and a
failed command are evidence, so their detached worktree and artifacts remain available
until an operator explicitly removes them outside this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from euboulia.execution import DEFAULT_ENV_ALLOWLIST
from euboulia.optimization.config import SourceConfig


class WorkspaceError(RuntimeError):
    """Base error for workspace creation and command execution."""


class PatchRejected(WorkspaceError):
    """Raised when a patch violates policy or fails ``git apply --check``."""


class WorkspaceAuthorizationError(WorkspaceError):
    """Raised when a mutating patch application was not explicitly authorized."""


class SourcePreparationError(WorkspaceError):
    """Raised when an immutable declared source cannot be prepared."""


STAGED_SOURCE_REF = "refs/heads/euboulia-source"


@dataclass(frozen=True, slots=True)
class PreparedGitSource:
    """A reusable Git repository cache pinned to a declared source revision."""

    name: str
    repository: Path
    remote: str
    ref: str
    revision: str
    observed_ref_revision: str | None
    evidence_dir: Path


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """One exact-revision Git bundle prepared for controller-to-worker delivery."""

    name: str
    path: Path
    ref: str
    revision: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PatchLimits:
    """Fail-closed limits applied before git parses a patch."""

    max_bytes: int = 256 * 1024
    max_files: int = 20
    max_changed_lines: int = 2_000

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_files", "max_changed_lines"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    """Persisted outcome of one argv-based workspace command."""

    argv: tuple[str, ...]
    cwd: Path
    returncode: int | None
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "returncode": self.returncode,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "error": self.error,
            "succeeded": self.succeeded,
        }


@dataclass(frozen=True, slots=True)
class PatchInspection:
    """Static facts established before invoking ``git apply``."""

    sha256: str
    size_bytes: int
    files: tuple[str, ...]
    additions: int
    deletions: int

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "files": list(self.files),
            "file_count": len(self.files),
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_lines": self.changed_lines,
        }


@dataclass(frozen=True, slots=True)
class PreparedPatch:
    """An immutable patch artifact that passed policy and ``git apply --check``."""

    patch_id: str
    patch_path: Path
    workspace_path: Path
    inspection: PatchInspection
    check_evidence: CommandEvidence


@dataclass(frozen=True, slots=True)
class PatchApplication:
    """Evidence for an explicitly authorized patch application."""

    prepared: PreparedPatch
    apply_evidence: CommandEvidence
    diff_evidence: CommandEvidence

    @property
    def succeeded(self) -> bool:
        return self.apply_evidence.succeeded and self.diff_evidence.succeeded


class GitWorktreeWorkspace:
    """A detached git worktree dedicated to one optimization candidate."""

    def __init__(
        self,
        *,
        repository: Path,
        root: Path,
        path: Path,
        evidence_dir: Path,
        base_revision: str,
        base_commit: str,
        git_executable: str,
        default_timeout_seconds: float,
        creation_evidence: tuple[CommandEvidence, ...],
    ) -> None:
        self.repository = repository
        self.root = root
        self.path = path
        self.evidence_dir = evidence_dir
        self.base_revision = base_revision
        self.base_commit = base_commit
        self.git_executable = git_executable
        self.default_timeout_seconds = default_timeout_seconds
        self.creation_evidence = creation_evidence

    @classmethod
    def create(
        cls,
        repository: str | os.PathLike[str],
        root: str | os.PathLike[str],
        *,
        revision: str = "HEAD",
        git_executable: str = "git",
        timeout_seconds: float = 120.0,
    ) -> GitWorktreeWorkspace:
        """Create a detached worktree, retaining diagnostics even on failure."""

        repository_path = Path(repository).resolve()
        root_path = Path(root).resolve()
        evidence_dir = root_path / "evidence"
        worktree_path = root_path / "worktree"
        if not revision or revision.startswith("-") or "\x00" in revision:
            raise ValueError("revision must be a non-empty git revision without option syntax")
        _validate_executable(git_executable, "git_executable")
        timeout = _positive_timeout(timeout_seconds)
        if worktree_path.exists() or worktree_path.is_symlink():
            raise WorkspaceError(f"worktree path already exists: {worktree_path}")
        evidence_dir.mkdir(parents=True, exist_ok=True)

        evidence: list[CommandEvidence] = []
        top_level = _run_command(
            [git_executable, "-C", str(repository_path), "rev-parse", "--show-toplevel"],
            cwd=repository_path,
            evidence_dir=evidence_dir,
            label="repository-root",
            timeout_seconds=timeout,
        )
        evidence.append(top_level)
        if not top_level.succeeded:
            raise WorkspaceError(_command_failure("repository validation failed", top_level))
        repository_root = Path(
            _read_single_line(top_level.stdout_path, "repository root")
        ).resolve()
        if root_path == repository_root or root_path.is_relative_to(repository_root):
            raise WorkspaceError("workspace root must be outside the source repository")

        resolved = _run_command(
            [
                git_executable,
                "-C",
                str(repository_root),
                "rev-parse",
                "--verify",
                f"{revision}^{{commit}}",
            ],
            cwd=repository_root,
            evidence_dir=evidence_dir,
            label="resolve-revision",
            timeout_seconds=timeout,
        )
        evidence.append(resolved)
        if not resolved.succeeded:
            raise WorkspaceError(_command_failure("revision resolution failed", resolved))
        base_commit = _read_single_line(resolved.stdout_path, "resolved revision")
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", base_commit) is None:
            raise WorkspaceError("git returned an invalid commit identifier")

        created = _run_command(
            [
                git_executable,
                "-C",
                str(repository_root),
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                base_commit,
            ],
            cwd=repository_root,
            evidence_dir=evidence_dir,
            label="worktree-add",
            timeout_seconds=timeout,
        )
        evidence.append(created)
        if not created.succeeded:
            raise WorkspaceError(_command_failure("detached worktree creation failed", created))

        workspace = cls(
            repository=repository_root,
            root=root_path,
            path=worktree_path,
            evidence_dir=evidence_dir,
            base_revision=revision,
            base_commit=base_commit.lower(),
            git_executable=git_executable,
            default_timeout_seconds=timeout,
            creation_evidence=tuple(evidence),
        )
        workspace._write_manifest()
        return workspace

    def execute(
        self,
        argv: Sequence[str],
        *,
        artifact_label: str = "workspace-command",
        timeout_seconds: float | None = None,
        env_overrides: Mapping[str, str | None] | None = None,
    ) -> CommandEvidence:
        """Execute an argv command inside the worktree without a shell."""

        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else _positive_timeout(timeout_seconds)
        )
        return _run_command(
            argv,
            cwd=self.path,
            evidence_dir=self.evidence_dir,
            label=artifact_label,
            timeout_seconds=timeout,
            env_overrides=env_overrides,
        )

    # A short alias makes the workspace convenient without introducing shell strings.
    run = execute

    def prepare_patch(
        self,
        patch: str | bytes,
        *,
        limits: PatchLimits | None = None,
    ) -> PreparedPatch:
        """Persist, audit, and check a patch without modifying the worktree."""

        effective_limits = limits or PatchLimits()
        patch_bytes = _patch_bytes(patch)
        patch_id = f"{hashlib.sha256(patch_bytes).hexdigest()[:16]}-{uuid.uuid4().hex[:8]}"
        patch_path = self.evidence_dir / f"patch-{patch_id}.diff"
        if len(patch_bytes) > effective_limits.max_bytes:
            rejection = PatchRejected(
                f"patch exceeds byte budget ({len(patch_bytes)} > {effective_limits.max_bytes})"
            )
            self._write_patch_rejection(patch_id, patch_path, rejection)
            raise rejection
        patch_path.write_bytes(patch_bytes)
        try:
            inspection = _inspect_patch(patch_bytes, effective_limits)
            self._reject_symlink_targets(inspection.files)
            self._require_clean_worktree()
        except (PatchRejected, WorkspaceError) as exc:
            self._write_patch_rejection(patch_id, patch_path, exc)
            raise

        inspection_path = self.evidence_dir / f"patch-{patch_id}.inspection.json"
        inspection_path.write_text(
            json.dumps(inspection.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        checked = self.execute(
            [self.git_executable, "apply", "--check", str(patch_path)],
            artifact_label=f"patch-{patch_id}-check",
        )
        if not checked.succeeded:
            rejection = PatchRejected(_command_failure("git apply --check rejected patch", checked))
            self._write_patch_rejection(patch_id, patch_path, rejection)
            raise rejection
        return PreparedPatch(
            patch_id=patch_id,
            patch_path=patch_path,
            workspace_path=self.path,
            inspection=inspection,
            check_evidence=checked,
        )

    def apply_patch(
        self,
        prepared: PreparedPatch,
        *,
        authorize: bool,
    ) -> PatchApplication:
        """Apply a previously checked patch after explicit authorization."""

        if not authorize:
            raise WorkspaceAuthorizationError("patch application requires authorize=True")
        if prepared.workspace_path != self.path:
            raise PatchRejected("prepared patch belongs to a different workspace")
        try:
            patch_bytes = prepared.patch_path.read_bytes()
        except OSError as exc:
            raise PatchRejected(f"prepared patch artifact cannot be read: {exc}") from exc
        digest = hashlib.sha256(patch_bytes).hexdigest()
        if digest != prepared.inspection.sha256:
            raise PatchRejected("prepared patch artifact changed after validation")
        self._reject_symlink_targets(prepared.inspection.files)
        self._require_clean_worktree()

        # Repeat the check at the mutation boundary to close the validation/application gap.
        recheck = self.execute(
            [self.git_executable, "apply", "--check", str(prepared.patch_path)],
            artifact_label=f"patch-{prepared.patch_id}-recheck",
        )
        if not recheck.succeeded:
            raise PatchRejected(_command_failure("patch recheck failed", recheck))
        applied = self.execute(
            [self.git_executable, "apply", str(prepared.patch_path)],
            artifact_label=f"patch-{prepared.patch_id}-apply",
        )
        if not applied.succeeded:
            raise PatchRejected(_command_failure("patch application failed", applied))
        diff = self.execute(
            [self.git_executable, "diff", "--binary", "--no-ext-diff", "--"],
            artifact_label=f"patch-{prepared.patch_id}-resulting-diff",
        )
        if not diff.succeeded:
            raise WorkspaceError(_command_failure("could not capture resulting diff", diff))
        return PatchApplication(
            prepared=prepared,
            apply_evidence=applied,
            diff_evidence=diff,
        )

    def _require_clean_worktree(self) -> None:
        status = self.execute(
            [self.git_executable, "status", "--porcelain", "--untracked-files=all"],
            artifact_label="worktree-cleanliness",
        )
        if not status.succeeded:
            raise WorkspaceError(_command_failure("could not inspect worktree status", status))
        if status.stdout_path.read_text(encoding="utf-8").strip():
            raise PatchRejected("worktree must be clean before preparing or applying a patch")

    def _reject_symlink_targets(self, files: Sequence[str]) -> None:
        for relative in files:
            cursor = self.path
            for component in PurePosixPath(relative).parts:
                cursor /= component
                if cursor.is_symlink():
                    raise PatchRejected(f"patch target traverses a symlink: {relative}")

    def _write_manifest(self) -> None:
        manifest = {
            "repository": str(self.repository),
            "root": str(self.root),
            "worktree": str(self.path),
            "evidence_dir": str(self.evidence_dir),
            "base_revision": self.base_revision,
            "base_commit": self.base_commit,
            "detached": True,
            "automatic_cleanup": False,
            "creation_commands": [item.to_dict() for item in self.creation_evidence],
        }
        (self.evidence_dir / "workspace-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_patch_rejection(self, patch_id: str, patch_path: Path, error: BaseException) -> None:
        payload = {
            "patch_id": patch_id,
            "patch_path": str(patch_path),
            "error_type": type(error).__name__,
            "error": str(error),
        }
        (self.evidence_dir / f"patch-{patch_id}.rejected.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# Descriptive alias for callers that do not need to know the git implementation.
IsolatedPatchWorkspace = GitWorktreeWorkspace


def prepare_git_source(
    name: str,
    source: SourceConfig,
    cache_root: str | os.PathLike[str],
    evidence_dir: str | os.PathLike[str],
    *,
    git_executable: str = "git",
    timeout_seconds: float = 300.0,
    transport_repository: str | os.PathLike[str] | None = None,
    transport_ref: str | None = None,
) -> PreparedGitSource:
    """Reuse a cached immutable commit, or fetch the declared source when absent."""

    if _SAFE_SOURCE_NAME.fullmatch(name) is None:
        raise ValueError("source name must be a safe identifier")
    _validate_executable(git_executable, "git_executable")
    timeout = _positive_timeout(timeout_seconds)
    cache_root_path = Path(cache_root).resolve()
    evidence_path = Path(evidence_dir).resolve()
    effective_repository = (
        source.repository
        if transport_repository is None
        else os.fspath(transport_repository)
    )
    effective_ref = source.ref if transport_ref is None else transport_ref
    if (transport_repository is None) != (transport_ref is None):
        raise ValueError("transport_repository and transport_ref must be supplied together")
    if not effective_ref.startswith(("refs/heads/", "refs/tags/")):
        raise ValueError("transport_ref must be a full branch or tag ref")
    cache_key = hashlib.sha256(source.repository.encode("utf-8")).hexdigest()[:16]
    repository_path = cache_root_path / f"{name}-{cache_key}"
    cache_root_path.mkdir(parents=True, exist_ok=True)
    evidence_path.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root_path / f".{name}-{cache_key}.lock"

    commands: list[CommandEvidence] = []
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        cache_existed = repository_path.exists()
        if not cache_existed:
            clone_ref = effective_ref.split("/", maxsplit=2)[-1]
            cloned = _run_command(
                [
                    git_executable,
                    "clone",
                    "--no-checkout",
                    "--single-branch",
                    "--no-tags",
                    "--branch",
                    clone_ref,
                    "--origin",
                    "origin",
                    "--",
                    effective_repository,
                    str(repository_path),
                ],
                cwd=cache_root_path,
                evidence_dir=evidence_path,
                label=f"source-{name}-clone",
                timeout_seconds=timeout,
                env_overrides=_git_auth_env(),
            )
            commands.append(cloned)
            if not cloned.succeeded:
                raise SourcePreparationError(
                    _command_failure(f"source {name!r} clone failed", cloned)
                )
        if repository_path.is_symlink() or not repository_path.is_dir():
            raise SourcePreparationError(
                f"source {name!r} cache is not a regular directory: {repository_path}"
            )

        origin = _run_command(
            [git_executable, "-C", str(repository_path), "remote", "get-url", "origin"],
            cwd=repository_path,
            evidence_dir=evidence_path,
            label=f"source-{name}-origin",
            timeout_seconds=timeout,
        )
        commands.append(origin)
        if not origin.succeeded:
            raise SourcePreparationError(
                _command_failure(f"source {name!r} cache validation failed", origin)
            )
        observed_origin = _read_single_line(origin.stdout_path, f"source {name!r} origin")
        if observed_origin != effective_repository:
            if transport_repository is None:
                raise SourcePreparationError(
                    f"source {name!r} cache origin mismatch: expected {source.repository!r}, "
                    f"observed {observed_origin!r}"
                )
            changed_origin = _run_command(
                [
                    git_executable,
                    "-C",
                    str(repository_path),
                    "remote",
                    "set-url",
                    "origin",
                    effective_repository,
                ],
                cwd=repository_path,
                evidence_dir=evidence_path,
                label=f"source-{name}-set-transport",
                timeout_seconds=timeout,
            )
            commands.append(changed_origin)
            if not changed_origin.succeeded:
                raise SourcePreparationError(
                    _command_failure(f"source {name!r} transport update failed", changed_origin)
                )

        resolved_revision = _git_output(
            repository_path,
            evidence_path,
            f"source-{name}-resolve-revision",
            timeout,
            git_executable,
            "rev-parse",
            "--verify",
            f"{source.revision}^{{commit}}",
            commands=commands,
            required=False,
        )
        cache_hit = cache_existed and resolved_revision is not None
        observed_ref_revision = None
        if not cache_hit:
            fetched_ref = _run_command(
                [
                    git_executable,
                    "-C",
                    str(repository_path),
                    "fetch",
                    "--no-tags",
                    "--force",
                    "origin",
                    effective_ref,
                ],
                cwd=repository_path,
                evidence_dir=evidence_path,
                label=f"source-{name}-fetch-ref",
                timeout_seconds=timeout,
                env_overrides=_git_auth_env(),
            )
            commands.append(fetched_ref)
            if not fetched_ref.succeeded:
                raise SourcePreparationError(
                    _command_failure(f"source {name!r} ref fetch failed", fetched_ref)
                )
            observed_ref_revision = _git_output(
                repository_path,
                evidence_path,
                f"source-{name}-resolve-ref",
                timeout,
                git_executable,
                "rev-parse",
                "--verify",
                "FETCH_HEAD^{commit}",
                commands=commands,
            )

            resolved_revision = _git_output(
                repository_path,
                evidence_path,
                f"source-{name}-resolve-revision",
                timeout,
                git_executable,
                "rev-parse",
                "--verify",
                f"{source.revision}^{{commit}}",
                commands=commands,
                required=False,
            )
        if resolved_revision is None:
            fetched_revision = _run_command(
                [
                    git_executable,
                    "-C",
                    str(repository_path),
                    "fetch",
                    "--no-tags",
                    "--force",
                    "origin",
                    source.revision,
                ],
                cwd=repository_path,
                evidence_dir=evidence_path,
                label=f"source-{name}-fetch-revision",
                timeout_seconds=timeout,
                env_overrides=_git_auth_env(),
            )
            commands.append(fetched_revision)
            if not fetched_revision.succeeded:
                raise SourcePreparationError(
                    _command_failure(f"source {name!r} revision fetch failed", fetched_revision)
                )
            resolved_revision = _git_output(
                repository_path,
                evidence_path,
                f"source-{name}-resolve-fetched-revision",
                timeout,
                git_executable,
                "rev-parse",
                "--verify",
                f"{source.revision}^{{commit}}",
                commands=commands,
            )
        if resolved_revision is None or resolved_revision.casefold() != source.revision:
            raise SourcePreparationError(
                f"source {name!r} did not resolve to locked revision {source.revision}"
            )

    manifest = {
        "schema_version": 1,
        "name": name,
        "repository": source.repository,
        "ref": source.ref,
        "revision": source.revision,
        "observed_ref_revision": observed_ref_revision,
        "ref_matches_revision": (
            observed_ref_revision == source.revision if observed_ref_revision is not None else None
        ),
        "cache_hit": cache_hit,
        "transport": "controller_bundle" if transport_repository is not None else "git",
        "cache": str(repository_path),
        "commands": [command.to_dict() for command in commands],
    }
    (evidence_path / f"source-{name}.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return PreparedGitSource(
        name=name,
        repository=repository_path,
        remote=source.repository,
        ref=source.ref,
        revision=source.revision,
        observed_ref_revision=observed_ref_revision,
        evidence_dir=evidence_path,
    )


def create_source_bundle(
    prepared: PreparedGitSource,
    destination: str | os.PathLike[str],
    evidence_dir: str | os.PathLike[str],
    *,
    git_executable: str = "git",
    timeout_seconds: float = 300.0,
) -> SourceBundle:
    """Create a full, exact-revision bundle without exporting unrelated refs or tags."""

    _validate_executable(git_executable, "git_executable")
    timeout = _positive_timeout(timeout_seconds)
    destination_path = Path(destination).resolve()
    evidence_path = Path(evidence_dir).resolve()
    destination_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    evidence_path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination_path.exists() or destination_path.is_symlink():
        raise SourcePreparationError(
            f"source bundle destination already exists: {destination_path}"
        )

    commands: list[CommandEvidence] = []
    with tempfile.TemporaryDirectory(
        prefix=f".{prepared.name}-bundle-",
        dir=destination_path.parent,
    ) as temporary:
        export_repository = Path(temporary) / "repository.git"
        cloned = _run_command(
            [
                git_executable,
                "clone",
                "--bare",
                "--shared",
                "--no-tags",
                "--",
                str(prepared.repository),
                str(export_repository),
            ],
            cwd=destination_path.parent,
            evidence_dir=evidence_path,
            label=f"source-{prepared.name}-bundle-clone",
            timeout_seconds=timeout,
        )
        commands.append(cloned)
        if not cloned.succeeded:
            raise SourcePreparationError(
                _command_failure(f"source {prepared.name!r} bundle clone failed", cloned)
            )
        pinned = _run_command(
            [
                git_executable,
                "-C",
                str(export_repository),
                "update-ref",
                STAGED_SOURCE_REF,
                prepared.revision,
            ],
            cwd=export_repository,
            evidence_dir=evidence_path,
            label=f"source-{prepared.name}-bundle-ref",
            timeout_seconds=timeout,
        )
        commands.append(pinned)
        if not pinned.succeeded:
            raise SourcePreparationError(
                _command_failure(f"source {prepared.name!r} bundle ref failed", pinned)
            )
        bundled = _run_command(
            [
                git_executable,
                "-C",
                str(export_repository),
                "bundle",
                "create",
                str(destination_path),
                STAGED_SOURCE_REF,
            ],
            cwd=export_repository,
            evidence_dir=evidence_path,
            label=f"source-{prepared.name}-bundle-create",
            timeout_seconds=timeout,
        )
        commands.append(bundled)
        if not bundled.succeeded:
            raise SourcePreparationError(
                _command_failure(f"source {prepared.name!r} bundle creation failed", bundled)
            )

    destination_path.chmod(0o600)
    head = _git_output(
        prepared.repository,
        evidence_path,
        f"source-{prepared.name}-bundle-head",
        timeout,
        git_executable,
        "bundle",
        "list-heads",
        str(destination_path),
        STAGED_SOURCE_REF,
        commands=commands,
    )
    fields = head.split(maxsplit=1) if head is not None else []
    if len(fields) != 2 or fields[0] != prepared.revision or fields[1] != STAGED_SOURCE_REF:
        raise SourcePreparationError(
            f"source {prepared.name!r} bundle does not contain the locked revision"
        )
    digest = _file_sha256(destination_path)
    bundle = SourceBundle(
        name=prepared.name,
        path=destination_path,
        ref=STAGED_SOURCE_REF,
        revision=prepared.revision,
        sha256=digest,
        size_bytes=destination_path.stat().st_size,
    )
    (evidence_path / f"source-{prepared.name}-bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": bundle.name,
                "ref": bundle.ref,
                "revision": bundle.revision,
                "sha256": bundle.sha256,
                "size_bytes": bundle.size_bytes,
                "commands": [command.to_dict() for command in commands],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle


def create_source_worktree(
    prepared: PreparedGitSource,
    source: SourceConfig,
    root: str | os.PathLike[str],
    *,
    git_executable: str = "git",
    timeout_seconds: float = 300.0,
) -> GitWorktreeWorkspace:
    """Create one isolated source worktree and initialize declared submodules."""

    workspace = GitWorktreeWorkspace.create(
        prepared.repository,
        root,
        revision=prepared.revision,
        git_executable=git_executable,
        timeout_seconds=timeout_seconds,
    )
    if source.submodules:
        updated = workspace.execute(
            [git_executable, "submodule", "update", "--init", "--recursive"],
            artifact_label="source-submodules",
            timeout_seconds=timeout_seconds,
        )
        if not updated.succeeded:
            raise SourcePreparationError(
                _command_failure(
                    f"source {prepared.name!r} submodule initialization failed",
                    updated,
                )
            )
    return workspace


_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_SOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MODE_LINE = re.compile(r"(?:old|new|new file|deleted file) mode ([0-7]{6})$")
_INDEX_MODE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+ ([0-7]{6})$")


def _inspect_patch(patch: bytes, limits: PatchLimits) -> PatchInspection:
    if len(patch) > limits.max_bytes:
        raise PatchRejected(f"patch exceeds byte budget ({len(patch)} > {limits.max_bytes})")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchRejected("patch must be valid UTF-8") from exc
    if "\x00" in text:
        raise PatchRejected("patch contains a NUL byte")
    if "GIT binary patch" in text or "Binary files " in text:
        raise PatchRejected("binary patches are not allowed")

    files: set[str] = set()
    additions = 0
    deletions = 0
    diff_headers = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("diff --git "):
            diff_headers += 1
            parts = line.split()
            if len(parts) != 4:
                raise PatchRejected(
                    f"line {line_number}: quoted or whitespace-containing paths are not allowed"
                )
            files.add(_safe_patch_path(parts[2], line_number, expected_prefix="a/"))
            files.add(_safe_patch_path(parts[3], line_number, expected_prefix="b/"))
            continue
        if line.startswith("--- "):
            raw = line[4:].split("\t", maxsplit=1)[0]
            if raw != "/dev/null":
                files.add(_safe_patch_path(raw, line_number, expected_prefix="a/"))
            continue
        if line.startswith("+++ "):
            raw = line[4:].split("\t", maxsplit=1)[0]
            if raw != "/dev/null":
                files.add(_safe_patch_path(raw, line_number, expected_prefix="b/"))
            continue
        for prefix in ("rename from ", "copy from "):
            if line.startswith(prefix):
                files.add(_safe_patch_path(line[len(prefix) :], line_number))
        for prefix in ("rename to ", "copy to "):
            if line.startswith(prefix):
                files.add(_safe_patch_path(line[len(prefix) :], line_number))
        mode_match = _MODE_LINE.fullmatch(line) or _INDEX_MODE.fullmatch(line)
        if mode_match is not None and mode_match.group(1) not in {"100644", "100755"}:
            mode = mode_match.group(1)
            if mode == "120000":
                raise PatchRejected("symbolic-link patches are not allowed")
            raise PatchRejected(f"non-regular git mode is not allowed: {mode}")
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    if diff_headers == 0 or not files:
        raise PatchRejected("patch must be a non-empty git-format diff")
    ordered_files = tuple(sorted(files))
    if len(ordered_files) > limits.max_files:
        raise PatchRejected(
            f"patch exceeds file budget ({len(ordered_files)} > {limits.max_files})"
        )
    changed_lines = additions + deletions
    if changed_lines > limits.max_changed_lines:
        raise PatchRejected(
            f"patch exceeds changed-line budget ({changed_lines} > {limits.max_changed_lines})"
        )
    return PatchInspection(
        sha256=hashlib.sha256(patch).hexdigest(),
        size_bytes=len(patch),
        files=ordered_files,
        additions=additions,
        deletions=deletions,
    )


def _safe_patch_path(raw_path: str, line_number: int, expected_prefix: str | None = None) -> str:
    if not raw_path or raw_path.startswith(("/", "\\")):
        raise PatchRejected(f"line {line_number}: absolute or empty patch path is not allowed")
    if any(character in raw_path for character in ('"', "'", "\\", "\x00")):
        raise PatchRejected(f"line {line_number}: quoted, escaped, or NUL path is not allowed")
    path = raw_path
    if expected_prefix is not None:
        if not path.startswith(expected_prefix):
            raise PatchRejected(f"line {line_number}: expected path prefix {expected_prefix!r}")
        path = path[len(expected_prefix) :]
    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts or path in {".", ".."} or path.endswith("/"):
        raise PatchRejected(f"line {line_number}: invalid patch path {raw_path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise PatchRejected(f"line {line_number}: non-normal patch path {raw_path!r}")
    if any(part.casefold() == ".git" for part in parts):
        raise PatchRejected(f"line {line_number}: .git paths are not allowed")
    if ":" in parts[0]:
        raise PatchRejected(f"line {line_number}: drive-qualified paths are not allowed")
    return pure.as_posix()


def _patch_bytes(patch: str | bytes) -> bytes:
    if isinstance(patch, str):
        return patch.encode("utf-8")
    if isinstance(patch, bytes):
        return patch
    raise TypeError("patch must be text or bytes")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_auth_env() -> Mapping[str, str | None]:
    """Expose an existing local SSH agent only to Git source transport commands."""

    return {"SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK")}


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    evidence_dir: Path,
    label: str,
    timeout_seconds: float,
    env_overrides: Mapping[str, str | None] | None = None,
) -> CommandEvidence:
    normalized = _validate_argv(argv)
    environment = {name: os.environ[name] for name in DEFAULT_ENV_ALLOWLIST if name in os.environ}
    if env_overrides is not None:
        for name, value in env_overrides.items():
            if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
                raise ValueError(f"invalid environment variable name: {name!r}")
            if value is not None and (not isinstance(value, str) or "\x00" in value):
                raise ValueError(f"invalid environment value for {name!r}")
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
    safe_label = _LABEL_UNSAFE.sub("-", label.strip()).strip(".-")[:80] or "command"
    command_id = uuid.uuid4().hex
    stdout_path = evidence_dir / f"{safe_label}-{command_id}.stdout.log"
    stderr_path = evidence_dir / f"{safe_label}-{command_id}.stderr.log"
    started = time.monotonic()
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
        try:
            completed = subprocess.run(
                list(normalized),
                cwd=cwd,
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"command timed out after {timeout_seconds:g} seconds"
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            stderr_file.write((error + "\n").encode("utf-8", errors="replace"))
    return CommandEvidence(
        argv=normalized,
        cwd=cwd,
        returncode=returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=max(0.0, time.monotonic() - started),
        timed_out=timed_out,
        error=error,
    )


def _git_output(
    repository: Path,
    evidence_dir: Path,
    label: str,
    timeout_seconds: float,
    git_executable: str,
    *arguments: str,
    commands: list[CommandEvidence],
    required: bool = True,
) -> str | None:
    evidence = _run_command(
        [git_executable, "-C", str(repository), *arguments],
        cwd=repository,
        evidence_dir=evidence_dir,
        label=label,
        timeout_seconds=timeout_seconds,
    )
    commands.append(evidence)
    if not evidence.succeeded:
        if not required:
            return None
        raise SourcePreparationError(_command_failure(f"{label} failed", evidence))
    return _read_single_line(evidence.stdout_path, label).casefold()


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str | bytes):
        raise TypeError("argv must be a sequence, not a command string")
    normalized = tuple(argv)
    if not normalized:
        raise ValueError("argv must not be empty")
    for index, value in enumerate(normalized):
        if not isinstance(value, str):
            raise TypeError(f"argv[{index}] must be a string")
        if "\x00" in value:
            raise ValueError(f"argv[{index}] contains a NUL byte")
    if not normalized[0]:
        raise ValueError("argv[0] must name an executable")
    return normalized


def _validate_executable(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty argv token")
    return value


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    return float(value)


def _read_single_line(path: Path, description: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text or "\n" in text:
        raise WorkspaceError(f"git returned an invalid {description}")
    return text


def _command_failure(prefix: str, evidence: CommandEvidence) -> str:
    detail = evidence.error or f"exit status {evidence.returncode}"
    return f"{prefix}: {detail}; stderr artifact: {evidence.stderr_path}"


__all__ = [
    "STAGED_SOURCE_REF",
    "CommandEvidence",
    "GitWorktreeWorkspace",
    "IsolatedPatchWorkspace",
    "PatchApplication",
    "PatchInspection",
    "PatchLimits",
    "PatchRejected",
    "PreparedPatch",
    "SourceBundle",
    "SourcePreparationError",
    "WorkspaceAuthorizationError",
    "WorkspaceError",
    "create_source_bundle",
    "create_source_worktree",
    "prepare_git_source",
]
