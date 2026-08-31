from pathlib import Path

import pytest

from euboulia.models import Framework
from euboulia.optimization import (
    EvaluationDirection,
    EvaluationTierKind,
    OptimizationConfigError,
    PlannerProvider,
    ProfileProvider,
    load_optimization_config,
)

VALID_CONFIG = """
schema_version: 2
name: p0-optimization
framework: vllm
workload:
  name: fixed-serving
  model: test/model
  input_tokens: 128
  output_tokens: 32
  concurrency: 4
  num_prompts: 16
  endpoint: http://127.0.0.1:8000
  dataset: random
benchmark:
  mode: serve
  base_args: [--ignore-eos]
  result_filename: result.json
  parameters:
    request_rate: 10
baseline:
  id: baseline
  source_revision: abc123
  target_parameters: {}
optimization:
  profiles:
    provider: imported
    paths: [profiles/baseline.json, profiles/champion.json]
  planner:
    provider: rules
    patch_catalog: catalogs/vllm-patches.yaml
    max_proposals_per_iteration: 1
    reject_duplicate_diffs: true
  evaluation:
    metric: output_throughput
    direction: maximize
    min_relative_improvement: 0.02
    max_regression: 0.01
    noise_tolerance: 0.005
    tiers:
      - kind: correctness
        warmups: 0
        repetitions: 1
        timeout_seconds: 60
        required_metrics: [success_rate]
      - kind: performance
        warmups: 2
        repetitions: 5
        timeout_seconds: 300
        required_metrics: [output_throughput]
  budget:
    max_iterations: 10
    max_wall_time_seconds: 3600
    max_consecutive_failures: 3
    no_improvement_patience: 4
    max_profile_bytes: 1048576
execution:
  artifacts_dir: artifacts
  ledger: artifacts/experiments.jsonl
  events: artifacts/events.jsonl
  memory: artifacts/memory.sqlite3
"""


def write_config(tmp_path: Path, contents: str = VALID_CONFIG) -> Path:
    path = tmp_path / "campaign-v2.yaml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_v2_config_is_strict_and_resolves_all_paths(tmp_path: Path) -> None:
    source = write_config(tmp_path)

    config = load_optimization_config(source)

    assert config.schema_version == 2
    assert config.framework is Framework.VLLM
    assert config.optimization.profiles.provider is ProfileProvider.IMPORTED
    assert config.optimization.planner.provider is PlannerProvider.RULES
    assert config.optimization.evaluation.direction is EvaluationDirection.MAXIMIZE
    assert tuple(tier.kind for tier in config.optimization.evaluation.tiers) == (
        EvaluationTierKind.CORRECTNESS,
        EvaluationTierKind.PERFORMANCE,
    )
    assert config.optimization.profiles.paths == (
        (tmp_path / "profiles/baseline.json").resolve(),
        (tmp_path / "profiles/champion.json").resolve(),
    )
    assert (
        config.optimization.planner.patch_catalog
        == (tmp_path / "catalogs/vllm-patches.yaml").resolve()
    )
    assert config.execution.artifacts_dir == (tmp_path / "artifacts").resolve()
    assert config.execution.event_ledger == (tmp_path / "artifacts/events.jsonl").resolve()
    assert config.execution.memory == (tmp_path / "artifacts/memory.sqlite3").resolve()
    assert not config.execution.artifacts_dir.exists()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version: 2", "schema_version: 1", "expected 2"),
        ("framework: vllm", "framework: other", "framework must be"),
        ("provider: imported", "provider: nsys", "must be 'imported'"),
        ("provider: rules", "provider: model", "must be 'rules'"),
        ("direction: maximize", "direction: sideways", "must be 'maximize'"),
        ("max_iterations: 10", "max_iterations: 0", "integer >= 1"),
        (
            "max_wall_time_seconds: 3600",
            "max_wall_time_seconds: .inf",
            "finite number",
        ),
    ],
)
def test_invalid_v2_values_fail_closed(tmp_path: Path, old: str, new: str, message: str) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace(old, new, 1))

    with pytest.raises(OptimizationConfigError, match=message):
        load_optimization_config(path)


def test_unknown_fields_and_invalid_tier_order_fail_closed(tmp_path: Path) -> None:
    unknown = write_config(
        tmp_path, VALID_CONFIG.replace("name: p0-optimization", "name: p0-optimization\ntypo: true")
    )
    with pytest.raises(OptimizationConfigError, match=r"unknown field.*typo"):
        load_optimization_config(unknown)

    invalid_order = VALID_CONFIG.replace("      - kind: performance", "      - kind: smoke", 1)
    path = write_config(tmp_path, invalid_order)
    with pytest.raises(OptimizationConfigError, match=r"ordered|finish with performance"):
        load_optimization_config(path)


def test_storage_files_must_remain_under_artifact_directory(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        VALID_CONFIG.replace("events: artifacts/events.jsonl", "events: elsewhere/events.jsonl"),
    )

    with pytest.raises(OptimizationConfigError, match="events must be located inside"):
        load_optimization_config(path)


def test_execution_storage_defaults_are_inside_artifacts(tmp_path: Path) -> None:
    contents = VALID_CONFIG.replace(
        "  ledger: artifacts/experiments.jsonl\n"
        "  events: artifacts/events.jsonl\n"
        "  memory: artifacts/memory.sqlite3\n",
        "",
    )
    config = load_optimization_config(write_config(tmp_path, contents))

    assert config.execution.experiment_ledger.name == "experiments.jsonl"
    assert config.execution.event_ledger.name == "events.jsonl"
    assert config.execution.memory.name == "memory.sqlite3"
