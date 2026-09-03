"""Owned, single-host lifecycle control for an SGLang optimization target.

The controller in this module deliberately has no process-discovery or port-kill
fallback.  It can signal only a process group that it started itself and for
which the caller presents an instance-signed :class:`ServiceHandle`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import string
import subprocess
import time
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.client import HTTPMessage
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from euboulia.execution import DEFAULT_ENV_ALLOWLIST, CommandExecutor, ExecutionResult

_OPTION_NAME: Final[re.Pattern[str]] = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9_-]*$")
_ENV_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_LABEL: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]+")
_PYTHON_EXECUTABLE: Final[re.Pattern[str]] = re.compile(r"^(?:python|python\d+(?:\.\d+)*)$")
_SGLANG_MODULES: Final[frozenset[str]] = frozenset(
    {
        "sglang.launch_server",
        "sglang.srt.entrypoints.http_server",
    }
)
_MAX_BUILD_COMMANDS: Final[int] = 32
_FORMATTER: Final[string.Formatter] = string.Formatter()


class TargetError(RuntimeError):
    """Base error for owned target operations."""


class TargetLaunchError(TargetError):
    """Raised when a declared target cannot be started safely."""


class TargetReadinessError(TargetError):
    """Raised when an owned target exits or misses its readiness deadline."""


class TargetOwnershipError(TargetError):
    """Raised when a handle is forged, foreign, stale, or already consumed."""


class TargetBuildError(TargetError):
    """A finite build stopped at its first failed command."""

    def __init__(
        self,
        message: str,
        *,
        results: tuple[ExecutionResult, ...],
        manifest_path: Path,
    ) -> None:
        super().__init__(message)
        self.results = results
        self.manifest_path = manifest_path


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep a loopback readiness probe from being redirected off target."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        return None


def _validate_argv(argv: Sequence[str], name: str = "argv") -> tuple[str, ...]:
    if isinstance(argv, str | bytes):
        raise TypeError(f"{name} must be an argv sequence, not a command string")
    if not isinstance(argv, Sequence):
        raise TypeError(f"{name} must be an argv sequence")
    result = tuple(argv)
    if not result:
        raise ValueError(f"{name} must not be empty")
    for index, token in enumerate(result):
        if not isinstance(token, str):
            raise TypeError(f"{name}[{index}] must be a string")
        if "\x00" in token:
            raise ValueError(f"{name}[{index}] contains a NUL byte")
    if not result[0]:
        raise ValueError(f"{name}[0] must name an executable")
    return result


def _validate_option_name(value: object, name: str) -> str:
    if not isinstance(value, str) or _OPTION_NAME.fullmatch(value) is None:
        raise ValueError(f"{name} must be a long option such as '--tp-size'")
    return value


def _validate_environment(value: Mapping[str, str | None], name: str) -> Mapping[str, str | None]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result: dict[str, str | None] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or _ENV_NAME.fullmatch(raw_key) is None:
            raise ValueError(f"{name} contains an invalid environment variable name: {raw_key!r}")
        if raw_value is not None and not isinstance(raw_value, str):
            raise TypeError(f"{name}.{raw_key} must be a string or None")
        if raw_value is not None and "\x00" in raw_value:
            raise ValueError(f"{name}.{raw_key} contains a NUL byte")
        result[raw_key] = raw_value
    return MappingProxyType(result)


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string without NUL bytes")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ServerArgumentPatch:
    """An immutable semantic overlay for long-form server options.

    ``set`` values are one argv token; ``None`` represents a bare boolean flag.
    Both ``--name value`` and ``--name=value`` in the baseline are recognized as
    the same option.  A baseline with any repeated long option is rejected rather
    than relying on framework-specific "last value wins" behavior.
    """

    set: Mapping[str, str | None] = field(default_factory=dict)
    remove: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.set, Mapping):
            raise TypeError("set must be a mapping of long options to string values or None")
        normalized_set: dict[str, str | None] = {}
        for raw_name, value in self.set.items():
            option = _validate_option_name(raw_name, "set option")
            if value is not None and not isinstance(value, str):
                raise TypeError(f"set[{option!r}] must be a string or None")
            if value is not None and "\x00" in value:
                raise ValueError(f"set[{option!r}] contains a NUL byte")
            normalized_set[option] = value

        if isinstance(self.remove, str | bytes) or not isinstance(self.remove, Collection):
            raise TypeError("remove must be a finite collection of long option names")
        normalized_remove = tuple(
            _validate_option_name(item, "remove option") for item in self.remove
        )
        if len(set(normalized_remove)) != len(normalized_remove):
            raise ValueError("remove contains a duplicate option")
        conflict = sorted(set(normalized_set).intersection(normalized_remove))
        if conflict:
            raise ValueError(f"options cannot be both set and removed: {', '.join(conflict)}")

        object.__setattr__(self, "set", MappingProxyType(dict(sorted(normalized_set.items()))))
        object.__setattr__(self, "remove", tuple(sorted(normalized_remove)))

    def apply(self, baseline_argv: Sequence[str]) -> tuple[str, ...]:
        """Return a deterministic overlay without mutating ``baseline_argv``."""

        baseline = _validate_argv(baseline_argv, "baseline_argv")
        occurrences = _long_option_occurrences(baseline)
        touched_options = set(self.set).union(self.remove)
        ambiguous = sorted(
            name
            for name, spans in occurrences.items()
            if len(spans) > 1 and name in touched_options
        )
        if ambiguous:
            raise ValueError(
                "baseline argv contains duplicate long options targeted by the patch: "
                + ", ".join(ambiguous)
            )

        replacements = dict(self.set)
        removals = set(self.remove)
        result: list[str] = []
        index = 0
        handled: set[str] = set()
        while index < len(baseline):
            token = baseline[index]
            if token == "--":
                result.extend(baseline[index:])
                break
            parsed = _parse_long_option(token)
            if parsed is None:
                result.append(token)
                index += 1
                continue
            option, inline = parsed
            consumes_value = inline is None and _has_separate_option_value(baseline, index)
            span = 2 if consumes_value else 1
            if option in removals:
                handled.add(option)
                index += span
                continue
            if option in replacements:
                result.append(option)
                value = replacements[option]
                if value is not None:
                    result.append(value)
                handled.add(option)
                index += span
                continue
            result.extend(baseline[index : index + span])
            index += span

        insertion_index = result.index("--") if "--" in result else len(result)
        additions: list[str] = []
        for option, value in replacements.items():
            if option in handled:
                continue
            additions.append(option)
            if value is not None:
                additions.append(value)
        result[insertion_index:insertion_index] = additions
        return tuple(result)

    merge = apply
    apply_to = apply


def _parse_long_option(token: str) -> tuple[str, str | None] | None:
    if token == "--" or not token.startswith("--"):
        return None
    name, separator, value = token.partition("=")
    _validate_option_name(name, "baseline option")
    return name, value if separator else None


def _has_separate_option_value(argv: tuple[str, ...], index: int) -> bool:
    next_index = index + 1
    if next_index >= len(argv):
        return False
    next_token = argv[next_index]
    return next_token != "--" and not next_token.startswith("--")


def _long_option_occurrences(
    argv: tuple[str, ...],
) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = {}
    index = 0
    while index < len(argv):
        if argv[index] == "--":
            break
        parsed = _parse_long_option(argv[index])
        if parsed is None:
            index += 1
            continue
        name, inline = parsed
        span = 2 if inline is None and _has_separate_option_value(argv, index) else 1
        result.setdefault(name, []).append((index, span))
        index += span
    return result


@dataclass(frozen=True, slots=True)
class BuildCommandSpec:
    """One bounded, argv-only build command."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float | None = None
    env: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "build command name"))
        object.__setattr__(self, "argv", _validate_argv(self.argv, "build command argv"))
        if self.timeout_seconds is not None:
            object.__setattr__(
                self,
                "timeout_seconds",
                _positive_number(self.timeout_seconds, "build command timeout_seconds"),
            )
        environment = _validate_environment(self.env, "build command env")
        object.__setattr__(self, "env", environment)
        placeholder_context = {"workspace": "/workspace"}
        for index, token in enumerate(self.argv):
            _materialize(token, placeholder_context, f"build command argv[{index}]")
        for name, value in environment.items():
            if value is not None:
                _materialize(value, placeholder_context, f"build command env.{name}")


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """A finite sequence of build commands, capped to prevent generated loops."""

    commands: tuple[BuildCommandSpec | Sequence[str], ...] = ()
    default_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if isinstance(self.commands, str | bytes) or not isinstance(self.commands, Sequence):
            raise TypeError("build commands must be a finite sequence")
        if len(self.commands) > _MAX_BUILD_COMMANDS:
            raise ValueError(f"build commands exceed the {_MAX_BUILD_COMMANDS}-command limit")
        normalized: list[BuildCommandSpec] = []
        for index, command in enumerate(self.commands, start=1):
            if isinstance(command, BuildCommandSpec):
                normalized.append(command)
            else:
                normalized.append(
                    BuildCommandSpec(name=f"build-{index:02d}", argv=_validate_argv(command))
                )
        object.__setattr__(self, "commands", tuple(normalized))
        object.__setattr__(
            self,
            "default_timeout_seconds",
            _positive_number(self.default_timeout_seconds, "default_timeout_seconds"),
        )


@dataclass(frozen=True, slots=True)
class ReadinessSpec:
    """A bounded HTTP readiness probe for a loopback target."""

    url: str
    timeout_seconds: float = 60.0
    interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _local_http_url(self.url, "readiness.url"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_number(self.timeout_seconds, "readiness.timeout_seconds"),
        )
        object.__setattr__(
            self,
            "interval_seconds",
            _positive_number(self.interval_seconds, "readiness.interval_seconds"),
        )


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """The complete, fixed declaration of one locally owned SGLang service."""

    provider: str
    model: str
    launch_argv: tuple[str, ...]
    endpoint: str
    readiness: ReadinessSpec
    launch_env: Mapping[str, str | None] = field(default_factory=dict)
    build: BuildSpec | None = None
    gpus: tuple[str | int, ...] = ()
    shutdown_timeout_seconds: float = 30.0
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider = _nonempty(str(self.provider), "provider").lower()
        if provider != "sglang":
            raise ValueError("provider must be 'sglang'")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", _nonempty(self.model, "model"))
        object.__setattr__(self, "launch_argv", _validate_argv(self.launch_argv, "launch_argv"))
        object.__setattr__(self, "endpoint", _local_http_url(self.endpoint, "endpoint"))
        if not isinstance(self.readiness, ReadinessSpec):
            raise TypeError("readiness must be a ReadinessSpec")
        launch_env = _validate_environment(self.launch_env, "launch_env")
        if "CUDA_VISIBLE_DEVICES" in launch_env:
            raise ValueError("launch_env must not override fixed CUDA_VISIBLE_DEVICES")
        object.__setattr__(self, "launch_env", launch_env)
        placeholder_context = {
            "workspace": "/workspace",
            "model": self.model,
            "endpoint": self.endpoint,
            "run_id": "run",
            "trial_id": "trial",
        }
        for index, token in enumerate(self.launch_argv):
            _materialize(token, placeholder_context, f"launch_argv[{index}]")
        for name, value in launch_env.items():
            if value is not None:
                _materialize(value, placeholder_context, f"launch_env.{name}")
        if self.build is not None and not isinstance(self.build, BuildSpec):
            raise TypeError("build must be a BuildSpec or None")
        object.__setattr__(self, "gpus", _validate_gpu_ids(self.gpus))
        object.__setattr__(
            self,
            "shutdown_timeout_seconds",
            _positive_number(self.shutdown_timeout_seconds, "shutdown_timeout_seconds"),
        )
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping")
        provenance = dict(self.provenance)
        try:
            frozen_source = json.dumps(provenance, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("provenance must be JSON-compatible and finite") from exc
        immutable_copy = json.loads(frozen_source)
        if not isinstance(immutable_copy, dict):  # pragma: no cover - source is a dict
            raise AssertionError("JSON provenance mapping did not round-trip as a mapping")
        object.__setattr__(self, "provenance", MappingProxyType(immutable_copy))

    @property
    def fixed_gpu_ids(self) -> tuple[str | int, ...]:
        return self.gpus


def _validate_gpu_ids(value: Sequence[str | int]) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence) or not value:
        raise ValueError("gpus must be a non-empty sequence of fixed GPU IDs")
    result: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, str | int):
            raise TypeError(f"gpus[{index}] must be an integer or string GPU ID")
        gpu_id = str(item)
        if (
            not gpu_id
            or any(character.isspace() for character in gpu_id)
            or "," in gpu_id
            or "\x00" in gpu_id
        ):
            raise ValueError(f"gpus[{index}] must be a non-empty token without comma or whitespace")
        result.append(gpu_id)
    if len(result) != len(set(result)):
        raise ValueError("gpus must not contain duplicate GPU IDs")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TargetChangeSet:
    """Server argument changes plus optional source-patch provenance."""

    arg_patch: ServerArgumentPatch = field(default_factory=ServerArgumentPatch)
    source_patch_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.arg_patch, ServerArgumentPatch):
            raise TypeError("arg_patch must be a ServerArgumentPatch")
        if self.source_patch_path is not None:
            object.__setattr__(self, "source_patch_path", Path(self.source_patch_path))

    @property
    def argument_patch(self) -> ServerArgumentPatch:
        return self.arg_patch


@dataclass(frozen=True, slots=True)
class ServiceHandle:
    """A signed bearer capability for exactly one controller-owned process."""

    handle_id: str
    pid: int
    process_group_id: int
    process_start_identity: str
    run_id: str
    trial_id: str
    argv_digest: str
    endpoint: str
    stdout_path: Path
    stderr_path: Path
    manifest_path: Path
    started_at: str
    _issuer_id: str = field(default="", repr=False)
    _signature: str = field(default="", repr=False)

    @property
    def pgid(self) -> int:
        return self.process_group_id

    @property
    def log_paths(self) -> tuple[Path, Path]:
        return (self.stdout_path, self.stderr_path)

    def to_dict(self) -> dict[str, object]:
        """Serialize public evidence without exposing the ownership signature."""

        return {
            "handle_id": self.handle_id,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "process_start_identity": self.process_start_identity,
            "run_id": self.run_id,
            "trial_id": self.trial_id,
            "argv_digest": self.argv_digest,
            "endpoint": self.endpoint,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "manifest_path": str(self.manifest_path),
            "started_at": self.started_at,
        }


@runtime_checkable
class TargetController(Protocol):
    """Protocol for an auditable, owned target lifecycle."""

    def build(
        self,
        workspace: str | os.PathLike[str] | object,
        spec: BuildSpec,
        evidence_dir: str | os.PathLike[str],
    ) -> tuple[ExecutionResult, ...]: ...

    def start(
        self,
        workspace: str | os.PathLike[str] | object,
        spec: TargetSpec,
        change_set: TargetChangeSet,
        evidence_dir: str | os.PathLike[str],
        *,
        run_id: str,
        trial_id: str,
    ) -> ServiceHandle: ...

    def wait_ready(self, handle: ServiceHandle) -> None: ...

    def stop(self, handle: ServiceHandle) -> None: ...


@dataclass(slots=True)
class _OwnedService:
    handle: ServiceHandle
    process: subprocess.Popen[bytes]
    spec: TargetSpec
    argv: tuple[str, ...]
    workspace: Path
    environment_keys: tuple[str, ...]
    source_patch: Mapping[str, object] | None
    ready_at: str | None = None
    readiness_status: str | None = None
    last_readiness_error: str | None = None
    stopped: bool = False


class SGLangTargetController:
    """Own one or more explicitly declared local SGLang process groups."""

    def __init__(
        self,
        *,
        env_allowlist: Iterable[str] = DEFAULT_ENV_ALLOWLIST,
    ) -> None:
        self._env_allowlist = _validate_env_allowlist(env_allowlist)
        self._issuer_id = uuid.uuid4().hex
        self._secret = secrets.token_bytes(32)
        self._services: dict[str, _OwnedService] = {}
        self._used_trials: set[tuple[str, str]] = set()

    def build(
        self,
        workspace: str | os.PathLike[str] | object,
        spec: BuildSpec,
        evidence_dir: str | os.PathLike[str],
    ) -> tuple[ExecutionResult, ...]:
        """Run the declared finite build in order and retain every result."""

        if not isinstance(spec, BuildSpec):
            raise TypeError("spec must be a BuildSpec")
        workspace_path = _workspace_path(workspace)
        evidence_path = _evidence_directory(evidence_dir)
        build_id = uuid.uuid4().hex
        manifest_path = evidence_path / f"build-{build_id}.manifest.json"
        executor = CommandExecutor(
            evidence_path,
            default_timeout_seconds=spec.default_timeout_seconds,
            env_allowlist=self._env_allowlist,
        )
        results: list[ExecutionResult] = []
        context = {"workspace": str(workspace_path)}

        for index, command in enumerate(spec.commands, start=1):
            if not isinstance(command, BuildCommandSpec):  # normalized by BuildSpec
                raise AssertionError("BuildSpec retained an unnormalized command")
            argv = tuple(
                _materialize(token, context, f"build command {index} argv")
                for token in command.argv
            )
            env = {
                name: (
                    None
                    if value is None
                    else _materialize(value, context, f"build command {index} env.{name}")
                )
                for name, value in command.env.items()
            }
            result = executor.run(
                argv,
                cwd=workspace_path,
                timeout_seconds=command.timeout_seconds,
                env_overrides=env,
                artifact_prefix=f"build-{index:02d}-{_safe_label(command.name)}",
            )
            results.append(result)
            status = "completed" if result.succeeded else "failed"
            _write_json(
                manifest_path,
                _build_manifest(build_id, workspace_path, spec, tuple(results), status),
            )
            if not result.succeeded:
                detail = result.error or f"exit status {result.returncode}"
                raise TargetBuildError(
                    f"build command {command.name!r} failed: {detail}; "
                    f"stderr artifact: {result.stderr_path}",
                    results=tuple(results),
                    manifest_path=manifest_path,
                )

        _write_json(
            manifest_path,
            _build_manifest(build_id, workspace_path, spec, tuple(results), "completed"),
        )
        return tuple(results)

    def start(
        self,
        workspace: str | os.PathLike[str] | object,
        spec: TargetSpec,
        change_set: TargetChangeSet,
        evidence_dir: str | os.PathLike[str],
        *,
        run_id: str,
        trial_id: str,
    ) -> ServiceHandle:
        """Start one SGLang server in a new session and issue its signed handle."""

        if not isinstance(spec, TargetSpec):
            raise TypeError("spec must be a TargetSpec")
        if not isinstance(change_set, TargetChangeSet):
            raise TypeError("change_set must be a TargetChangeSet")
        normalized_run = _nonempty(run_id, "run_id")
        normalized_trial = _nonempty(trial_id, "trial_id")
        trial_key = (normalized_run, normalized_trial)
        if any(not state.stopped for state in self._services.values()):
            raise TargetOwnershipError(
                "this controller already owns a service; consume its handle before starting another"
            )
        if trial_key in self._used_trials:
            raise TargetOwnershipError(
                f"run/trial identity has already been used: {normalized_run}/{normalized_trial}"
            )

        workspace_path = _workspace_path(workspace)
        evidence_path = _evidence_directory(evidence_dir)
        context = {
            "workspace": str(workspace_path),
            "model": spec.model,
            "endpoint": spec.endpoint,
            "run_id": normalized_run,
            "trial_id": normalized_trial,
        }
        patched_argv = change_set.arg_patch.apply(spec.launch_argv)
        argv = tuple(
            _materialize(token, context, f"launch_argv[{index}]")
            for index, token in enumerate(patched_argv)
        )
        if not _is_sglang_entrypoint(argv):
            raise TargetLaunchError(
                "launch argv must use 'sglang', 'sglang.launch_server', or an approved "
                "'python -m sglang.<server>' entrypoint"
            )
        environment = _build_environment(
            self._env_allowlist,
            {
                name: (
                    None if value is None else _materialize(value, context, f"launch_env.{name}")
                )
                for name, value in spec.launch_env.items()
            },
            spec.gpus,
        )
        source_patch = _source_patch_evidence(change_set.source_patch_path, workspace_path)

        handle_id = uuid.uuid4().hex
        label = (
            f"service-{_safe_label(normalized_run)}-{_safe_label(normalized_trial)}-{handle_id[:8]}"
        )
        stdout_path = evidence_path / f"{label}.stdout.log"
        stderr_path = evidence_path / f"{label}.stderr.log"
        manifest_path = evidence_path / f"{label}.manifest.json"
        started_at = _utc_now()
        argv_digest = _argv_digest(argv)

        try:
            with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
                process = subprocess.Popen(
                    list(argv),
                    cwd=workspace_path,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                )
        except OSError as exc:
            _write_json(
                manifest_path,
                {
                    "schema_version": 1,
                    "status": "launch_failed",
                    "run_id": normalized_run,
                    "trial_id": normalized_trial,
                    "argv": list(argv),
                    "argv_digest": argv_digest,
                    "workspace": str(workspace_path),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "started_at": started_at,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise TargetLaunchError(f"failed to launch SGLang target: {exc}") from exc

        process_group_id = _capture_process_group(process, manifest_path)
        process_start_identity = _capture_start_identity(process, manifest_path)
        unsigned = ServiceHandle(
            handle_id=handle_id,
            pid=process.pid,
            process_group_id=process_group_id,
            process_start_identity=process_start_identity,
            run_id=normalized_run,
            trial_id=normalized_trial,
            argv_digest=argv_digest,
            endpoint=spec.endpoint,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            manifest_path=manifest_path,
            started_at=started_at,
            _issuer_id=self._issuer_id,
        )
        handle = replace(unsigned, _signature=self._sign(unsigned))
        state = _OwnedService(
            handle=handle,
            process=process,
            spec=spec,
            argv=argv,
            workspace=workspace_path,
            environment_keys=tuple(sorted(environment)),
            source_patch=source_patch,
        )
        self._services[handle_id] = state
        self._used_trials.add(trial_key)
        try:
            self._write_service_manifest(state, "running")
        except BaseException as exc:
            try:
                _abort_unidentified_process(process, process_group_id)
            except BaseException as cleanup_exc:
                raise TargetLaunchError(
                    "failed to persist the service manifest and emergency teardown failed"
                ) from cleanup_exc
            state.stopped = True
            self._services.pop(handle_id, None)
            raise TargetLaunchError(
                "failed to persist the service manifest; the owned process was terminated"
            ) from exc
        return handle

    def wait_ready(self, handle: ServiceHandle) -> None:
        """Poll the declared loopback HTTP URL until ready or the deadline expires."""

        state = self._owned_state(handle)
        readiness = state.spec.readiness
        deadline = time.monotonic() + readiness.timeout_seconds
        last_error = "no HTTP response"
        request = Request(
            readiness.url,
            method="GET",
            headers={"User-Agent": "euboulia-target-controller/1"},
        )
        # A loopback ownership probe must never be routed through ambient proxies.
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        while True:
            returncode = state.process.poll()
            if returncode is not None:
                state.readiness_status = "exited_before_ready"
                state.last_readiness_error = last_error
                self._write_service_manifest(
                    state,
                    "exited_before_ready",
                    {"returncode": returncode, "last_readiness_error": last_error},
                )
                raise TargetReadinessError(
                    f"owned SGLang process exited before readiness with status {returncode}; "
                    f"stderr artifact: {handle.stderr_path}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.readiness_status = "timeout"
                state.last_readiness_error = last_error
                self._write_service_manifest(
                    state,
                    "readiness_timeout",
                    {"last_readiness_error": last_error},
                )
                raise TargetReadinessError(
                    f"readiness URL {readiness.url} did not become ready within "
                    f"{readiness.timeout_seconds:g} seconds: {last_error}"
                )
            try:
                with opener.open(request, timeout=max(0.001, min(1.0, remaining))) as response:
                    status = response.getcode()
                    if 200 <= status < 400:
                        state.ready_at = _utc_now()
                        state.readiness_status = "ready"
                        state.last_readiness_error = None
                        self._write_service_manifest(
                            state,
                            "ready",
                            {"ready_at": state.ready_at, "readiness_status": status},
                        )
                        return
                    last_error = f"HTTP status {status}"
            except HTTPError as exc:
                last_error = f"HTTP status {exc.code}"
            except (URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            state.last_readiness_error = last_error
            sleep_seconds = min(readiness.interval_seconds, max(0.0, deadline - time.monotonic()))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def stop(self, handle: ServiceHandle) -> None:
        """Stop only the exact signed process group represented by ``handle``."""

        state = self._owned_state(handle)
        if state.stopped:
            raise TargetOwnershipError("service handle has already been consumed")
        process = state.process
        returncode = process.poll()
        forced_kill = False
        term_sent = False

        if returncode is None:
            self._validate_live_identity(state)
            try:
                os.killpg(handle.process_group_id, signal.SIGTERM)
                term_sent = True
            except ProcessLookupError:
                pass
            try:
                returncode = process.wait(timeout=state.spec.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                returncode = process.poll()
                if returncode is None:
                    self._validate_live_identity(state)
                    try:
                        os.killpg(handle.process_group_id, signal.SIGKILL)
                        forced_kill = True
                    except ProcessLookupError:
                        pass
                returncode = process.wait()

        state.stopped = True
        self._write_service_manifest(
            state,
            "stopped",
            {
                "stopped_at": _utc_now(),
                "returncode": returncode,
                "sigterm_sent": term_sent,
                "forced_kill": forced_kill,
            },
        )

    def _sign(self, handle: ServiceHandle) -> str:
        payload = json.dumps(handle.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _owned_state(self, handle: ServiceHandle) -> _OwnedService:
        if not isinstance(handle, ServiceHandle):
            raise TargetOwnershipError("a ServiceHandle issued by this controller is required")
        if handle._issuer_id != self._issuer_id or not handle._signature:
            raise TargetOwnershipError("service handle was not issued by this controller")
        if not hmac.compare_digest(handle._signature, self._sign(handle)):
            raise TargetOwnershipError("service handle signature is invalid")
        state = self._services.get(handle.handle_id)
        if state is None or state.handle != handle:
            raise TargetOwnershipError("service handle is unknown or forged")
        if state.stopped:
            raise TargetOwnershipError("service handle has already been consumed")
        return state

    def _validate_live_identity(self, state: _OwnedService) -> None:
        handle = state.handle
        if state.process.pid != handle.pid:
            raise TargetOwnershipError("owned Popen PID no longer matches the service handle")
        current_identity = _read_process_start_identity(handle.pid)
        if current_identity != handle.process_start_identity:
            raise TargetOwnershipError("PID start identity no longer matches the service handle")
        try:
            current_group = os.getpgid(handle.pid)
        except ProcessLookupError as exc:
            raise TargetOwnershipError("owned process disappeared before teardown") from exc
        if current_group != handle.process_group_id:
            raise TargetOwnershipError("process group no longer matches the service handle")

    def _write_service_manifest(
        self,
        state: _OwnedService,
        status: str,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        handle = state.handle
        document: dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "provider": state.spec.provider,
            "model": state.spec.model,
            "handle": handle.to_dict(),
            "argv": list(state.argv),
            "argv_digest": handle.argv_digest,
            "workspace": str(state.workspace),
            "endpoint": state.spec.endpoint,
            "readiness_url": state.spec.readiness.url,
            "fixed_gpu_ids": list(state.spec.gpus),
            "environment_keys": list(state.environment_keys),
            "source_patch": dict(state.source_patch) if state.source_patch is not None else None,
            "provenance": dict(state.spec.provenance),
            "ready_at": state.ready_at,
            "readiness_status": state.readiness_status,
            "last_readiness_error": state.last_readiness_error,
        }
        if extra is not None:
            document.update(extra)
        _write_json(handle.manifest_path, document)


def _validate_env_allowlist(value: Iterable[str]) -> frozenset[str]:
    if isinstance(value, str | bytes):
        raise TypeError("env_allowlist must be an iterable of names, not a string")
    result: set[str] = set()
    for name in value:
        if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
            raise ValueError(f"invalid allowlisted environment variable name: {name!r}")
        result.add(name)
    return frozenset(result)


def _build_environment(
    allowlist: frozenset[str],
    overrides: Mapping[str, str | None],
    gpus: Sequence[str | int],
) -> dict[str, str]:
    environment = {name: os.environ[name] for name in allowlist if name in os.environ}
    for name, value in overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    environment["CUDA_VISIBLE_DEVICES"] = ",".join(str(item) for item in gpus)
    return environment


def _workspace_path(value: str | os.PathLike[str] | object) -> Path:
    raw: object = getattr(value, "path", value)
    if not isinstance(raw, str | os.PathLike):
        raise TypeError("workspace must be a path or expose a path attribute")
    path = Path(raw).resolve()
    if not path.is_dir():
        raise ValueError(f"workspace must be an existing directory: {path}")
    return path


def _evidence_directory(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_symlink():
        raise ValueError("evidence_dir must not be a symbolic link")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"evidence_dir is not a directory: {path}")
    return path.resolve()


def _local_http_url(value: object, name: str) -> str:
    url = _nonempty(value, name)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials or a fragment")
    host = parsed.hostname
    if host is None:
        raise ValueError(f"{name} must contain a host")
    if host.casefold() != "localhost":
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError(f"{name} must use localhost or a loopback IP address")
    if port is not None and port < 1:
        raise ValueError(f"{name} contains an invalid port")
    return url


def _materialize(value: str, context: Mapping[str, str], name: str) -> str:
    try:
        parsed = tuple(_FORMATTER.parse(value))
    except ValueError as exc:
        raise ValueError(f"{name} contains malformed placeholders") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in context:
            raise ValueError(f"{name} contains unknown placeholder {{{field_name}}}")
        if format_spec or conversion:
            raise ValueError(f"{name} placeholders must not use conversion or format syntax")
    try:
        return value.format_map(context)
    except (KeyError, ValueError) as exc:  # pragma: no cover - guarded by parse above
        raise ValueError(f"{name} contains an invalid placeholder") from exc


def _is_sglang_entrypoint(argv: tuple[str, ...]) -> bool:
    executable = Path(argv[0]).name
    if executable in {"sglang", "sglang.launch_server"}:
        return True
    if _PYTHON_EXECUTABLE.fullmatch(executable) is None:
        return False
    module_index = 2 if len(argv) >= 2 and argv[1] == "-m" else 3
    return (
        len(argv) > module_index
        and module_index == 3
        and argv[1:3] == ("-u", "-m")
        and argv[module_index] in _SGLANG_MODULES
    ) or (
        len(argv) > module_index
        and module_index == 2
        and argv[1] == "-m"
        and argv[module_index] in _SGLANG_MODULES
    )


def _source_patch_evidence(path: Path | None, workspace: Path) -> Mapping[str, object] | None:
    if path is None:
        return None
    candidate = path if path.is_absolute() else workspace / path
    if candidate.is_symlink() or not candidate.is_file():
        raise TargetLaunchError(f"source patch must be an existing regular file: {candidate}")
    content = candidate.read_bytes()
    return MappingProxyType(
        {
            "path": str(candidate.resolve()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )


def _capture_process_group(process: subprocess.Popen[bytes], manifest_path: Path) -> int:
    try:
        process_group_id = os.getpgid(process.pid)
    except ProcessLookupError as exc:
        _write_json(
            manifest_path,
            {"schema_version": 1, "status": "launch_failed", "pid": process.pid},
        )
        raise TargetLaunchError("SGLang process exited before ownership was established") from exc
    if process_group_id != process.pid:
        _abort_unidentified_process(process, process_group_id)
        raise TargetLaunchError("start_new_session did not create an isolated process group")
    return process_group_id


def _capture_start_identity(process: subprocess.Popen[bytes], manifest_path: Path) -> str:
    identity = _read_process_start_identity(process.pid)
    if identity is not None:
        return identity
    process_group_id = process.pid
    returncode = process.poll()
    _abort_unidentified_process(process, process_group_id)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "launch_failed",
            "pid": process.pid,
            "returncode": returncode,
            "error": "could not establish PID start identity",
        },
    )
    raise TargetLaunchError("could not establish SGLang PID start identity")


def _read_process_start_identity(pid: int) -> str | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        stat_text = proc_stat.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        stat_text = ""
    if stat_text:
        closing = stat_text.rfind(")")
        fields = stat_text[closing + 2 :].split() if closing >= 0 else []
        if len(fields) > 19:
            return f"proc-start-ticks:{fields[19]}"

    try:
        ps_executable = Path("/bin/ps")
        if not ps_executable.is_file():
            ps_executable = Path("/usr/bin/ps")
        completed = subprocess.run(
            [str(ps_executable), "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(completed.stdout.split())
    if completed.returncode != 0 or not started:
        return None
    return f"ps-lstart:{started}"


def _abort_unidentified_process(process: subprocess.Popen[bytes], process_group_id: int) -> None:
    if process.poll() is not None:
        process.wait()
        return
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)
    process.wait()


def _argv_digest(argv: tuple[str, ...]) -> str:
    payload = json.dumps(list(argv), separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _safe_label(value: str) -> str:
    return _SAFE_LABEL.sub("-", value.strip()).strip(".-")[:64] or "target"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _build_manifest(
    build_id: str,
    workspace: Path,
    spec: BuildSpec,
    results: tuple[ExecutionResult, ...],
    status: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "build_id": build_id,
        "status": status,
        "workspace": str(workspace),
        "declared_command_count": len(spec.commands),
        "completed_command_count": len(results),
        "commands": [_execution_result_dict(result) for result in results],
    }


def _execution_result_dict(result: ExecutionResult) -> dict[str, object]:
    document = result.to_dict()
    document["cwd"] = str(result.cwd) if result.cwd is not None else None
    document["stdout_path"] = str(result.stdout_path)
    document["stderr_path"] = str(result.stderr_path)
    return document


__all__ = [
    "BuildCommandSpec",
    "BuildSpec",
    "ReadinessSpec",
    "SGLangTargetController",
    "ServerArgumentPatch",
    "ServiceHandle",
    "TargetBuildError",
    "TargetChangeSet",
    "TargetController",
    "TargetError",
    "TargetLaunchError",
    "TargetOwnershipError",
    "TargetReadinessError",
    "TargetSpec",
]
