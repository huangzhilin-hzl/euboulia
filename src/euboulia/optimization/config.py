"""Strict schema-version 2 configuration for iterative optimization."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

from euboulia.models import Framework, JSONValue
from euboulia.optimization.contracts import EvaluationTierKind, _json_mapping


class OptimizationConfigError(ValueError):
    """Raised when a v2 optimization document is malformed or unsafe."""


class ProfileProvider(StrEnum):
    IMPORTED = "imported"


class PlannerProvider(StrEnum):
    RULES = "rules"


class EvaluationDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass(frozen=True, slots=True)
class OptimizationWorkloadConfig:
    name: str
    model: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_prompts: int
    endpoint: str
    dataset: str = "random"


@dataclass(frozen=True, slots=True)
class OptimizationBenchmarkConfig:
    mode: str
    base_args: tuple[str, ...] = ()
    result_filename: str = "result.json"
    parameters: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    candidate_id: str
    source_revision: str
    target_parameters: Mapping[str, JSONValue] = field(default_factory=dict)


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
    """Fully normalized v2 optimization configuration."""

    schema_version: int
    name: str
    framework: Framework
    workload: OptimizationWorkloadConfig
    benchmark: OptimizationBenchmarkConfig
    baseline: BaselineConfig
    optimization: OptimizationPolicyConfig
    execution: OptimizationExecutionConfig
    source: Path


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
    _reject_unknown(raw, {"id", "source_revision", "target_parameters"}, path)
    return BaselineConfig(
        candidate_id=_string(raw.get("id"), "baseline.id"),
        source_revision=_string(raw.get("source_revision"), "baseline.source_revision"),
        target_parameters=_parse_json_mapping(
            raw.get("target_parameters", {}), "baseline.target_parameters"
        ),
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
                argv=_string_tuple(raw.get("argv"), f"{item_path}.argv", allow_empty=False),
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
                f"{item_path}.kind must be smoke, correctness, or performance"
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
        raise OptimizationConfigError(f"{path} must be ordered smoke, correctness, performance")
    if (
        EvaluationTierKind.CORRECTNESS not in kinds
        or kinds[-1] is not EvaluationTierKind.PERFORMANCE
    ):
        raise OptimizationConfigError(
            f"{path} must include correctness and finish with performance"
        )
    return tuple(tiers)


def _parse_evaluation(value: object) -> TieredEvaluationConfig:
    path = "optimization.evaluation"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "metric",
            "direction",
            "tiers",
            "min_relative_improvement",
            "max_regression",
            "noise_tolerance",
            "baseline_value",
            "metrics_path",
        },
        path,
    )
    try:
        direction = EvaluationDirection(_string(raw.get("direction"), f"{path}.direction"))
    except ValueError as exc:
        raise OptimizationConfigError(
            "optimization.evaluation.direction must be 'maximize' or 'minimize'"
        ) from exc
    return TieredEvaluationConfig(
        metric=_string(raw.get("metric"), f"{path}.metric"),
        direction=direction,
        tiers=_parse_evaluation_tiers(raw.get("tiers")),
        min_relative_improvement=_number(
            raw.get("min_relative_improvement", 0.0),
            f"{path}.min_relative_improvement",
            minimum=0.0,
        ),
        max_regression=_number(
            raw.get("max_regression", 0.0), f"{path}.max_regression", minimum=0.0
        ),
        noise_tolerance=_number(
            raw.get("noise_tolerance", 0.0), f"{path}.noise_tolerance", minimum=0.0
        ),
        baseline_value=_optional_number(raw.get("baseline_value"), f"{path}.baseline_value"),
        metrics_path=(
            None
            if raw.get("metrics_path") is None
            else _workspace_relative_path(raw.get("metrics_path"), f"{path}.metrics_path")
        ),
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


def _parse_optimization(value: object, source: Path) -> OptimizationPolicyConfig:
    path = "optimization"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"profiles", "planner", "evaluation", "budget", "workspace"}, path)
    return OptimizationPolicyConfig(
        profiles=_parse_profiles(raw.get("profiles"), source),
        planner=_parse_planner(raw.get("planner"), source),
        evaluation=_parse_evaluation(raw.get("evaluation")),
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


def load_optimization_config(path: str | Path) -> OptimizationConfig:
    """Load only strict schema-version 2 input without touching v1 behavior."""

    source = Path(path).expanduser().resolve()
    try:
        document_object: object = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OptimizationConfigError(f"configuration does not exist: {source}") from exc
    except yaml.YAMLError as exc:
        raise OptimizationConfigError(f"invalid YAML in {source}: {exc}") from exc
    document = _mapping(document_object, "document")
    _reject_unknown(
        document,
        {
            "schema_version",
            "name",
            "framework",
            "workload",
            "benchmark",
            "baseline",
            "optimization",
            "execution",
        },
        "document",
    )
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1)
    if schema_version != 2:
        raise OptimizationConfigError(
            f"unsupported optimization schema_version {schema_version}; expected 2"
        )
    framework_value = _string(document.get("framework"), "framework").lower()
    if framework_value not in {Framework.SGLANG.value, Framework.VLLM.value}:
        raise OptimizationConfigError("framework must be 'sglang' or 'vllm'")
    return OptimizationConfig(
        schema_version=schema_version,
        name=_string(document.get("name"), "name"),
        framework=Framework(framework_value),
        workload=_parse_workload(document.get("workload")),
        benchmark=_parse_benchmark(document.get("benchmark")),
        baseline=_parse_baseline(document.get("baseline")),
        optimization=_parse_optimization(document.get("optimization"), source),
        execution=_parse_execution(document.get("execution"), source),
        source=source,
    )


__all__ = [
    "BaselineConfig",
    "BudgetConfig",
    "EvaluationDirection",
    "EvaluationTierConfig",
    "ImportedProfilesConfig",
    "OptimizationBenchmarkConfig",
    "OptimizationCommandConfig",
    "OptimizationConfig",
    "OptimizationConfigError",
    "OptimizationExecutionConfig",
    "OptimizationPolicyConfig",
    "OptimizationWorkloadConfig",
    "PlannerProvider",
    "ProfileArtifactConfig",
    "ProfileProvider",
    "RulesPlannerConfig",
    "TieredEvaluationConfig",
    "WorkspaceConfig",
    "load_optimization_config",
]
