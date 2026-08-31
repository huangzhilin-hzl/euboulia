"""Configuration loading and validation for Euboulia campaigns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a campaign configuration is malformed."""


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _string(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{path} must be a non-empty string")
    return value


def _integer(value: object, path: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{path} must be an integer >= {minimum}")
    return value


def _number(value: object, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{path} must be a number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ConfigError(f"{path} must be >= {minimum}")
    return result


def _string_map(value: object, path: str) -> dict[str, str]:
    mapping = _mapping(value, path)
    result: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError(f"{path} keys and values must be strings")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class WorkloadConfig:
    """A framework-neutral serving workload."""

    name: str
    model: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_prompts: int
    endpoint: str
    dataset: str = "random"


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """How a framework adapter should invoke its benchmark CLI."""

    mode: str
    base_args: tuple[str, ...] = ()
    result_filename: str = "result.json"


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """A predeclared candidate for the static MVP planner."""

    candidate_id: str
    name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    patch: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectnessGateConfig:
    metric: str
    minimum: float


@dataclass(frozen=True, slots=True)
class PerformanceGateConfig:
    metric: str
    direction: str
    min_relative_improvement: float = 0.0
    max_regression: float = 0.0
    noise_tolerance: float = 0.0


@dataclass(frozen=True, slots=True)
class GatesConfig:
    correctness: CorrectnessGateConfig
    performance: PerformanceGateConfig


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    artifacts_dir: Path
    ledger: Path
    timeout_seconds: float = 1800.0
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    """Validated campaign configuration."""

    schema_version: int
    name: str
    framework: str
    workload: WorkloadConfig
    benchmark: BenchmarkConfig
    candidates: tuple[CandidateConfig, ...]
    gates: GatesConfig
    execution: ExecutionConfig
    source: Path

    @property
    def baseline(self) -> CandidateConfig:
        """The first candidate is the immutable comparison baseline."""

        return self.candidates[0]


def _parse_workload(data: object) -> WorkloadConfig:
    raw = _mapping(data, "workload")
    return WorkloadConfig(
        name=_string(raw.get("name"), "workload.name"),
        model=_string(raw.get("model"), "workload.model"),
        input_tokens=_integer(raw.get("input_tokens"), "workload.input_tokens"),
        output_tokens=_integer(raw.get("output_tokens"), "workload.output_tokens"),
        concurrency=_integer(raw.get("concurrency"), "workload.concurrency"),
        num_prompts=_integer(raw.get("num_prompts"), "workload.num_prompts"),
        endpoint=_string(raw.get("endpoint"), "workload.endpoint"),
        dataset=_string(raw.get("dataset", "random"), "workload.dataset"),
    )


def _parse_benchmark(data: object) -> BenchmarkConfig:
    raw = _mapping(data, "benchmark")
    mode = _string(raw.get("mode"), "benchmark.mode")
    base_args_raw = raw.get("base_args", [])
    if not isinstance(base_args_raw, list) or not all(
        isinstance(item, str) for item in base_args_raw
    ):
        raise ConfigError("benchmark.base_args must be a list of strings")
    result_filename = _string(
        raw.get("result_filename", "result.json"), "benchmark.result_filename"
    )
    result_path = Path(result_filename)
    if result_path.is_absolute() or len(result_path.parts) != 1:
        raise ConfigError("benchmark.result_filename must be a plain filename")
    return BenchmarkConfig(
        mode=mode, base_args=tuple(base_args_raw), result_filename=result_filename
    )


def _parse_candidates(data: object) -> tuple[CandidateConfig, ...]:
    if not isinstance(data, list) or not data:
        raise ConfigError("candidates must be a non-empty list whose first item is the baseline")
    candidates: list[CandidateConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        path = f"candidates[{index}]"
        raw = _mapping(item, path)
        candidate_id = _string(raw.get("id"), f"{path}.id")
        if candidate_id in seen:
            raise ConfigError(f"duplicate candidate id: {candidate_id}")
        seen.add(candidate_id)
        parameters = _mapping(raw.get("parameters", {}), f"{path}.parameters")
        patch = raw.get("patch")
        if patch is not None and not isinstance(patch, str):
            raise ConfigError(f"{path}.patch must be a string or null")
        candidates.append(
            CandidateConfig(
                candidate_id=candidate_id,
                name=_string(raw.get("name", candidate_id), f"{path}.name"),
                parameters=parameters,
                env=_string_map(raw.get("env", {}), f"{path}.env"),
                patch=patch,
            )
        )
    return tuple(candidates)


def _parse_gates(data: object) -> GatesConfig:
    raw = _mapping(data, "gates")
    correctness_raw = _mapping(raw.get("correctness"), "gates.correctness")
    performance_raw = _mapping(raw.get("performance"), "gates.performance")
    direction = _string(performance_raw.get("direction"), "gates.performance.direction")
    if direction not in {"maximize", "minimize"}:
        raise ConfigError("gates.performance.direction must be 'maximize' or 'minimize'")
    return GatesConfig(
        correctness=CorrectnessGateConfig(
            metric=_string(correctness_raw.get("metric"), "gates.correctness.metric"),
            minimum=_number(correctness_raw.get("minimum"), "gates.correctness.minimum"),
        ),
        performance=PerformanceGateConfig(
            metric=_string(performance_raw.get("metric"), "gates.performance.metric"),
            direction=direction,
            min_relative_improvement=_number(
                performance_raw.get("min_relative_improvement", 0.0),
                "gates.performance.min_relative_improvement",
                minimum=0.0,
            ),
            max_regression=_number(
                performance_raw.get("max_regression", 0.0),
                "gates.performance.max_regression",
                minimum=0.0,
            ),
            noise_tolerance=_number(
                performance_raw.get("noise_tolerance", 0.0),
                "gates.performance.noise_tolerance",
                minimum=0.0,
            ),
        ),
    )


def _parse_execution(data: object, source: Path) -> ExecutionConfig:
    raw = _mapping(data, "execution")
    base = source.parent
    artifacts = Path(_string(raw.get("artifacts_dir", "artifacts"), "execution.artifacts_dir"))
    ledger = Path(_string(raw.get("ledger", "artifacts/experiments.jsonl"), "execution.ledger"))
    if not artifacts.is_absolute():
        artifacts = (base / artifacts).resolve()
    if not ledger.is_absolute():
        ledger = (base / ledger).resolve()
    filesystem_root = Path(artifacts.anchor)
    if artifacts in {filesystem_root, Path.home().resolve(), base.resolve()}:
        raise ConfigError(
            "execution.artifacts_dir must not be the filesystem root, home directory, "
            "or campaign directory"
        )
    if not ledger.is_relative_to(artifacts):
        raise ConfigError("execution.ledger must be located inside execution.artifacts_dir")
    return ExecutionConfig(
        artifacts_dir=artifacts,
        ledger=ledger,
        timeout_seconds=_number(
            raw.get("timeout_seconds", 1800), "execution.timeout_seconds", minimum=0.001
        ),
        env=_string_map(raw.get("env", {}), "execution.env"),
    )


def load_config(path: str | Path) -> CampaignConfig:
    """Load a YAML or JSON campaign file and fail closed on malformed input."""

    source = Path(path).expanduser().resolve()
    try:
        raw_document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration does not exist: {source}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
    document = _mapping(raw_document, "document")
    schema_version = _integer(document.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version {schema_version}; expected 1")
    framework = _string(document.get("framework"), "framework").lower()
    if framework not in {"sglang", "vllm"}:
        raise ConfigError("framework must be 'sglang' or 'vllm'")
    return CampaignConfig(
        schema_version=schema_version,
        name=_string(document.get("name"), "name"),
        framework=framework,
        workload=_parse_workload(document.get("workload")),
        benchmark=_parse_benchmark(document.get("benchmark")),
        candidates=_parse_candidates(document.get("candidates")),
        gates=_parse_gates(document.get("gates")),
        execution=_parse_execution(document.get("execution", {}), source),
        source=source,
    )


__all__ = [
    "BenchmarkConfig",
    "CampaignConfig",
    "CandidateConfig",
    "ConfigError",
    "CorrectnessGateConfig",
    "ExecutionConfig",
    "GatesConfig",
    "PerformanceGateConfig",
    "WorkloadConfig",
    "load_config",
]
