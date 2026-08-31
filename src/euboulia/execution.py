"""Safe, auditable subprocess execution.

The execution layer deliberately accepts only argument vectors.  It has no API for
shell command strings, process discovery, or process termination.  Framework
adapters can therefore describe a benchmark without gaining an implicit ability to
manage an inference server.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

DEFAULT_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "CONDA_PREFIX",
        "CUDA_VISIBLE_DEVICES",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONPATH",
        "TMP",
        "TEMP",
        "TMPDIR",
        "VIRTUAL_ENV",
    }
)

_ARTIFACT_PREFIX = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The complete, serializable outcome of one attempted command execution."""

    command_id: str
    argv: tuple[str, ...]
    cwd: Path | None
    returncode: int | None
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    environment_keys: tuple[str, ...]
    timed_out: bool = False
    dry_run: bool = False
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether execution, or dry-run validation, completed successfully."""

        if self.dry_run:
            return self.error is None and not self.timed_out
        return self.returncode == 0 and self.error is None and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation without environment values."""

        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "cwd": str(self.cwd) if self.cwd is not None else None,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "environment_keys": list(self.environment_keys),
            "timed_out": self.timed_out,
            "dry_run": self.dry_run,
            "error": self.error,
            "succeeded": self.succeeded,
        }


class CommandExecutor:
    """Run argv-based commands and persist stdout/stderr as artifacts.

    Only explicitly allowlisted variables are inherited from the parent process.
    Overrides are explicit additions and may also remove a variable by assigning
    ``None``.  Values are never copied into :class:`ExecutionResult`.
    """

    def __init__(
        self,
        artifact_dir: str | os.PathLike[str],
        *,
        dry_run: bool = False,
        default_timeout_seconds: float | None = None,
        env_allowlist: Iterable[str] = DEFAULT_ENV_ALLOWLIST,
        env_overrides: Mapping[str, str | None] | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.dry_run = dry_run
        self.default_timeout_seconds = _validate_timeout(default_timeout_seconds)
        self.env_allowlist = _validate_env_allowlist(env_allowlist)
        self.env_overrides = _validate_env_overrides(env_overrides or {})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        timeout_seconds: float | None = None,
        env_allowlist: Iterable[str] | None = None,
        env_overrides: Mapping[str, str | None] | None = None,
        artifact_prefix: str = "command",
        dry_run: bool | None = None,
    ) -> ExecutionResult:
        """Validate and run one command without invoking a shell.

        Invalid API input raises ``TypeError`` or ``ValueError``.  Runtime failures
        such as a missing executable or timeout are returned as structured results
        so the experiment ledger can retain the failed attempt.
        """

        normalized_argv = _validate_argv(argv)
        normalized_cwd = Path(cwd) if cwd is not None else None
        effective_timeout = _validate_timeout(
            self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        effective_allowlist = (
            self.env_allowlist if env_allowlist is None else _validate_env_allowlist(env_allowlist)
        )
        effective_overrides = dict(self.env_overrides)
        if env_overrides is not None:
            effective_overrides.update(_validate_env_overrides(env_overrides))
        environment = _build_environment(effective_allowlist, effective_overrides)

        command_id = uuid.uuid4().hex
        safe_prefix = _safe_artifact_prefix(artifact_prefix)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.artifact_dir / f"{safe_prefix}-{command_id}.stdout.log"
        stderr_path = self.artifact_dir / f"{safe_prefix}-{command_id}.stderr.log"

        started_wall = datetime.now(UTC)
        started_monotonic = time.monotonic()
        is_dry_run = self.dry_run if dry_run is None else dry_run
        returncode: int | None = None
        timed_out = False
        error: str | None = None

        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
            if not is_dry_run:
                try:
                    completed = subprocess.run(
                        list(normalized_argv),
                        cwd=normalized_cwd,
                        env=environment,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        timeout=effective_timeout,
                        check=False,
                        shell=False,
                    )
                    returncode = completed.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    error = (
                        "command timed out"
                        if effective_timeout is None
                        else f"command timed out after {effective_timeout:g} seconds"
                    )
                except OSError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    stderr_file.write((error + "\n").encode("utf-8", errors="replace"))

        finished_wall = datetime.now(UTC)
        duration_seconds = max(0.0, time.monotonic() - started_monotonic)
        return ExecutionResult(
            command_id=command_id,
            argv=normalized_argv,
            cwd=normalized_cwd,
            returncode=returncode,
            started_at=started_wall.isoformat(),
            finished_at=finished_wall.isoformat(),
            duration_seconds=duration_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            environment_keys=tuple(sorted(environment)),
            timed_out=timed_out,
            dry_run=is_dry_run,
            error=error,
        )


def execute(
    argv: Sequence[str],
    *,
    artifact_dir: str | os.PathLike[str],
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: float | None = None,
    env_allowlist: Iterable[str] = DEFAULT_ENV_ALLOWLIST,
    env_overrides: Mapping[str, str | None] | None = None,
    artifact_prefix: str = "command",
    dry_run: bool = False,
) -> ExecutionResult:
    """Convenience wrapper for a single :class:`CommandExecutor` invocation."""

    executor = CommandExecutor(
        artifact_dir,
        dry_run=dry_run,
        default_timeout_seconds=timeout_seconds,
        env_allowlist=env_allowlist,
        env_overrides=env_overrides,
    )
    return executor.run(argv, cwd=cwd, artifact_prefix=artifact_prefix)


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str | bytes):
        raise TypeError("argv must be a sequence of strings, not a command string")
    if not isinstance(argv, Sequence):
        raise TypeError("argv must be a sequence of strings")
    normalized = tuple(argv)
    if not normalized:
        raise ValueError("argv must not be empty")
    for index, item in enumerate(normalized):
        if not isinstance(item, str):
            raise TypeError(f"argv[{index}] must be a string")
        if "\x00" in item:
            raise ValueError(f"argv[{index}] contains a NUL byte")
    if not normalized[0]:
        raise ValueError("argv[0] must name an executable")
    return normalized


def _validate_timeout(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise TypeError("timeout_seconds must be a positive number or None")
    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    return timeout


def _validate_env_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError("environment variable names must be strings")
    if not name or "=" in name or "\x00" in name:
        raise ValueError(f"invalid environment variable name: {name!r}")
    return name


def _validate_env_allowlist(env_allowlist: Iterable[str]) -> frozenset[str]:
    if isinstance(env_allowlist, str | bytes):
        raise TypeError("env_allowlist must be an iterable of variable names")
    return frozenset(_validate_env_name(name) for name in env_allowlist)


def _validate_env_overrides(
    env_overrides: Mapping[str, str | None],
) -> dict[str, str | None]:
    if not isinstance(env_overrides, Mapping):
        raise TypeError("env_overrides must be a mapping")
    validated: dict[str, str | None] = {}
    for raw_name, value in env_overrides.items():
        name = _validate_env_name(raw_name)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"environment value for {name!r} must be a string or None")
        if value is not None and "\x00" in value:
            raise ValueError(f"environment value for {name!r} contains a NUL byte")
        validated[name] = value
    return validated


def _build_environment(
    allowlist: Iterable[str], overrides: Mapping[str, str | None]
) -> dict[str, str]:
    environment = {name: os.environ[name] for name in allowlist if name in os.environ}
    for name, value in overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def _safe_artifact_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise TypeError("artifact_prefix must be a string")
    safe = _ARTIFACT_PREFIX.sub("-", prefix.strip()).strip(".-")
    return safe[:80] or "command"


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "CommandExecutor",
    "ExecutionResult",
    "execute",
]
