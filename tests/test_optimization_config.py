from pathlib import Path

import pytest
import yaml

from euboulia.models import Framework
from euboulia.optimization import (
    EvaluationDirection,
    EvaluationTierKind,
    OptimizationConfigError,
    PlannerProvider,
    ProfileProvider,
    load_optimization_config,
)
from euboulia.optimization.config import (
    ManagedTargetConfig,
    TargetBuildConfig,
    TargetLaunchConfig,
    TargetReadinessConfig,
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

MINIMAL_TARGET = """target:
  provider: sglang
  launch:
    argv: [python3, -m, sglang.launch_server]
  readiness:
    url: http://localhost:30000/health_generate
  gpus: [0]
"""

FULL_TARGET = """target:
  provider: sglang
  launch:
    argv: [python3, -m, sglang.launch_server, --model-path, test/model]
    env:
      SGLANG_LOG_LEVEL: info
      OPTIONAL_SECRET:
  readiness:
    url: http://[::1]:30000/health_generate
    timeout_seconds: 90
    interval_seconds: 0.25
  shutdown_timeout_seconds: 12
  gpus: [0, GPU-acde]
  build:
    commands:
      - name: install-editable
        argv: [python3, -m, pip, install, -e, .]
        timeout_seconds: 120
        env:
          PIP_DISABLE_PIP_VERSION_CHECK: "1"
  provenance:
    source: https://example.invalid/recipe.json
    verified: true
"""


def write_config(tmp_path: Path, contents: str = VALID_CONFIG) -> Path:
    path = tmp_path / "campaign-v2.yaml"
    path.write_text(contents, encoding="utf-8")
    return path


def config_with_target(target: str, *, framework: str = "sglang") -> str:
    contents = VALID_CONFIG.replace("framework: vllm", f"framework: {framework}", 1)
    return contents.replace("workload:\n", f"{target}workload:\n", 1)


def v3_managed_document(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 3,
        "name": "suite-runtime-speculative",
        "framework": "sglang",
        "models": {
            "target": {
                "id": "target",
                "path": "/models/target",
                "served_name": "target-served",
                "revision": "model-commit",
            },
            "drafts": [
                {
                    "id": "draft",
                    "path": "/models/draft",
                    "served_name": "draft-served",
                    "revision": "draft-commit",
                }
            ],
        },
        "endpoint": "http://127.0.0.1:30000",
        "workload_suite": {
            "id": "latency-throughput",
            "dataset": "random",
            "request_rate": "inf",
            "points": [
                {
                    "id": "short-c1",
                    "input_tokens": 128,
                    "output_tokens": 32,
                    "concurrency": 1,
                    "num_prompts": 8,
                },
                {
                    "id": "long-c16",
                    "input_tokens": 4096,
                    "output_tokens": 512,
                    "concurrency": 16,
                    "num_prompts": 64,
                },
            ],
        },
        "benchmark": {"mode": "serve"},
        "baseline": {"source_revision": "sglang-commit"},
        "target": {
            "provider": "sglang",
            "gpus": list(range(8)),
            "launch": {
                "argv": [
                    "python",
                    "-m",
                    "sglang.launch_server",
                    "--model-path",
                    "/models/target",
                    "--kv-cache-dtype",
                    "bfloat16",
                    "--speculative-algorithm",
                    "DFLASH2",
                    "--speculative-draft-model-path",
                    "/models/draft",
                ]
            },
            "readiness": {"url": "http://127.0.0.1:30000/health_generate"},
            "runtime": {
                "expected": {
                    "container": {
                        "image": "example/sglang:pinned",
                        "digest": "sha256:" + "a" * 64,
                    },
                    "components": {
                        "torch": {"version": "2.8.0"},
                        "cuda": {"version": "12.8"},
                        "deepep": {"revision": "deepep-commit"},
                        "deepgemm": {"revision": "deepgemm-commit"},
                        "flashinfer": {"version": "0.4.1"},
                    },
                },
                "capture": {"fail_on_mismatch": True, "require_observed": False},
            },
            "serving": {
                "backends": {"kv_cache_dtype": "bfloat16"},
                "speculative": {
                    "algorithm": "DFLASH2",
                    "draft": {"kind": "external", "model_ref": "draft"},
                    "num_steps": 1,
                    "num_draft_tokens": 8,
                    "draft_attention_backend": "fa4",
                },
            },
        },
        "optimization": {
            "profiles": {"provider": "imported", "paths": ["profile.json"]},
            "planner": {"provider": "rules", "patch_catalog": "changes.yaml"},
            "workspace": {
                "repository": str(tmp_path / "sglang"),
                "root_dir": str(tmp_path / "worktrees"),
            },
            "evaluation": {
                "metric": "output_throughput",
                "direction": "maximize",
                "metrics_path": "metrics.json",
                "promotion": {
                    "primary_points": ["short-c1", "long-c16"],
                    "min_relative_improvement": 0.03,
                    "max_regression_per_point": 0.01,
                    "noise_tolerance": 0.005,
                },
                "tiers": [
                    {
                        "kind": "correctness",
                        "timeout_seconds": 10,
                        "commands": [{"name": "correct", "argv": ["python", "correct.py"]}],
                    },
                    {
                        "kind": "performance",
                        "timeout_seconds": 10,
                        "commands": [{"name": "bench", "argv": ["python", "bench.py"]}],
                    },
                ],
            },
            "budget": {"max_iterations": 1, "max_wall_time_seconds": 30},
        },
        "execution": {"artifacts_dir": str(tmp_path / "artifacts")},
    }


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
    assert config.target is None


def test_load_v3_normalizes_models_suite_runtime_and_speculative_draft(
    tmp_path: Path,
) -> None:
    source = tmp_path / "campaign-v3.yaml"
    source.write_text(
        yaml.safe_dump(v3_managed_document(tmp_path), sort_keys=False),
        encoding="utf-8",
    )

    config = load_optimization_config(source)

    assert config.schema_version == 3
    assert config.models.target.path == "/models/target"
    assert config.models.drafts[0].path == "/models/draft"
    assert tuple(point.point_id for point in config.workload_suite.points) == (
        "short-c1",
        "long-c16",
    )
    assert config.workload.name == "short-c1"
    assert config.target is not None
    assert config.target.runtime is not None
    assert config.target.runtime.expected.container is not None
    assert config.target.runtime.expected.container.digest == "sha256:" + "a" * 64
    assert set(config.target.runtime.expected.components) == {
        "torch",
        "cuda",
        "deepep",
        "deepgemm",
        "flashinfer",
    }
    assert config.target.serving is not None
    assert config.target.serving.speculative.draft is not None
    assert config.target.serving.speculative.draft.model_ref == "draft"
    promotion = config.optimization.evaluation.promotion
    assert promotion is not None
    assert promotion.primary_points == ("short-c1", "long-c16")


def test_v3_rejects_external_draft_not_materialized_in_launch_argv(tmp_path: Path) -> None:
    document = v3_managed_document(tmp_path)
    target = document["target"]
    assert isinstance(target, dict)
    launch = target["launch"]
    assert isinstance(launch, dict)
    argv = launch["argv"]
    assert isinstance(argv, list)
    argv.remove("/models/draft")
    source = tmp_path / "campaign-v3.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match="external speculative draft model path"):
        load_optimization_config(source)


def test_loads_complete_managed_target_config(tmp_path: Path) -> None:
    config = load_optimization_config(write_config(tmp_path, config_with_target(FULL_TARGET)))

    assert isinstance(config.target, ManagedTargetConfig)
    assert config.target.provider is Framework.SGLANG
    assert config.target.launch == TargetLaunchConfig(
        argv=(
            "python3",
            "-m",
            "sglang.launch_server",
            "--model-path",
            "test/model",
        ),
        env={"SGLANG_LOG_LEVEL": "info", "OPTIONAL_SECRET": None},
    )
    assert config.target.readiness == TargetReadinessConfig(
        url="http://[::1]:30000/health_generate",
        timeout_seconds=90.0,
        interval_seconds=0.25,
    )
    assert config.target.shutdown_timeout_seconds == 12.0
    assert config.target.gpus == ("0", "GPU-acde")
    assert isinstance(config.target.build, TargetBuildConfig)
    assert len(config.target.build.commands) == 1
    assert config.target.build.commands[0].name == "install-editable"
    assert config.target.build.commands[0].argv == (
        "python3",
        "-m",
        "pip",
        "install",
        "-e",
        ".",
    )
    assert config.target.build.commands[0].timeout_seconds == 120.0
    assert config.target.provenance == {
        "source": "https://example.invalid/recipe.json",
        "verified": True,
    }


def test_loads_minimal_managed_target_with_safe_defaults(tmp_path: Path) -> None:
    config = load_optimization_config(write_config(tmp_path, config_with_target(MINIMAL_TARGET)))

    assert config.target is not None
    assert config.target.launch.env == {}
    assert config.target.readiness.timeout_seconds == 60.0
    assert config.target.readiness.interval_seconds == 1.0
    assert config.target.shutdown_timeout_seconds == 30.0
    assert config.target.gpus == ("0",)
    assert config.target.build is None
    assert config.target.provenance == {}


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  gpus: [0]\n", "  gpus: [0]\n  typo: true\n", "unknown field.*typo"),
        (
            "    argv: [python3, -m, sglang.launch_server]\n",
            "    argv: [python3, -m, sglang.launch_server]\n    shell: true\n",
            "unknown field.*shell",
        ),
        (
            "    url: http://localhost:30000/health_generate\n",
            "    url: http://localhost:30000/health_generate\n    retries: 2\n",
            "unknown field.*retries",
        ),
    ],
)
def test_target_unknown_fields_fail_closed(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    target = MINIMAL_TARGET.replace(old, new, 1)

    with pytest.raises(OptimizationConfigError, match=message):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


def test_target_provider_must_be_sglang_and_match_framework(tmp_path: Path) -> None:
    with pytest.raises(OptimizationConfigError, match="must match framework"):
        load_optimization_config(
            write_config(tmp_path, config_with_target(MINIMAL_TARGET, framework="vllm"))
        )

    unsupported = MINIMAL_TARGET.replace("provider: sglang", "provider: vllm", 1)
    with pytest.raises(OptimizationConfigError, match="must be 'sglang'"):
        load_optimization_config(
            write_config(tmp_path, config_with_target(unsupported, framework="vllm"))
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:30000/health_generate",
        "http://localhost.example.com:30000/health_generate",
        "http://0.0.0.0:30000/health_generate",
        "ftp://localhost:30000/health_generate",
    ],
)
def test_target_readiness_rejects_nonlocal_or_non_http_urls(tmp_path: Path, url: str) -> None:
    target = MINIMAL_TARGET.replace("http://localhost:30000/health_generate", url, 1)

    with pytest.raises(OptimizationConfigError, match=r"HTTP|localhost|loopback"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


def test_managed_target_workload_endpoint_must_also_be_loopback(tmp_path: Path) -> None:
    contents = config_with_target(MINIMAL_TARGET).replace(
        "endpoint: http://127.0.0.1:8000",
        "endpoint: https://inference.example.com:8000",
        1,
    )

    with pytest.raises(OptimizationConfigError, match=r"localhost|loopback"):
        load_optimization_config(write_config(tmp_path, contents))


@pytest.mark.parametrize(
    "argv_yaml",
    [
        "python3 -m sglang.launch_server; touch /tmp/unsafe",
        "[]",
        '["python\\0bad", -m, sglang.launch_server]',
    ],
)
def test_target_launch_rejects_command_strings_empty_argv_and_nul(
    tmp_path: Path, argv_yaml: str
) -> None:
    target = MINIMAL_TARGET.replace("[python3, -m, sglang.launch_server]", argv_yaml, 1)

    with pytest.raises(OptimizationConfigError, match=r"list|empty|NUL"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


@pytest.mark.parametrize("gpus", ['[0, "0"]', "[]", '["0,1"]', '["GPU 0"]'])
def test_target_gpu_ids_are_fixed_unique_tokens(tmp_path: Path, gpus: str) -> None:
    target = MINIMAL_TARGET.replace("[0]", gpus, 1)

    with pytest.raises(OptimizationConfigError, match=r"GPU IDs|duplicate|comma|whitespace"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


def test_target_launch_cannot_override_fixed_gpu_assignment(tmp_path: Path) -> None:
    target = MINIMAL_TARGET.replace(
        "  readiness:\n",
        "    env:\n      CUDA_VISIBLE_DEVICES: '1'\n  readiness:\n",
        1,
    )

    with pytest.raises(OptimizationConfigError, match="CUDA_VISIBLE_DEVICES"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


def test_target_build_rejects_shell_command_strings(tmp_path: Path) -> None:
    target = MINIMAL_TARGET.replace(
        "  readiness:\n",
        "  build:\n    commands: python3 -m pip install -e .\n  readiness:\n",
        1,
    )

    with pytest.raises(OptimizationConfigError, match="must be a list"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


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
