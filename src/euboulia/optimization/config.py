"""Strict schema-version 2 and 3 configuration for iterative optimization."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import yaml

from euboulia.models import Framework, JSONScalar, JSONValue
from euboulia.optimization.contracts import EvaluationTierKind, _json_mapping

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_GIT_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_CONTAINER_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-fA-F]{64}")
_INPUT_REFERENCE = re.compile(r"\$\{([A-Za-z0-9][A-Za-z0-9._-]*)\}")
_LONG_OPTION = re.compile(r"--[A-Za-z0-9][A-Za-z0-9_-]*")
_PYTHON_EXECUTABLE = re.compile(r"(?:python|python\d+(?:\.\d+)*)")
_SGLANG_MODULES = frozenset(
    {"sglang.launch_server", "sglang.srt.entrypoints.http_server"}
)
_MANAGED_LAUNCH_OPTIONS = frozenset(
    {"--host", "--model-path", "--port", "--served-model-name"}
)


class OptimizationConfigError(ValueError):
    """Raised when an optimization document is malformed or unsafe."""


class ProfileProvider(StrEnum):
    IMPORTED = "imported"


class PlannerProvider(StrEnum):
    RULES = "rules"


class EvaluationDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class RecipeInputType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    GIT_COMMIT = "git_commit"
    CONTAINER_DIGEST = "container_digest"
    SHA256 = "sha256"


@dataclass(frozen=True, slots=True)
class RecipeInputConfig:
    name: str
    type: RecipeInputType
    required: bool = True


@dataclass(frozen=True, slots=True)
class OptimizationWorkloadConfig:
    """Compatibility view for one normalized workload point."""

    name: str
    model: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_prompts: int
    endpoint: str
    dataset: str = "random"


@dataclass(frozen=True, slots=True)
class ModelArtifactConfig:
    name: str
    path: str
    served_name: str
    revision: str
    weights_manifest_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    target: ModelArtifactConfig
    drafts: tuple[ModelArtifactConfig, ...] = ()

    def by_name(self, name: str) -> ModelArtifactConfig:
        if self.target.name == name:
            return self.target
        for draft in self.drafts:
            if draft.name == name:
                return draft
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class WorkloadPointConfig:
    name: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_prompts: int
    request_rate: str | float | None = None

@dataclass(frozen=True, slots=True)
class WorkloadSuiteConfig:
    name: str
    dataset: str
    request_rate: str | float
    points: tuple[WorkloadPointConfig, ...]

@dataclass(frozen=True, slots=True)
class RuntimeContainerConfig:
    image: str
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeComponentConfig:
    version: str | None = None
    revision: str | None = None
    digest: str | None = None
    path: Path | None = None
    dirty: bool | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeExpectedConfig:
    container: RuntimeContainerConfig | None = None
    components: Mapping[str, RuntimeComponentConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeCaptureConfig:
    collect_observed: bool = True
    fail_on_mismatch: bool = True
    require_observed: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeProvenanceConfig:
    expected: RuntimeExpectedConfig
    capture: RuntimeCaptureConfig = field(default_factory=RuntimeCaptureConfig)


@dataclass(frozen=True, slots=True)
class OptimizationBenchmarkConfig:
    mode: str
    base_args: tuple[str, ...] = ()
    result_filename: str = "result.json"
    parameters: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    name: str
    source_revision: str
    target_parameters: Mapping[str, JSONValue] = field(default_factory=dict)
    metric_values: Mapping[str, float] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class ProfileArtifactConfig:
    path: Path
    source: str
    nsys_report: str | None = None
    timestamp_unit: str = "us"


@dataclass(frozen=True, slots=True)
class ImportedProfilesConfig:
    provider: ProfileProvider
    paths: tuple[Path, ...]
    artifacts: tuple[ProfileArtifactConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class RulesPlannerConfig:
    provider: PlannerProvider
    patch_catalog: Path
    max_proposals_per_iteration: int = 1
    reject_duplicate_diffs: bool = True


@dataclass(frozen=True, slots=True)
class OptimizationCommandConfig:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float | None = None
    env: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetLaunchConfig:
    python: str = "python3"
    python_options: tuple[str, ...] = ()
    module: str = "sglang.launch_server"
    bind_host: str | None = None
    options: Mapping[str, JSONScalar] = field(default_factory=dict)
    extra_argv: tuple[str, ...] = ()
    env: Mapping[str, str | None] = field(default_factory=dict)

    def compile_argv(
        self,
        *,
        model_path: str,
        served_name: str,
        endpoint: str,
    ) -> tuple[str, ...]:
        """Compile the author-facing launch declaration into a safe argv tuple."""

        parsed_endpoint = urlsplit(endpoint)
        endpoint_host = parsed_endpoint.hostname
        if endpoint_host is None:  # pragma: no cover - endpoint validation guards this
            raise OptimizationConfigError("endpoint must contain a host")
        host = self.bind_host or endpoint_host
        port = parsed_endpoint.port
        if port is None:
            port = 443 if parsed_endpoint.scheme.casefold() == "https" else 80

        argv = [
            self.python,
            *self.python_options,
            "-m",
            self.module,
            "--model-path",
            model_path,
            "--served-model-name",
            served_name,
            "--host",
            host,
            "--port",
            str(port),
        ]
        for option, value in self.options.items():
            if value is None or value is False:
                continue
            argv.append(option)
            if value is not True:
                argv.append(str(value))
        argv.extend(self.extra_argv)
        return tuple(argv)


@dataclass(frozen=True, slots=True)
class TargetReadinessConfig:
    url: str
    timeout_seconds: float = 60.0
    interval_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class TargetBuildConfig:
    commands: tuple[OptimizationCommandConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagedTargetConfig:
    provider: Framework
    launch: TargetLaunchConfig
    readiness: TargetReadinessConfig
    shutdown_timeout_seconds: float
    gpus: tuple[str, ...]
    hardware: Mapping[str, JSONValue] = field(default_factory=dict)
    build: TargetBuildConfig | None = None
    provenance: Mapping[str, JSONValue] = field(default_factory=dict)
    runtime: RuntimeProvenanceConfig | None = None


@dataclass(frozen=True, slots=True)
class PromotionConfig:
    primary_points: tuple[str, ...]
    require_all_points_valid: bool = True
    min_relative_improvement: float = 0.0
    max_regression_per_point: float = 0.0
    noise_tolerance: float = 0.0


@dataclass(frozen=True, slots=True)
class EvaluationTierConfig:
    kind: EvaluationTierKind
    warmups: int
    repetitions: int
    timeout_seconds: float
    required_metrics: tuple[str, ...] = ()
    commands: tuple[OptimizationCommandConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class TieredEvaluationConfig:
    metric: str
    direction: EvaluationDirection
    tiers: tuple[EvaluationTierConfig, ...]
    min_relative_improvement: float = 0.0
    max_regression: float = 0.0
    noise_tolerance: float = 0.0
    baseline_value: float | None = None
    metrics_path: Path | None = None
    promotion: PromotionConfig | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    repository: Path
    root_dir: Path
    timeout_seconds: float = 120.0
    max_patch_bytes: int = 256 * 1024
    max_changed_files: int = 20
    max_changed_lines: int = 2_000


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    max_iterations: int
    max_wall_time_seconds: float
    max_consecutive_failures: int = 3
    no_improvement_patience: int = 5
    max_profile_bytes: int = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OptimizationPolicyConfig:
    profiles: ImportedProfilesConfig
    planner: RulesPlannerConfig
    evaluation: TieredEvaluationConfig
    budget: BudgetConfig
    workspace: WorkspaceConfig | None = None


@dataclass(frozen=True, slots=True)
class OptimizationExecutionConfig:
    artifacts_dir: Path
    experiment_ledger: Path
    event_ledger: Path
    memory: Path


@dataclass(frozen=True, slots=True)
class OptimizationConfig:
    """Fully normalized optimization configuration."""

    schema_version: int
    name: str
    framework: Framework
    models: ModelsConfig
    workload_suite: WorkloadSuiteConfig
    endpoint: str
    benchmark: OptimizationBenchmarkConfig
    baseline: BaselineConfig
    optimization: OptimizationPolicyConfig
    execution: OptimizationExecutionConfig
    source: Path
    target: ManagedTargetConfig | None = None
    input_bindings: Mapping[str, JSONScalar] = field(default_factory=dict)
    resolved_document: Mapping[str, JSONValue] = field(default_factory=dict)

    @property
    def target_launch_argv(self) -> tuple[str, ...]:
        """Return the compiled machine-facing argv for the managed target."""

        if self.target is None:
            return ()
        return self.target.launch.compile_argv(
            model_path=self.models.target.path,
            served_name=self.models.target.served_name,
            endpoint=self.endpoint,
        )

    @property
    def workload(self) -> OptimizationWorkloadConfig:
        """Return the first point for legacy single-point integrations."""

        point = self.workload_suite.points[0]
        return OptimizationWorkloadConfig(
            name=point.name,
            model=self.models.target.path,
            input_tokens=point.input_tokens,
            output_tokens=point.output_tokens,
            concurrency=point.concurrency,
            num_prompts=point.num_prompts,
            endpoint=self.endpoint,
            dataset=self.workload_suite.dataset,
        )


@dataclass(frozen=True, slots=True)
class OptimizationRecipeResolution:
    """One template after applying explicit values, possibly still unresolved."""

    source: Path
    name: str
    inputs: Mapping[str, RecipeInputConfig]
    bindings: Mapping[str, JSONScalar]
    missing_inputs: tuple[str, ...]
    resolved_document: Mapping[str, JSONValue]
    config: OptimizationConfig | None = None

    @property
    def resolved(self) -> bool:
        return not self.missing_inputs


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OptimizationConfigError(f"{path} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise OptimizationConfigError(f"{path} keys must be strings")
    return value


def _reject_unknown(mapping: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise OptimizationConfigError(f"{path} contains unknown field(s): {joined}")


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptimizationConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OptimizationConfigError(f"{path} must be an integer >= {minimum}")
    return value


def _number(value: object, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OptimizationConfigError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise OptimizationConfigError(f"{path} must be a finite number")
    if minimum is not None and result < minimum:
        raise OptimizationConfigError(f"{path} must be >= {minimum}")
    return result


def _optional_number(value: object, path: str) -> float | None:
    return None if value is None else _number(value, path)


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise OptimizationConfigError(f"{path} must be a boolean")
    return value


def _string_tuple(value: object, path: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OptimizationConfigError(f"{path} must be a list of strings")
    result = tuple(_string(item, f"{path}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not result:
        raise OptimizationConfigError(f"{path} must not be empty")
    return result


def _argv_tuple(value: object, path: str) -> tuple[str, ...]:
    argv = _string_tuple(value, path, allow_empty=False)
    for index, token in enumerate(argv):
        if "\x00" in token:
            raise OptimizationConfigError(f"{path}[{index}] contains a NUL byte")
    return argv


def _environment(value: object, path: str) -> Mapping[str, str | None]:
    raw = _mapping(value, path)
    result: dict[str, str | None] = {}
    for key, item in raw.items():
        name = _string(key, f"{path} key")
        if "=" in name or "\x00" in name:
            raise OptimizationConfigError(f"{path} contains an invalid environment name")
        if item is not None and (not isinstance(item, str) or "\x00" in item):
            raise OptimizationConfigError(f"{path}.{name} must be a string or null")
        result[name] = item
    return result


def _launch_tokens(value: object, path: str) -> tuple[str, ...]:
    tokens = _string_tuple(value, path)
    for index, token in enumerate(tokens):
        if "\x00" in token:
            raise OptimizationConfigError(f"{path}[{index}] contains a NUL byte")
    return tokens


def _launch_options(value: object, path: str) -> Mapping[str, JSONScalar]:
    raw = _mapping(value, path)
    result: dict[str, JSONScalar] = {}
    for raw_name, raw_value in raw.items():
        if _LONG_OPTION.fullmatch(raw_name) is None:
            raise OptimizationConfigError(
                f"{path} key {raw_name!r} must be a long option such as '--tp-size'"
            )
        if raw_name in _MANAGED_LAUNCH_OPTIONS:
            raise OptimizationConfigError(
                f"{path}.{raw_name} is generated from models.target and endpoint"
            )
        if isinstance(raw_value, str):
            if "\x00" in raw_value:
                raise OptimizationConfigError(f"{path}.{raw_name} contains a NUL byte")
            normalized: JSONScalar = raw_value
        elif raw_value is None or isinstance(raw_value, bool | int):
            normalized = raw_value
        elif isinstance(raw_value, float):
            if not math.isfinite(raw_value):
                raise OptimizationConfigError(f"{path}.{raw_name} must be finite")
            normalized = raw_value
        else:
            raise OptimizationConfigError(
                f"{path}.{raw_name} must be a string, number, boolean, or null; "
                "use target.launch.extra_argv for positional or repeated arguments"
            )
        result[raw_name] = normalized
    return dict(sorted(result.items()))


def _resolve_path(value: object, path: str, source: Path) -> Path:
    candidate = Path(_string(value, path)).expanduser()
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    return candidate.resolve()


def _workspace_relative_path(value: object, path: str) -> Path:
    candidate = Path(_string(value, path))
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise OptimizationConfigError(f"{path} must be a normalized workspace-relative path")
    return candidate


def _parse_json_mapping(value: object, path: str) -> Mapping[str, JSONValue]:
    raw = _mapping(value, path)
    try:
        return _json_mapping(raw, path)
    except (TypeError, ValueError) as exc:
        raise OptimizationConfigError(str(exc)) from exc


def _parse_workload(value: object) -> OptimizationWorkloadConfig:
    path = "workload"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "name",
            "model",
            "input_tokens",
            "output_tokens",
            "concurrency",
            "num_prompts",
            "endpoint",
            "dataset",
        },
        path,
    )
    return OptimizationWorkloadConfig(
        name=_string(raw.get("name"), "workload.name"),
        model=_string(raw.get("model"), "workload.model"),
        input_tokens=_integer(raw.get("input_tokens"), "workload.input_tokens", minimum=1),
        output_tokens=_integer(raw.get("output_tokens"), "workload.output_tokens", minimum=1),
        concurrency=_integer(raw.get("concurrency"), "workload.concurrency", minimum=1),
        num_prompts=_integer(raw.get("num_prompts"), "workload.num_prompts", minimum=1),
        endpoint=_string(raw.get("endpoint"), "workload.endpoint"),
        dataset=_string(raw.get("dataset", "random"), "workload.dataset"),
    )


def _safe_id(value: object, path: str) -> str:
    result = _string(value, path)
    if _SAFE_ID.fullmatch(result) is None or result in {".", ".."}:
        raise OptimizationConfigError(
            f"{path} must contain only letters, numbers, dot, underscore, and hyphen"
        )
    return result


def _name(
    raw: Mapping[str, object],
    path: str,
    *,
    default: str,
) -> str:
    """Read an optional human-facing name without treating it as content identity."""

    return _safe_id(raw.get("name", default), f"{path}.name")


def _point_alias(
    *,
    input_tokens: int,
    output_tokens: int,
    concurrency: int,
    num_prompts: int,
    request_rate: str | float | None,
) -> str:
    parts = [
        f"isl{input_tokens}",
        f"osl{output_tokens}",
        f"c{concurrency}",
        f"n{num_prompts}",
    ]
    if request_rate is not None:
        normalized_rate = str(request_rate).replace(".", "p")
        parts.append(f"r{normalized_rate}")
    return "-".join(parts)


def _optional_sha256(value: object, path: str) -> str | None:
    if value is None:
        return None
    result = _string(value, path).lower()
    if _SHA256.fullmatch(result) is None:
        raise OptimizationConfigError(f"{path} must contain exactly 64 hexadecimal characters")
    return result


def _parse_model_artifact(value: object, path: str) -> ModelArtifactConfig:
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {"name", "path", "served_name", "revision", "weights_manifest_sha256"},
        path,
    )
    model_path = _string(raw.get("path"), f"{path}.path")
    revision = _string(raw.get("revision"), f"{path}.revision")
    default_alias = "target"
    if not path.endswith(".target"):
        alias_seed = f"{revision}\0{model_path}".encode()
        default_alias = f"draft-{hashlib.sha256(alias_seed).hexdigest()[:12]}"
    return ModelArtifactConfig(
        name=_name(raw, path, default=default_alias),
        path=model_path,
        served_name=_string(raw.get("served_name", model_path), f"{path}.served_name"),
        revision=revision,
        weights_manifest_sha256=_optional_sha256(
            raw.get("weights_manifest_sha256"), f"{path}.weights_manifest_sha256"
        ),
    )


def _parse_models(value: object) -> ModelsConfig:
    path = "models"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"target", "drafts"}, path)
    target = _parse_model_artifact(raw.get("target"), f"{path}.target")
    raw_drafts = raw.get("drafts", [])
    if not isinstance(raw_drafts, list):
        raise OptimizationConfigError("models.drafts must be a list")
    drafts = tuple(
        _parse_model_artifact(item, f"models.drafts[{index}]")
        for index, item in enumerate(raw_drafts)
    )
    names = (target.name, *(item.name for item in drafts))
    if len(names) != len(set(names)):
        raise OptimizationConfigError("models target and draft names must be unique")
    return ModelsConfig(target=target, drafts=drafts)


def _parse_workload_suite(value: object) -> WorkloadSuiteConfig:
    path = "workload_suite"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"name", "dataset", "request_rate", "points"}, path)
    request_rate = _parse_request_rate(
        raw.get("request_rate", "inf"), "workload_suite.request_rate"
    )

    raw_points = raw.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise OptimizationConfigError("workload_suite.points must be a non-empty list")
    points: list[WorkloadPointConfig] = []
    for index, value_item in enumerate(raw_points):
        item_path = f"workload_suite.points[{index}]"
        item = _mapping(value_item, item_path)
        _reject_unknown(
            item,
            {
                "name",
                "input_tokens",
                "output_tokens",
                "concurrency",
                "num_prompts",
                "request_rate",
            },
            item_path,
        )
        input_tokens = _integer(
            item.get("input_tokens"), f"{item_path}.input_tokens", minimum=1
        )
        output_tokens = _integer(
            item.get("output_tokens"), f"{item_path}.output_tokens", minimum=1
        )
        concurrency = _integer(
            item.get("concurrency"), f"{item_path}.concurrency", minimum=1
        )
        num_prompts = _integer(
            item.get("num_prompts"), f"{item_path}.num_prompts", minimum=1
        )
        point_request_rate = (
            None
            if item.get("request_rate") is None
            else _parse_request_rate(item.get("request_rate"), f"{item_path}.request_rate")
        )
        points.append(
            WorkloadPointConfig(
                name=_name(
                    item,
                    item_path,
                    default=_point_alias(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        concurrency=concurrency,
                        num_prompts=num_prompts,
                        request_rate=point_request_rate,
                    ),
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                concurrency=concurrency,
                num_prompts=num_prompts,
                request_rate=point_request_rate,
            )
        )
    point_names = tuple(point.name for point in points)
    if len(point_names) != len(set(point_names)):
        raise OptimizationConfigError("workload_suite point names must be unique")
    return WorkloadSuiteConfig(
        name=_name(raw, path, default="workload"),
        dataset=_string(raw.get("dataset", "random"), "workload_suite.dataset"),
        request_rate=request_rate,
        points=tuple(points),
    )


def _parse_request_rate(value: object, path: str) -> str | float:
    request_rate_raw = value
    if isinstance(request_rate_raw, str):
        if request_rate_raw.casefold() != "inf":
            raise OptimizationConfigError(f"{path} string must be 'inf'")
        request_rate: str | float = "inf"
    else:
        request_rate = _number(request_rate_raw, path, minimum=0.001)
    return request_rate


def _legacy_models_and_suite(
    workload: OptimizationWorkloadConfig,
    target_raw: object,
) -> tuple[ModelsConfig, WorkloadSuiteConfig]:
    revision = workload.model
    if isinstance(target_raw, Mapping):
        provenance = target_raw.get("provenance")
        if isinstance(provenance, Mapping):
            declared_revision = provenance.get("model_revision")
            if isinstance(declared_revision, str) and declared_revision.strip():
                revision = declared_revision.strip()
    return (
        ModelsConfig(
            target=ModelArtifactConfig(
                name="target",
                path=workload.model,
                served_name=workload.model,
                revision=revision,
            )
        ),
        WorkloadSuiteConfig(
            name=workload.name,
            dataset=workload.dataset,
            request_rate="inf",
            points=(
                WorkloadPointConfig(
                    name=workload.name,
                    input_tokens=workload.input_tokens,
                    output_tokens=workload.output_tokens,
                    concurrency=workload.concurrency,
                    num_prompts=workload.num_prompts,
                ),
            ),
        ),
    )


def _parse_benchmark(value: object) -> OptimizationBenchmarkConfig:
    path = "benchmark"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"mode", "base_args", "result_filename", "parameters"}, path)
    result_filename = _string(
        raw.get("result_filename", "result.json"), "benchmark.result_filename"
    )
    result_path = Path(result_filename)
    if result_path.is_absolute() or len(result_path.parts) != 1:
        raise OptimizationConfigError("benchmark.result_filename must be a plain filename")
    return OptimizationBenchmarkConfig(
        mode=_string(raw.get("mode"), "benchmark.mode"),
        base_args=_string_tuple(raw.get("base_args", []), "benchmark.base_args"),
        result_filename=result_filename,
        parameters=_parse_json_mapping(raw.get("parameters", {}), "benchmark.parameters"),
    )


def _parse_baseline(value: object) -> BaselineConfig:
    path = "baseline"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {"name", "source_revision", "target_parameters", "metric_values"},
        path,
    )
    raw_values = _mapping(raw.get("metric_values", {}), "baseline.metric_values")
    metric_values: dict[str, float] = {}
    for point_name, metric_value in raw_values.items():
        normalized_name = _safe_id(
            point_name, f"baseline.metric_values key {point_name!r}"
        )
        metric_values[normalized_name] = _number(
            metric_value, f"baseline.metric_values.{normalized_name}"
        )
    return BaselineConfig(
        name=_name(raw, path, default="baseline"),
        source_revision=_string(raw.get("source_revision"), "baseline.source_revision"),
        target_parameters=_parse_json_mapping(
            raw.get("target_parameters", {}), "baseline.target_parameters"
        ),
        metric_values=metric_values,
    )


def _parse_profiles(value: object, source: Path) -> ImportedProfilesConfig:
    path = "optimization.profiles"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"provider", "paths", "artifacts"}, path)
    provider_value = _string(raw.get("provider"), f"{path}.provider")
    try:
        provider = ProfileProvider(provider_value)
    except ValueError as exc:
        raise OptimizationConfigError(
            "optimization.profiles.provider must be 'imported' in the current runtime"
        ) from exc
    has_paths = raw.get("paths") is not None
    has_artifacts = raw.get("artifacts") is not None
    if has_paths == has_artifacts:
        raise OptimizationConfigError(
            "optimization.profiles must contain exactly one of paths or artifacts"
        )
    artifacts: list[ProfileArtifactConfig] = []
    if has_paths:
        path_values = _string_tuple(raw.get("paths"), f"{path}.paths", allow_empty=False)
        for index, item in enumerate(path_values):
            artifact_path = _resolve_path(item, f"{path}.paths[{index}]", source)
            artifacts.append(
                ProfileArtifactConfig(
                    path=artifact_path,
                    source=_infer_profile_source(artifact_path),
                )
            )
    else:
        raw_artifacts = raw.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise OptimizationConfigError(f"{path}.artifacts must be a non-empty list")
        allowed_sources = {"torch_chrome_trace", "nsys_stats_csv", "ncu_csv"}
        for index, item in enumerate(raw_artifacts):
            item_path = f"{path}.artifacts[{index}]"
            artifact = _mapping(item, item_path)
            _reject_unknown(
                artifact, {"path", "source", "nsys_report", "timestamp_unit"}, item_path
            )
            profile_source = _string(artifact.get("source"), f"{item_path}.source")
            if profile_source not in allowed_sources:
                raise OptimizationConfigError(
                    f"{item_path}.source must be torch_chrome_trace, nsys_stats_csv, or ncu_csv"
                )
            raw_report = artifact.get("nsys_report")
            report = None if raw_report is None else _string(raw_report, f"{item_path}.nsys_report")
            if profile_source == "nsys_stats_csv" and report is None:
                raise OptimizationConfigError(
                    f"{item_path}.nsys_report is required for nsys_stats_csv"
                )
            if profile_source != "nsys_stats_csv" and report is not None:
                raise OptimizationConfigError(
                    f"{item_path}.nsys_report is valid only for nsys_stats_csv"
                )
            artifacts.append(
                ProfileArtifactConfig(
                    path=_resolve_path(artifact.get("path"), f"{item_path}.path", source),
                    source=profile_source,
                    nsys_report=report,
                    timestamp_unit=_string(
                        artifact.get("timestamp_unit", "us"), f"{item_path}.timestamp_unit"
                    ),
                )
            )
    paths = tuple(artifact.path for artifact in artifacts)
    if len(set(paths)) != len(paths):
        raise OptimizationConfigError("optimization.profiles.paths must not contain duplicates")
    return ImportedProfilesConfig(provider=provider, paths=paths, artifacts=tuple(artifacts))


def _infer_profile_source(path: Path) -> str:
    lowered = path.name.casefold()
    if lowered.endswith((".json", ".json.gz")):
        return "torch_chrome_trace"
    if "ncu" in lowered and lowered.endswith(".csv"):
        return "ncu_csv"
    raise OptimizationConfigError(
        f"cannot infer profiler format from {path.name!r}; use optimization.profiles.artifacts"
    )


def _parse_planner(value: object, source: Path) -> RulesPlannerConfig:
    path = "optimization.planner"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "provider",
            "patch_catalog",
            "max_proposals_per_iteration",
            "reject_duplicate_diffs",
        },
        path,
    )
    provider_value = _string(raw.get("provider"), f"{path}.provider")
    try:
        provider = PlannerProvider(provider_value)
    except ValueError as exc:
        raise OptimizationConfigError(
            "optimization.planner.provider must be 'rules' in the current runtime"
        ) from exc
    return RulesPlannerConfig(
        provider=provider,
        patch_catalog=_resolve_path(raw.get("patch_catalog"), f"{path}.patch_catalog", source),
        max_proposals_per_iteration=_integer(
            raw.get("max_proposals_per_iteration", 1),
            f"{path}.max_proposals_per_iteration",
            minimum=1,
        ),
        reject_duplicate_diffs=_boolean(
            raw.get("reject_duplicate_diffs", True), f"{path}.reject_duplicate_diffs"
        ),
    )


_TIER_RANK = {
    EvaluationTierKind.SMOKE: 0,
    EvaluationTierKind.CORRECTNESS: 1,
    EvaluationTierKind.PERFORMANCE: 2,
    EvaluationTierKind.ACCURACY: 3,
}


def _parse_commands(value: object, path: str) -> tuple[OptimizationCommandConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OptimizationConfigError(f"{path} must be a list")
    commands: list[OptimizationCommandConfig] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _mapping(item, item_path)
        _reject_unknown(raw, {"name", "argv", "timeout_seconds", "env"}, item_path)
        commands.append(
            OptimizationCommandConfig(
                name=_string(raw.get("name"), f"{item_path}.name"),
                argv=_argv_tuple(raw.get("argv"), f"{item_path}.argv"),
                timeout_seconds=(
                    None
                    if raw.get("timeout_seconds") is None
                    else _number(
                        raw.get("timeout_seconds"),
                        f"{item_path}.timeout_seconds",
                        minimum=0.001,
                    )
                ),
                env=_environment(raw.get("env", {}), f"{item_path}.env"),
            )
        )
    return tuple(commands)


def _local_http_url(value: object, path: str) -> str:
    url = _string(value, path)
    if "\x00" in url:
        raise OptimizationConfigError(f"{path} contains a NUL byte")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise OptimizationConfigError(f"{path} must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise OptimizationConfigError(f"{path} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise OptimizationConfigError(f"{path} must not contain credentials")
    if parsed.fragment:
        raise OptimizationConfigError(f"{path} must not contain a fragment")
    host = parsed.hostname
    if host is None:
        raise OptimizationConfigError(f"{path} must contain a host")
    if host.casefold() != "localhost":
        try:
            local = ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise OptimizationConfigError(f"{path} must use localhost or a loopback IP address")
    if port is not None and port < 1:
        raise OptimizationConfigError(f"{path} must contain a valid port")
    return url


def _parse_target_launch(value: object) -> TargetLaunchConfig:
    path = "target.launch"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "python",
            "python_options",
            "module",
            "bind_host",
            "options",
            "extra_argv",
            "env",
        },
        path,
    )
    python = _string(raw.get("python", "python3"), f"{path}.python")
    module = _string(raw.get("module", "sglang.launch_server"), f"{path}.module")
    if "\x00" in python:
        raise OptimizationConfigError(f"{path}.python contains a NUL byte")
    if "\x00" in module:
        raise OptimizationConfigError(f"{path}.module contains a NUL byte")
    if _PYTHON_EXECUTABLE.fullmatch(Path(python).name) is None:
        raise OptimizationConfigError(f"{path}.python must name a Python executable")
    if module not in _SGLANG_MODULES:
        raise OptimizationConfigError(f"{path}.module must name an approved SGLang server")
    python_options = _launch_tokens(
        raw.get("python_options", []), f"{path}.python_options"
    )
    if python_options not in {(), ("-u",)}:
        raise OptimizationConfigError(f"{path}.python_options must be [] or [-u]")
    bind_host = (
        None
        if raw.get("bind_host") is None
        else _string(raw.get("bind_host"), f"{path}.bind_host")
    )
    if bind_host is not None and (
        "\x00" in bind_host or any(char.isspace() for char in bind_host)
    ):
        raise OptimizationConfigError(
            f"{path}.bind_host must not contain whitespace or NUL bytes"
        )
    options = _launch_options(raw.get("options", {}), f"{path}.options")
    extra_argv = _launch_tokens(raw.get("extra_argv", []), f"{path}.extra_argv")
    extra_options: set[str] = set()
    for index, token in enumerate(extra_argv):
        option = token.partition("=")[0]
        if _LONG_OPTION.fullmatch(option) is None:
            continue
        if option in _MANAGED_LAUNCH_OPTIONS:
            raise OptimizationConfigError(
                f"{path}.extra_argv[{index}] must not override an option generated "
                "from models.target or endpoint"
            )
        extra_options.add(option)
    duplicated_options = sorted(set(options).intersection(extra_options))
    if duplicated_options:
        raise OptimizationConfigError(
            f"{path}.options and {path}.extra_argv both declare: "
            + ", ".join(duplicated_options)
        )
    return TargetLaunchConfig(
        python=python,
        python_options=python_options,
        module=module,
        bind_host=bind_host,
        options=options,
        extra_argv=extra_argv,
        env=_environment(raw.get("env", {}), f"{path}.env"),
    )


def _parse_target_readiness(value: object) -> TargetReadinessConfig:
    path = "target.readiness"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"url", "timeout_seconds", "interval_seconds"}, path)
    return TargetReadinessConfig(
        url=_local_http_url(raw.get("url"), f"{path}.url"),
        timeout_seconds=_number(
            raw.get("timeout_seconds", 60.0),
            f"{path}.timeout_seconds",
            minimum=0.001,
        ),
        interval_seconds=_number(
            raw.get("interval_seconds", 1.0),
            f"{path}.interval_seconds",
            minimum=0.001,
        ),
    )


def _parse_target_gpus(value: object) -> tuple[str, ...]:
    path = "target.gpus"
    if not isinstance(value, list) or not value:
        raise OptimizationConfigError(f"{path} must be a non-empty list of GPU IDs")
    result: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(item, bool) or not isinstance(item, int | str):
            raise OptimizationConfigError(f"{item_path} must be an integer or string GPU ID")
        gpu_id = str(item)
        if not gpu_id or any(character.isspace() for character in gpu_id):
            raise OptimizationConfigError(f"{item_path} must not be empty or contain whitespace")
        if "," in gpu_id or "\x00" in gpu_id:
            raise OptimizationConfigError(f"{item_path} must not contain a comma or NUL byte")
        result.append(gpu_id)
    if len(set(result)) != len(result):
        raise OptimizationConfigError(f"{path} must not contain duplicate GPU IDs")
    return tuple(result)


def _parse_target_build(value: object) -> TargetBuildConfig:
    path = "target.build"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"commands"}, path)
    return TargetBuildConfig(commands=_parse_commands(raw.get("commands"), f"{path}.commands"))


def _runtime_digest(value: object, path: str) -> str | None:
    if value is None:
        return None
    result = _string(value, path).lower()
    hexadecimal = result.removeprefix("sha256:")
    if _SHA256.fullmatch(hexadecimal) is None:
        raise OptimizationConfigError(f"{path} must be a sha256 digest")
    return f"sha256:{hexadecimal}"


def _parse_runtime_component(
    value: object,
    path: str,
    source: Path,
) -> RuntimeComponentConfig:
    raw = _mapping(value, path)
    _reject_unknown(raw, {"version", "revision", "digest", "path", "dirty", "metadata"}, path)
    return RuntimeComponentConfig(
        version=(
            None if raw.get("version") is None else _string(raw.get("version"), f"{path}.version")
        ),
        revision=(
            None
            if raw.get("revision") is None
            else _string(raw.get("revision"), f"{path}.revision")
        ),
        digest=_runtime_digest(raw.get("digest"), f"{path}.digest"),
        path=(
            None
            if raw.get("path") is None
            else _resolve_path(raw.get("path"), f"{path}.path", source)
        ),
        dirty=(None if raw.get("dirty") is None else _boolean(raw.get("dirty"), f"{path}.dirty")),
        metadata=_parse_json_mapping(raw.get("metadata", {}), f"{path}.metadata"),
    )


def _parse_runtime(value: object, source: Path) -> RuntimeProvenanceConfig:
    path = "target.runtime"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"expected", "capture"}, path)
    expected_raw = _mapping(raw.get("expected", {}), f"{path}.expected")
    _reject_unknown(expected_raw, {"container", "components"}, f"{path}.expected")
    container_raw = expected_raw.get("container")
    container: RuntimeContainerConfig | None = None
    if container_raw is not None:
        container_mapping = _mapping(container_raw, f"{path}.expected.container")
        _reject_unknown(container_mapping, {"image", "digest"}, f"{path}.expected.container")
        container = RuntimeContainerConfig(
            image=_string(container_mapping.get("image"), f"{path}.expected.container.image"),
            digest=_runtime_digest(
                container_mapping.get("digest"), f"{path}.expected.container.digest"
            ),
        )
    components_raw = _mapping(expected_raw.get("components", {}), f"{path}.expected.components")
    components: dict[str, RuntimeComponentConfig] = {}
    for component_name, component_value in components_raw.items():
        normalized_name = _safe_id(
            component_name, f"{path}.expected.components key {component_name!r}"
        )
        components[normalized_name] = _parse_runtime_component(
            component_value,
            f"{path}.expected.components.{normalized_name}",
            source,
        )
    if container is None and not components:
        raise OptimizationConfigError(
            "target.runtime.expected must declare a container or at least one component"
        )
    capture_raw = _mapping(raw.get("capture", {}), f"{path}.capture")
    _reject_unknown(
        capture_raw,
        {"collect_observed", "fail_on_mismatch", "require_observed"},
        f"{path}.capture",
    )
    return RuntimeProvenanceConfig(
        expected=RuntimeExpectedConfig(container=container, components=components),
        capture=RuntimeCaptureConfig(
            collect_observed=_boolean(
                capture_raw.get("collect_observed", True), f"{path}.capture.collect_observed"
            ),
            fail_on_mismatch=_boolean(
                capture_raw.get("fail_on_mismatch", True), f"{path}.capture.fail_on_mismatch"
            ),
            require_observed=_boolean(
                capture_raw.get("require_observed", False), f"{path}.capture.require_observed"
            ),
        ),
    )


def _parse_target(
    value: object,
    framework: Framework,
    *,
    source: Path,
    schema_version: int,
) -> ManagedTargetConfig:
    path = "target"
    raw = _mapping(value, path)
    allowed = {
        "provider",
        "launch",
        "readiness",
        "shutdown_timeout_seconds",
        "gpus",
        "hardware",
        "build",
    }
    allowed.update({"provenance"} if schema_version == 2 else {"runtime"})
    _reject_unknown(raw, allowed, path)
    provider_value = _string(raw.get("provider"), f"{path}.provider").lower()
    if provider_value != Framework.SGLANG.value:
        raise OptimizationConfigError("target.provider must be 'sglang' in the current runtime")
    provider = Framework(provider_value)
    if provider is not framework:
        raise OptimizationConfigError("target.provider must match framework")
    launch = _parse_target_launch(raw.get("launch"))
    if "CUDA_VISIBLE_DEVICES" in launch.env:
        raise OptimizationConfigError(
            "target.launch.env must not override target.gpus via CUDA_VISIBLE_DEVICES"
        )
    runtime = None if raw.get("runtime") is None else _parse_runtime(raw.get("runtime"), source)
    if schema_version == 3 and runtime is None:
        raise OptimizationConfigError("target.runtime is required for schema_version 3")
    return ManagedTargetConfig(
        provider=provider,
        launch=launch,
        readiness=_parse_target_readiness(raw.get("readiness")),
        shutdown_timeout_seconds=_number(
            raw.get("shutdown_timeout_seconds", 30.0),
            f"{path}.shutdown_timeout_seconds",
            minimum=0.001,
        ),
        gpus=_parse_target_gpus(raw.get("gpus")),
        hardware=_parse_json_mapping(raw.get("hardware", {}), f"{path}.hardware"),
        build=(None if raw.get("build") is None else _parse_target_build(raw.get("build"))),
        provenance=_parse_json_mapping(raw.get("provenance", {}), f"{path}.provenance"),
        runtime=runtime,
    )


def _parse_evaluation_tiers(value: object) -> tuple[EvaluationTierConfig, ...]:
    path = "optimization.evaluation.tiers"
    if not isinstance(value, list) or not value:
        raise OptimizationConfigError(f"{path} must be a non-empty list")
    tiers: list[EvaluationTierConfig] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _mapping(item, item_path)
        _reject_unknown(
            raw,
            {
                "kind",
                "warmups",
                "repetitions",
                "timeout_seconds",
                "required_metrics",
                "commands",
            },
            item_path,
        )
        try:
            kind = EvaluationTierKind(_string(raw.get("kind"), f"{item_path}.kind"))
        except ValueError as exc:
            raise OptimizationConfigError(
                f"{item_path}.kind must be smoke, correctness, performance, or accuracy"
            ) from exc
        tiers.append(
            EvaluationTierConfig(
                kind=kind,
                warmups=_integer(raw.get("warmups", 0), f"{item_path}.warmups"),
                repetitions=_integer(
                    raw.get("repetitions", 1), f"{item_path}.repetitions", minimum=1
                ),
                timeout_seconds=_number(
                    raw.get("timeout_seconds"), f"{item_path}.timeout_seconds", minimum=0.001
                ),
                required_metrics=_string_tuple(
                    raw.get("required_metrics", []), f"{item_path}.required_metrics"
                ),
                commands=_parse_commands(raw.get("commands"), f"{item_path}.commands"),
            )
        )
    kinds = tuple(tier.kind for tier in tiers)
    if len(set(kinds)) != len(kinds):
        raise OptimizationConfigError(f"{path} must not contain duplicate tier kinds")
    ranks = tuple(_TIER_RANK[kind] for kind in kinds)
    if tuple(sorted(ranks)) != ranks:
        raise OptimizationConfigError(
            f"{path} must be ordered smoke, correctness, performance, accuracy"
        )
    if (
        EvaluationTierKind.CORRECTNESS not in kinds
        or EvaluationTierKind.PERFORMANCE not in kinds
    ):
        raise OptimizationConfigError(
            f"{path} must include correctness and performance"
        )
    return tuple(tiers)


def _parse_promotion(value: object, point_names: tuple[str, ...]) -> PromotionConfig:
    path = "optimization.evaluation.promotion"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "primary_points",
            "require_all_points_valid",
            "min_relative_improvement",
            "max_regression_per_point",
            "noise_tolerance",
        },
        path,
    )
    primary_points = tuple(
        _safe_id(item, f"{path}.primary_points[{index}]")
        for index, item in enumerate(
            _string_tuple(
                raw.get("primary_points", list(point_names)),
                f"{path}.primary_points",
                allow_empty=False,
            )
        )
    )
    if len(primary_points) != len(set(primary_points)):
        raise OptimizationConfigError(f"{path}.primary_points must not contain duplicates")
    unknown = sorted(set(primary_points) - set(point_names))
    if unknown:
        raise OptimizationConfigError(
            f"{path}.primary_points references unknown workload point(s): {', '.join(unknown)}"
        )
    return PromotionConfig(
        primary_points=primary_points,
        require_all_points_valid=_boolean(
            raw.get("require_all_points_valid", True), f"{path}.require_all_points_valid"
        ),
        min_relative_improvement=_number(
            raw.get("min_relative_improvement", 0.0),
            f"{path}.min_relative_improvement",
            minimum=0.0,
        ),
        max_regression_per_point=_number(
            raw.get("max_regression_per_point", 0.0),
            f"{path}.max_regression_per_point",
            minimum=0.0,
        ),
        noise_tolerance=_number(
            raw.get("noise_tolerance", 0.0), f"{path}.noise_tolerance", minimum=0.0
        ),
    )


def _parse_evaluation(
    value: object,
    *,
    point_names: tuple[str, ...],
    schema_version: int,
) -> TieredEvaluationConfig:
    path = "optimization.evaluation"
    raw = _mapping(value, path)
    allowed = {"metric", "direction", "tiers", "metrics_path"}
    if schema_version == 2:
        allowed.update(
            {
                "min_relative_improvement",
                "max_regression",
                "noise_tolerance",
                "baseline_value",
            }
        )
    else:
        allowed.add("promotion")
    _reject_unknown(raw, allowed, path)
    try:
        direction = EvaluationDirection(_string(raw.get("direction"), f"{path}.direction"))
    except ValueError as exc:
        raise OptimizationConfigError(
            "optimization.evaluation.direction must be 'maximize' or 'minimize'"
        ) from exc
    min_relative_improvement = _number(
        raw.get("min_relative_improvement", 0.0),
        f"{path}.min_relative_improvement",
        minimum=0.0,
    )
    max_regression = _number(raw.get("max_regression", 0.0), f"{path}.max_regression", minimum=0.0)
    noise_tolerance = _number(
        raw.get("noise_tolerance", 0.0), f"{path}.noise_tolerance", minimum=0.0
    )
    promotion = (
        _parse_promotion(raw.get("promotion", {}), point_names)
        if schema_version == 3
        else PromotionConfig(
            primary_points=point_names,
            min_relative_improvement=min_relative_improvement,
            max_regression_per_point=max_regression,
            noise_tolerance=noise_tolerance,
        )
    )
    return TieredEvaluationConfig(
        metric=_string(raw.get("metric"), f"{path}.metric"),
        direction=direction,
        tiers=_parse_evaluation_tiers(raw.get("tiers")),
        min_relative_improvement=min_relative_improvement,
        max_regression=max_regression,
        noise_tolerance=noise_tolerance,
        baseline_value=_optional_number(raw.get("baseline_value"), f"{path}.baseline_value"),
        metrics_path=(
            None
            if raw.get("metrics_path") is None
            else _workspace_relative_path(raw.get("metrics_path"), f"{path}.metrics_path")
        ),
        promotion=promotion,
    )


def _parse_budget(value: object) -> BudgetConfig:
    path = "optimization.budget"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "max_iterations",
            "max_wall_time_seconds",
            "max_consecutive_failures",
            "no_improvement_patience",
            "max_profile_bytes",
        },
        path,
    )
    max_iterations = _integer(raw.get("max_iterations"), f"{path}.max_iterations", minimum=1)
    patience = _integer(
        raw.get("no_improvement_patience", min(5, max_iterations)),
        f"{path}.no_improvement_patience",
        minimum=1,
    )
    if patience > max_iterations:
        raise OptimizationConfigError(
            "optimization.budget.no_improvement_patience must not exceed max_iterations"
        )
    return BudgetConfig(
        max_iterations=max_iterations,
        max_wall_time_seconds=_number(
            raw.get("max_wall_time_seconds"), f"{path}.max_wall_time_seconds", minimum=0.001
        ),
        max_consecutive_failures=_integer(
            raw.get("max_consecutive_failures", 3),
            f"{path}.max_consecutive_failures",
            minimum=1,
        ),
        no_improvement_patience=patience,
        max_profile_bytes=_integer(
            raw.get("max_profile_bytes", 2 * 1024 * 1024 * 1024),
            f"{path}.max_profile_bytes",
            minimum=1,
        ),
    )


def _parse_workspace(value: object, source: Path) -> WorkspaceConfig:
    path = "optimization.workspace"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "repository",
            "root_dir",
            "timeout_seconds",
            "max_patch_bytes",
            "max_changed_files",
            "max_changed_lines",
        },
        path,
    )
    return WorkspaceConfig(
        repository=_resolve_path(raw.get("repository"), f"{path}.repository", source),
        root_dir=_resolve_path(raw.get("root_dir"), f"{path}.root_dir", source),
        timeout_seconds=_number(
            raw.get("timeout_seconds", 120.0), f"{path}.timeout_seconds", minimum=0.001
        ),
        max_patch_bytes=_integer(
            raw.get("max_patch_bytes", 256 * 1024), f"{path}.max_patch_bytes", minimum=1
        ),
        max_changed_files=_integer(
            raw.get("max_changed_files", 20), f"{path}.max_changed_files", minimum=1
        ),
        max_changed_lines=_integer(
            raw.get("max_changed_lines", 2_000), f"{path}.max_changed_lines", minimum=1
        ),
    )


def _parse_optimization(
    value: object,
    source: Path,
    *,
    point_names: tuple[str, ...],
    schema_version: int,
) -> OptimizationPolicyConfig:
    path = "optimization"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"profiles", "planner", "evaluation", "budget", "workspace"}, path)
    return OptimizationPolicyConfig(
        profiles=_parse_profiles(raw.get("profiles"), source),
        planner=_parse_planner(raw.get("planner"), source),
        evaluation=_parse_evaluation(
            raw.get("evaluation"), point_names=point_names, schema_version=schema_version
        ),
        budget=_parse_budget(raw.get("budget")),
        workspace=(
            None if raw.get("workspace") is None else _parse_workspace(raw.get("workspace"), source)
        ),
    )


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def _parse_execution(value: object, source: Path) -> OptimizationExecutionConfig:
    path = "execution"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"artifacts_dir", "ledger", "events", "memory"}, path)
    artifacts = _resolve_path(raw.get("artifacts_dir"), "execution.artifacts_dir", source)
    filesystem_root = Path(artifacts.anchor)
    if artifacts in {filesystem_root, Path.home().resolve(), source.parent.resolve()}:
        raise OptimizationConfigError(
            "execution.artifacts_dir must not be the filesystem root, home directory, "
            "or campaign directory"
        )

    def storage_path(key: str, default_name: str) -> Path:
        raw_value = raw.get(key)
        if raw_value is None:
            result = artifacts / default_name
        else:
            result = _resolve_path(raw_value, f"execution.{key}", source)
        if not _inside(result, artifacts):
            raise OptimizationConfigError(
                f"execution.{key} must be located inside execution.artifacts_dir"
            )
        return result

    return OptimizationExecutionConfig(
        artifacts_dir=artifacts,
        experiment_ledger=storage_path("ledger", "experiments.jsonl"),
        event_ledger=storage_path("events", "events.jsonl"),
        memory=storage_path("memory", "memory.sqlite3"),
    )


def _parse_optimization_document(
    document_object: object,
    source: Path,
    *,
    input_bindings: Mapping[str, JSONScalar],
    resolved_document: Mapping[str, JSONValue],
) -> OptimizationConfig:
    """Parse one fully resolved document into the normalized runtime model."""

    document = _mapping(document_object, "document")
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1)
    if schema_version not in {2, 3}:
        raise OptimizationConfigError(
            f"unsupported optimization schema_version {schema_version}; expected 2 or 3"
        )
    allowed = {
        "schema_version",
        "name",
        "framework",
        "benchmark",
        "baseline",
        "optimization",
        "execution",
        "target",
    }
    allowed.update(
        {"workload"} if schema_version == 2 else {"models", "workload_suite", "endpoint"}
    )
    _reject_unknown(document, allowed, "document")
    framework_value = _string(document.get("framework"), "framework").lower()
    if framework_value not in {Framework.SGLANG.value, Framework.VLLM.value}:
        raise OptimizationConfigError("framework must be 'sglang' or 'vllm'")
    framework = Framework(framework_value)
    if schema_version == 2:
        workload = _parse_workload(document.get("workload"))
        models, workload_suite = _legacy_models_and_suite(workload, document.get("target"))
        endpoint = workload.endpoint
    else:
        models = _parse_models(document.get("models"))
        workload_suite = _parse_workload_suite(document.get("workload_suite"))
        endpoint = _string(document.get("endpoint"), "endpoint")
    if document.get("target") is not None:
        endpoint = _local_http_url(
            endpoint, "endpoint" if schema_version == 3 else "workload.endpoint"
        )
    target = (
        None
        if document.get("target") is None
        else _parse_target(
            document.get("target"),
            framework,
            source=source,
            schema_version=schema_version,
        )
    )
    baseline = _parse_baseline(document.get("baseline"))
    if schema_version == 3 and target is not None and baseline.target_parameters:
        raise OptimizationConfigError(
            "baseline.target_parameters must be empty for a managed schema_version 3 "
            "target; put SGLang server switches in target.launch.options"
        )
    point_names = tuple(point.name for point in workload_suite.points)
    if schema_version == 3 and target is None:
        missing_values = sorted(set(point_names) - set(baseline.metric_values))
        unknown_values = sorted(set(baseline.metric_values) - set(point_names))
        if missing_values:
            raise OptimizationConfigError(
                "baseline.metric_values is missing workload point(s): " + ", ".join(missing_values)
            )
        if unknown_values:
            raise OptimizationConfigError(
                "baseline.metric_values contains unknown workload point(s): "
                + ", ".join(unknown_values)
            )
    return OptimizationConfig(
        schema_version=schema_version,
        name=_string(document.get("name"), "name"),
        framework=framework,
        models=models,
        workload_suite=workload_suite,
        endpoint=endpoint,
        benchmark=_parse_benchmark(document.get("benchmark")),
        baseline=baseline,
        optimization=_parse_optimization(
            document.get("optimization"),
            source,
            point_names=point_names,
            schema_version=schema_version,
        ),
        execution=_parse_execution(document.get("execution"), source),
        source=source,
        target=target,
        input_bindings=dict(input_bindings),
        resolved_document=dict(resolved_document),
    )


def _read_yaml_mapping(path: Path, label: str) -> Mapping[str, JSONValue]:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OptimizationConfigError(f"{label} does not exist: {path}") from exc
    except yaml.YAMLError as exc:
        raise OptimizationConfigError(f"invalid YAML in {path}: {exc}") from exc
    raw = _mapping(loaded, label)
    return cast(Mapping[str, JSONValue], dict(raw))


def _parse_recipe_inputs(value: object) -> Mapping[str, RecipeInputConfig]:
    if value is None:
        return {}
    path = "inputs"
    raw = _mapping(value, path)
    result: dict[str, RecipeInputConfig] = {}
    for raw_name, raw_definition in raw.items():
        name = _safe_id(raw_name, f"{path} key {raw_name!r}")
        definition_path = f"{path}.{name}"
        definition = _mapping(raw_definition, definition_path)
        _reject_unknown(definition, {"type", "required"}, definition_path)
        raw_type = _string(definition.get("type"), f"{definition_path}.type")
        try:
            input_type = RecipeInputType(raw_type)
        except ValueError as exc:
            choices = ", ".join(item.value for item in RecipeInputType)
            raise OptimizationConfigError(
                f"{definition_path}.type must be one of: {choices}"
            ) from exc
        result[name] = RecipeInputConfig(
            name=name,
            type=input_type,
            required=_boolean(
                definition.get("required", True), f"{definition_path}.required"
            ),
        )
    return dict(sorted(result.items()))


def _normalize_input_value(
    definition: RecipeInputConfig,
    value: object,
    path: str,
) -> JSONScalar:
    input_type = definition.type
    if input_type is RecipeInputType.STRING:
        return _string(value, path)
    if input_type is RecipeInputType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise OptimizationConfigError(f"{path} must be an integer")
        return value
    if input_type is RecipeInputType.NUMBER:
        return _number(value, path)
    if input_type is RecipeInputType.BOOLEAN:
        return _boolean(value, path)
    normalized = _string(value, path)
    if input_type is RecipeInputType.GIT_COMMIT:
        if _GIT_COMMIT.fullmatch(normalized) is None:
            raise OptimizationConfigError(
                f"{path} must be a full 40- or 64-character hexadecimal Git commit"
            )
        if set(normalized) == {"0"}:
            raise OptimizationConfigError(f"{path} must not be an all-zero placeholder")
        return normalized.casefold()
    if input_type is RecipeInputType.CONTAINER_DIGEST:
        if _CONTAINER_DIGEST.fullmatch(normalized) is None:
            raise OptimizationConfigError(
                f"{path} must be an immutable image reference such as "
                "'registry/repository@sha256:<64 hex characters>'"
            )
        repository, digest = normalized.rsplit("@sha256:", 1)
        if set(digest) == {"0"}:
            raise OptimizationConfigError(f"{path} must not contain an all-zero digest")
        return f"{repository}@sha256:{digest.casefold()}"
    if _SHA256.fullmatch(normalized) is None:
        raise OptimizationConfigError(f"{path} must be a 64-character SHA-256 digest")
    if set(normalized) == {"0"}:
        raise OptimizationConfigError(f"{path} must not be an all-zero placeholder")
    return normalized.casefold()


def _collect_input_references(
    value: JSONValue,
    path: str,
    references: set[str],
) -> None:
    if isinstance(value, str):
        match = _INPUT_REFERENCE.fullmatch(value)
        if match is not None:
            references.add(match.group(1))
        elif _INPUT_REFERENCE.search(value) is not None or "${" in value:
            raise OptimizationConfigError(
                f"{path} input reference must occupy the entire scalar value"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_input_references(item, f"{path}[{index}]", references)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_input_references(item, f"{path}.{key}", references)


def _substitute_inputs(
    value: JSONValue,
    bindings: Mapping[str, JSONScalar],
) -> JSONValue:
    if isinstance(value, str):
        match = _INPUT_REFERENCE.fullmatch(value)
        if match is None:
            return value
        return bindings.get(match.group(1), value)
    if isinstance(value, list):
        return [_substitute_inputs(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_inputs(item, bindings) for key, item in value.items()}
    return value


def resolve_optimization_config(
    path: str | Path,
    values: str | Path | None = None,
    *,
    allow_unresolved: bool = False,
) -> OptimizationRecipeResolution:
    """Bind a recipe template and optionally return its unresolved input inventory."""

    source = Path(path).expanduser().resolve()
    template = dict(_read_yaml_mapping(source, "document"))
    inputs = _parse_recipe_inputs(template.pop("inputs", None))
    values_source = None if values is None else Path(values).expanduser().resolve()
    supplied = (
        {}
        if values_source is None
        else dict(_read_yaml_mapping(values_source, "values document"))
    )
    unknown_values = sorted(set(supplied) - set(inputs))
    if unknown_values:
        raise OptimizationConfigError(
            "values document contains undeclared input(s): " + ", ".join(unknown_values)
        )

    references: set[str] = set()
    for key, item in template.items():
        _collect_input_references(item, f"document.{key}", references)
    undeclared_references = sorted(references - set(inputs))
    if undeclared_references:
        raise OptimizationConfigError(
            "recipe references undeclared input(s): " + ", ".join(undeclared_references)
        )
    unused_inputs = sorted(set(inputs) - references)
    if unused_inputs:
        raise OptimizationConfigError(
            "recipe declares unused input(s): " + ", ".join(unused_inputs)
        )

    bindings: dict[str, JSONScalar] = {}
    missing: list[str] = []
    for name, definition in inputs.items():
        if name in supplied:
            bindings[name] = _normalize_input_value(
                definition, supplied[name], f"values.{name}"
            )
        elif definition.required:
            missing.append(name)
        else:
            bindings[name] = None

    resolved_value = _substitute_inputs(template, bindings)
    if not isinstance(resolved_value, dict):  # pragma: no cover - template is a mapping
        raise AssertionError("resolved optimization recipe must remain a mapping")
    resolved_document = resolved_value
    name_value = resolved_document.get("name")
    recipe_name = name_value if isinstance(name_value, str) else source.stem
    missing_inputs = tuple(sorted(missing))
    if missing_inputs and not allow_unresolved:
        raise OptimizationConfigError(
            "missing required input binding(s): " + ", ".join(missing_inputs)
        )
    config = (
        None
        if missing_inputs
        else _parse_optimization_document(
            resolved_document,
            source,
            input_bindings=bindings,
            resolved_document=resolved_document,
        )
    )
    return OptimizationRecipeResolution(
        source=source,
        name=recipe_name,
        inputs=inputs,
        bindings=dict(sorted(bindings.items())),
        missing_inputs=missing_inputs,
        resolved_document=resolved_document,
        config=config,
    )


def load_optimization_config(
    path: str | Path,
    values: str | Path | None = None,
) -> OptimizationConfig:
    """Load a fully bound strict v2 or v3 input into the normalized runtime model."""

    resolution = resolve_optimization_config(path, values)
    if resolution.config is None:  # pragma: no cover - strict resolution raises first
        raise AssertionError("strict recipe resolution did not produce a configuration")
    return resolution.config


def optimization_execution_lock_issues(config: OptimizationConfig) -> tuple[str, ...]:
    """Return immutable-identity problems that must block a managed execution."""

    target = config.target
    if target is None:
        return ()
    issues: list[str] = []
    source_revision = config.baseline.source_revision
    if _GIT_COMMIT.fullmatch(source_revision) is None or set(source_revision) == {"0"}:
        issues.append("baseline.source_revision must be a full Git commit")

    if config.schema_version < 3:
        return tuple(issues)
    runtime = target.runtime
    if runtime is None:  # pragma: no cover - schema-v3 parsing already requires this
        issues.append("target.runtime is required")
        return tuple(issues)
    container = runtime.expected.container
    if container is None:
        issues.append("target.runtime.expected.container must pin an image digest")
    else:
        embedded_digest: str | None = None
        if "@sha256:" in container.image:
            _, raw_embedded_digest = container.image.rsplit("@", 1)
            embedded_digest = _runtime_digest(
                raw_embedded_digest, "container image digest"
            )
            if embedded_digest is None:  # pragma: no cover - value is not None
                raise AssertionError("embedded container digest did not normalize")
            if embedded_digest.removeprefix("sha256:") == "0" * 64:
                issues.append("container image digest must not be an all-zero placeholder")
        if embedded_digest is None and container.digest is None:
            issues.append(
                "target.runtime.expected.container must use image@sha256 or declare digest"
            )
        if container.digest == "sha256:" + "0" * 64:
            issues.append("container.digest must not be an all-zero placeholder")
        if (
            embedded_digest is not None
            and container.digest is not None
            and embedded_digest != container.digest
        ):
            issues.append("container image digest and container.digest must match")

    sglang = runtime.expected.components.get("sglang")
    if sglang is None or sglang.revision is None:
        issues.append("target.runtime.expected.components.sglang.revision is required")
    elif sglang.revision != source_revision:
        issues.append("SGLang runtime revision must equal baseline.source_revision")

    for name, component in sorted(runtime.expected.components.items()):
        if component.revision is not None and (
            _GIT_COMMIT.fullmatch(component.revision) is None
            or set(component.revision) == {"0"}
        ):
            issues.append(
                f"target.runtime.expected.components.{name}.revision must be a full Git commit"
            )
        if (
            component.path is not None
            and component.revision is None
            and component.digest is None
        ):
            issues.append(
                f"target.runtime.expected.components.{name} has a source path but no "
                "revision or digest"
            )
        if (component.path is not None or name == "sglang") and component.dirty is not False:
            issues.append(
                f"target.runtime.expected.components.{name}.dirty must be false"
            )
    for model in (config.models.target, *config.models.drafts):
        if model.weights_manifest_sha256 == "0" * 64:
            issues.append(
                f"models.{model.name}.weights_manifest_sha256 must not be an all-zero placeholder"
            )
        revision_is_commit = (
            _GIT_COMMIT.fullmatch(model.revision) is not None
            and set(model.revision) != {"0"}
        )
        manifest_is_pinned = (
            model.weights_manifest_sha256 is not None
            and model.weights_manifest_sha256 != "0" * 64
        )
        if not revision_is_commit and not manifest_is_pinned:
            issues.append(
                f"models.{model.name} must pin a full revision commit or weights manifest"
            )
    return tuple(issues)


def require_optimization_execution_lock(config: OptimizationConfig) -> None:
    """Fail before active execution when content identity remains floating."""

    issues = optimization_execution_lock_issues(config)
    if issues:
        raise OptimizationConfigError("execution recipe is not locked: " + "; ".join(issues))


def dump_resolved_optimization_config(config: OptimizationConfig) -> str:
    """Serialize the exact bound recipe used for identity and execution."""

    return yaml.safe_dump(
        dict(config.resolved_document),
        allow_unicode=True,
        sort_keys=False,
    )


__all__ = [
    "BaselineConfig",
    "BudgetConfig",
    "EvaluationDirection",
    "EvaluationTierConfig",
    "ImportedProfilesConfig",
    "ManagedTargetConfig",
    "ModelArtifactConfig",
    "ModelsConfig",
    "OptimizationBenchmarkConfig",
    "OptimizationCommandConfig",
    "OptimizationConfig",
    "OptimizationConfigError",
    "OptimizationExecutionConfig",
    "OptimizationPolicyConfig",
    "OptimizationRecipeResolution",
    "OptimizationWorkloadConfig",
    "PlannerProvider",
    "ProfileArtifactConfig",
    "ProfileProvider",
    "PromotionConfig",
    "RecipeInputConfig",
    "RecipeInputType",
    "RulesPlannerConfig",
    "RuntimeCaptureConfig",
    "RuntimeComponentConfig",
    "RuntimeContainerConfig",
    "RuntimeExpectedConfig",
    "RuntimeProvenanceConfig",
    "TargetBuildConfig",
    "TargetLaunchConfig",
    "TargetReadinessConfig",
    "TieredEvaluationConfig",
    "WorkloadPointConfig",
    "WorkloadSuiteConfig",
    "WorkspaceConfig",
    "dump_resolved_optimization_config",
    "load_optimization_config",
    "optimization_execution_lock_issues",
    "require_optimization_execution_lock",
    "resolve_optimization_config",
]
