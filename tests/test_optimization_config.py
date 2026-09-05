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
    derive_sglang_launch_facets,
    load_optimization_config,
    scenario_identity,
)
from euboulia.optimization.config import (
    ManagedTargetConfig,
    TargetBuildConfig,
    TargetLaunchConfig,
    TargetReadinessConfig,
    dump_resolved_optimization_config,
    optimization_execution_lock_issues,
    require_optimization_execution_lock,
    resolve_optimization_config,
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
  name: baseline
  source_revision: abc123
  target_parameters: {}
optimization:
  profiling:
    provider: sglang_torch
    workload_point: fixed-serving
    warmup_runs: 0
    num_steps: 2
    max_raw_bytes: 1048576
    min_free_disk_bytes: 1048576
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
execution:
  artifacts_dir: artifacts
  ledger: artifacts/experiments.jsonl
  events: artifacts/events.jsonl
  memory: artifacts/memory.sqlite3
"""

MINIMAL_TARGET = """target:
  provider: sglang
  launch:
    options: {}
  readiness:
    url: http://localhost:30000/health_generate
  gpus: [0]
"""

FULL_TARGET = """target:
  provider: sglang
  launch:
    python: python3
    python_options: [-u]
    module: sglang.launch_server
    options:
      --tp-size: 8
      --trust-remote-code: true
      --disable-overlap: false
    extra_argv: [--custom-flag]
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
                "name": "target",
                "path": "/models/target",
                "served_name": "target-served",
                "revision": "e" * 40,
            },
            "drafts": [
                {
                    "name": "draft",
                    "path": "/models/draft",
                    "served_name": "draft-served",
                    "revision": "f" * 40,
                }
            ],
        },
        "endpoint": "http://127.0.0.1:30000",
        "workload_suite": {
            "name": "latency-throughput",
            "dataset": "random",
            "request_rate": "inf",
            "points": [
                {
                    "name": "short-c1",
                    "input_tokens": 128,
                    "output_tokens": 32,
                    "concurrency": 1,
                    "num_prompts": 8,
                },
                {
                    "name": "long-c16",
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
                "python": "python",
                "module": "sglang.launch_server",
                "options": {
                    "--kv-cache-dtype": "bfloat16",
                    "--speculative-algorithm": "DFLASH2",
                    "--speculative-draft-attention-backend": "FA4",
                    "--speculative-draft-model-path": "/models/draft",
                    "--speculative-num-draft-tokens": 8,
                    "--speculative-num-steps": 1,
                },
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
                        "deepep": {"revision": "c" * 40},
                        "deepgemm": {"revision": "d" * 40},
                        "flashinfer": {"version": "0.4.1"},
                    },
                },
                "capture": {"fail_on_mismatch": True, "require_observed": False},
            },
        },
        "optimization": {
            "profiling": {
                "provider": "sglang_torch",
                "workload_point": "short-c1",
                "warmup_runs": 0,
                "num_steps": 2,
                "max_raw_bytes": 1_000_000,
                "min_free_disk_bytes": 1_000_000,
            },
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


def write_input_template(tmp_path: Path) -> tuple[Path, Path]:
    document = v3_managed_document(tmp_path)
    document["inputs"] = {
        "container_image": {"type": "container_digest", "required": True},
        "sglang_revision": {"type": "git_commit", "required": True},
    }
    baseline = document["baseline"]
    target = document["target"]
    assert isinstance(baseline, dict) and isinstance(target, dict)
    baseline["source_revision"] = "${sglang_revision}"
    runtime = target["runtime"]
    assert isinstance(runtime, dict)
    expected = runtime["expected"]
    assert isinstance(expected, dict)
    container = expected["container"]
    components = expected["components"]
    assert isinstance(container, dict) and isinstance(components, dict)
    container["image"] = "${container_image}"
    components["sglang"] = {"revision": "${sglang_revision}", "dirty": False}

    template = tmp_path / "template.yaml"
    values = tmp_path / "values.yaml"
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    values.write_text(
        yaml.safe_dump(
            {
                "container_image": "registry.example/sglang@sha256:" + "a" * 64,
                "sglang_revision": "B" * 40,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return template, values


def test_load_v2_config_is_strict_and_resolves_all_paths(tmp_path: Path) -> None:
    source = write_config(tmp_path)

    config = load_optimization_config(source)

    assert config.schema_version == 2
    assert config.framework is Framework.VLLM
    assert config.optimization.profiling.provider is ProfileProvider.SGLANG_TORCH
    assert config.optimization.profiling.workload_point == "fixed-serving"
    assert config.optimization.planner.provider is PlannerProvider.RULES
    assert config.optimization.evaluation.direction is EvaluationDirection.MAXIMIZE
    assert tuple(tier.kind for tier in config.optimization.evaluation.tiers) == (
        EvaluationTierKind.CORRECTNESS,
        EvaluationTierKind.PERFORMANCE,
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


def test_imported_profile_compatibility_shape_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(VALID_CONFIG)
    optimization = document["optimization"]
    optimization.pop("profiling")
    optimization["profiles"] = {
        "provider": "imported",
        "paths": ["profiles/baseline.json"],
    }
    source = tmp_path / "legacy-imported-profile.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match=r"unknown field.*profiles"):
        load_optimization_config(source)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model_id": None}, "model_id is required"),
        ({"model_id": "/models/local"}, "owner/model repository ID"),
        ({"model_id": "https://huggingface.co/owner/model"}, "owner/model repository ID"),
        ({"download": {"provider": "unknown"}}, "modelscope or hf_mirror"),
        ({"path": "relative/model"}, "absolute model directory"),
        ({"path": "/"}, "absolute model directory"),
        ({"revision": "main"}, "pin a full commit"),
        ({"download": {"provider": "modelscope", "timeout_seconds": 0}}, "must be >= 1"),
    ],
)
def test_model_download_rejects_incomplete_or_unsafe_configuration(
    tmp_path: Path,
    overrides: dict,
    message: str,
) -> None:
    document = v3_managed_document(tmp_path)
    document["models"]["target"].update(
        model_id="example/model", download={"provider": "modelscope"}
    )
    document["models"]["target"].update(overrides)
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(document))
    with pytest.raises(OptimizationConfigError, match=message):
        load_optimization_config(recipe)


def test_model_download_lock_round_trip(tmp_path: Path) -> None:
    document = v3_managed_document(tmp_path)
    document["models"]["target"].update(
        model_id="example/model", download={"provider": "hf_mirror", "timeout_seconds": 1800}
    )
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(document))
    config = load_optimization_config(recipe)
    lock = tmp_path / "recipe.lock.yaml"
    lock.write_text(dump_resolved_optimization_config(config, destination=lock))
    assert load_optimization_config(lock).models.target == config.models.target


def test_loads_exact_dsv4_megamoe_target_validation_scenario(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    values = tmp_path / "dsv4-values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "container_image": "registry.example/dsv4@sha256:" + "a" * 64,
                "deepgemm_revision": "c" * 40,
                "deepgemm_repository": "https://example.invalid/deepgemm.git",
                "deepgemm_ref": "refs/heads/deepgemm-test",
                "lm_eval_version": "0.4.9.2",
                "model_revision": "d" * 40,
                "model_id": "example/DeepSeek-V4-Flash-0731",
                "model_provider": "modelscope",
                "sglang_revision": "b" * 40,
                "sglang_repository": "https://example.invalid/sglang.git",
                "sglang_ref": "refs/heads/sglang-test",
            }
        ),
        encoding="utf-8",
    )
    config = load_optimization_config(repository / "examples/scenarios/dsv4-megamoe.yaml", values)

    assert config.name == "ds-v4-flash-dspark-cp8-tp8-ep8-megamoe"
    assert config.workload_suite.dataset == "random"
    assert len(config.workload_suite.points) == 30
    assert [tier.kind for tier in config.optimization.evaluation.tiers] == [
        EvaluationTierKind.CORRECTNESS,
        EvaluationTierKind.PERFORMANCE,
    ]
    performance_command = config.optimization.evaluation.tiers[-1].commands[0]
    assert config.benchmark.parameters == {
        "seed": 1,
        "random_range_ratio": 0,
        "flush_cache": True,
    }
    assert "EUBOULIA_BENCHMARK_SEED" not in performance_command.env
    assert "EUBOULIA_RANDOM_RANGE_RATIO" not in performance_command.env
    assert not any(name.startswith("EUBOULIA_SHAREGPT") for name in performance_command.env)
    lanes = config.optimization.evaluation.lanes
    assert lanes is not None
    assert len(lanes.fast.points) == 4
    assert len(lanes.qualification.points) == 30
    assert lanes.fast.stability.max_windows == 4
    accuracy = config.optimization.evaluation.accuracy
    assert accuracy is not None
    assert accuracy.command.argv[:3] == ("python3", "-m", "lm_eval")
    assert accuracy.command.argv[accuracy.command.argv.index("--model") + 1] == (
        "local-chat-completions"
    )
    model_args = accuracy.command.argv[accuracy.command.argv.index("--model_args") + 1]
    assert set(model_args.split(",")) == {
        "api_key=EMPTY",
        "base_url={endpoint}/v1/chat/completions",
        "max_length=16384",
        "max_retries=5",
        "model={served_name}",
        "num_concurrent=64",
        "timeout=1800",
        "tokenized_requests=false",
    }
    assert accuracy.result.metric == "results.gsm8k.exact_match,flexible-extract"
    assert config.target is not None
    assert config.target.hardware == {
        "accelerator": "NVIDIA-H20",
        "accelerator_count": 8,
        "node_count": 1,
    }
    assert config.target.launch.bind_host == "0.0.0.0"
    assert config.baseline.source_revision == "b" * 40
    assert config.sources["sglang"].repository == "https://example.invalid/sglang.git"
    assert config.sources["sglang"].ref == "refs/heads/sglang-test"
    assert config.sources["deepgemm"].revision == "c" * 40
    assert config.sources["deepgemm"].submodules is True
    assert config.models.target.model_id == "example/DeepSeek-V4-Flash-0731"
    assert config.models.target.download is not None
    assert config.models.target.download.provider == "modelscope"
    assert config.optimization.workspace is not None
    assert config.optimization.workspace.source == "sglang"
    assert config.optimization.workspace.repository is None
    assert config.target.build is not None
    assert "{source.deepgemm}" in config.target.build.commands[-1].argv
    assert config.target.runtime.expected.components["sglang"].source == "sglang"
    assert config.target.runtime.expected.components["sglang"].path is None
    assert config.target.runtime.expected.components["deepgemm"].source == "deepgemm"
    assert config.target.runtime is not None
    assert config.target.runtime.expected.components["sglang"].revision == "b" * 40
    assert optimization_execution_lock_issues(config) == ()
    host_index = config.target_launch_argv.index("--host")
    assert config.target_launch_argv[host_index + 1] == "0.0.0.0"
    assert config.target.launch.env["SGLANG_OPT_USE_DEEPGEMM_MEGA_MOE"] == "1"
    assert config.optimization.profiling.workload_point == "isl16384-osl256-c1-n1"
    assert config.optimization.profiling.expected_rank_traces == 8
    assert config.optimization.profiling.keep_raw is False
    assert config.optimization.profiling.required_kernel_pattern == "fp8_mxfp4_mega_moe"
    launch_facets = derive_sglang_launch_facets(config.target.launch.options)
    assert launch_facets["backends"] == {"moe_a2a": "megamoe"}
    assert launch_facets["speculative"] == {
        "algorithm": "dspark",
        "dspark_block_size": 5,
    }
    parallelism = launch_facets["parallelism"]
    assert isinstance(parallelism, dict)
    assert parallelism["tp_size"] == 8
    assert parallelism["enable_prefill_cp"] is True
    assert parallelism["cp_strategy"] == "interleave"


def test_load_v3_normalizes_models_suite_runtime_and_derives_launch_facets(
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
    assert tuple(point.name for point in config.workload_suite.points) == (
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
    assert derive_sglang_launch_facets(config.target.launch.options) == {
        "model_execution": {"kv_cache_dtype": "bfloat16"},
        "speculative": {
            "algorithm": "dflash2",
            "draft_attention_backend": "fa4",
            "draft_model_path": "/models/draft",
            "num_draft_tokens": 8,
            "num_steps": 1,
        },
    }
    hard_facets = scenario_identity(config, "same-hardware").compatibility_facets["hard"]
    assert isinstance(hard_facets, dict)
    launch_facets = hard_facets["launch"]
    assert isinstance(launch_facets, dict)
    speculative_facets = launch_facets["speculative"]
    assert isinstance(speculative_facets, dict)
    assert speculative_facets["draft_model_path"] == "<model:1>"
    promotion = config.optimization.evaluation.promotion
    assert promotion is not None
    assert promotion.primary_points == ("short-c1", "long-c16")


def test_v3_external_accuracy_rejects_raw_argv(tmp_path: Path) -> None:
    document = v3_managed_document(tmp_path)
    evaluation = document["optimization"]["evaluation"]
    assert isinstance(evaluation, dict)
    evaluation["accuracy"] = {
        "command": {"name": "accuracy", "argv": ["lm_eval", "--tasks", "gsm8k"]},
        "result": {
            "path": "accuracy.json",
            "metric": "results.gsm8k.exact_match,flexible-extract",
            "direction": "maximize",
            "threshold": 0.8,
        },
    }
    source = tmp_path / "raw-accuracy-argv.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        OptimizationConfigError,
        match="must use executable/module/options instead of argv",
    ):
        load_optimization_config(source)


def test_recipe_inputs_allow_plan_inventory_but_strict_load_requires_values(
    tmp_path: Path,
) -> None:
    template, _ = write_input_template(tmp_path)

    resolution = resolve_optimization_config(template, allow_unresolved=True)

    assert resolution.config is None
    assert resolution.missing_inputs == ("container_image", "sglang_revision")
    assert resolution.resolved is False
    with pytest.raises(OptimizationConfigError, match="missing required input binding"):
        load_optimization_config(template)


def test_recipe_values_produce_normalized_executable_configuration(tmp_path: Path) -> None:
    template, values = write_input_template(tmp_path)

    resolution = resolve_optimization_config(template, values)
    config = resolution.config

    assert config is not None
    assert resolution.resolved is True
    assert resolution.missing_inputs == ()
    assert config.baseline.source_revision == "b" * 40
    assert config.target is not None and config.target.runtime is not None
    container = config.target.runtime.expected.container
    assert container is not None
    assert container.image == "registry.example/sglang@sha256:" + "a" * 64
    assert config.target.runtime.expected.components["sglang"].revision == "b" * 40
    assert config.input_bindings == {
        "container_image": "registry.example/sglang@sha256:" + "a" * 64,
        "sglang_revision": "b" * 40,
    }
    assert "inputs" not in config.resolved_document
    resolved_yaml = dump_resolved_optimization_config(config)
    assert "${" not in resolved_yaml
    assert "inputs:" not in resolved_yaml
    assert optimization_execution_lock_issues(config) == ()
    require_optimization_execution_lock(config)


def test_resolved_recipe_can_move_without_changing_source_relative_paths(
    tmp_path: Path,
) -> None:
    template, values = write_input_template(tmp_path)
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    document["optimization"]["workspace"] = {
        "repository": "source/sglang",
        "root_dir": "state/worktrees",
    }
    document["target"]["runtime"]["expected"]["components"]["sglang"]["path"] = "source/sglang"
    document["execution"] = {
        "artifacts_dir": "state/artifacts",
        "ledger": "state/artifacts/experiments.jsonl",
        "events": "state/artifacts/events.jsonl",
        "memory": "state/artifacts/memory.sqlite3",
    }
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    expected = load_optimization_config(template, values)
    destination = tmp_path / "private" / "baseline" / "recipe.lock.yaml"

    destination.parent.mkdir(parents=True)
    destination.write_text(
        dump_resolved_optimization_config(expected, destination=destination),
        encoding="utf-8",
    )
    actual = load_optimization_config(destination)

    assert actual.optimization.planner.patch_catalog == (
        expected.optimization.planner.patch_catalog
    )
    assert actual.optimization.workspace == expected.optimization.workspace
    assert actual.target is not None and actual.target.runtime is not None
    assert actual.target.runtime.expected.components["sglang"].path == (
        expected.target.runtime.expected.components["sglang"].path
    )
    assert actual.execution == expected.execution


def test_execution_lock_rejects_floating_managed_identity(tmp_path: Path) -> None:
    source = tmp_path / "floating.yaml"
    source.write_text(
        yaml.safe_dump(v3_managed_document(tmp_path), sort_keys=False),
        encoding="utf-8",
    )
    config = load_optimization_config(source)

    issues = optimization_execution_lock_issues(config)

    assert "baseline.source_revision must be a full Git commit" in issues
    assert "target.runtime.expected.components.sglang.revision is required" in issues
    with pytest.raises(OptimizationConfigError, match="execution recipe is not locked"):
        require_optimization_execution_lock(config)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("sglang_revision", "main", "full 40- or 64-character"),
        ("sglang_revision", "0" * 40, "all-zero placeholder"),
        ("container_image", "registry.example/sglang:latest", "immutable image reference"),
        (
            "container_image",
            "registry.example/sglang@sha256:" + "0" * 64,
            "all-zero digest",
        ),
    ],
)
def test_recipe_input_types_reject_floating_execution_identity(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    template, values = write_input_template(tmp_path)
    supplied = yaml.safe_load(values.read_text(encoding="utf-8"))
    assert isinstance(supplied, dict)
    supplied[key] = value
    values.write_text(yaml.safe_dump(supplied, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match=message):
        load_optimization_config(template, values)


def test_recipe_rejects_undeclared_partial_and_unused_inputs(tmp_path: Path) -> None:
    template, values = write_input_template(tmp_path)
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["name"] = "prefix-${sglang_revision}"
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(OptimizationConfigError, match="entire scalar"):
        resolve_optimization_config(template, values)

    document["name"] = "template"
    inputs = document["inputs"]
    assert isinstance(inputs, dict)
    inputs["unused"] = {"type": "string"}
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(OptimizationConfigError, match="unused input"):
        resolve_optimization_config(template, values)

    inputs.pop("unused")
    baseline = document["baseline"]
    assert isinstance(baseline, dict)
    baseline["source_revision"] = "${undeclared}"
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(OptimizationConfigError, match="undeclared input"):
        resolve_optimization_config(template, values)


def test_v3_aliases_are_optional_and_do_not_change_semantic_identity(tmp_path: Path) -> None:
    named = v3_managed_document(tmp_path)
    anonymous = v3_managed_document(tmp_path)
    named_source = tmp_path / "named.yaml"
    anonymous_source = tmp_path / "anonymous.yaml"

    named_models = named["models"]
    anonymous_models = anonymous["models"]
    named_suite = named["workload_suite"]
    anonymous_suite = anonymous["workload_suite"]
    assert isinstance(named_models, dict) and isinstance(anonymous_models, dict)
    assert isinstance(named_suite, dict) and isinstance(anonymous_suite, dict)
    named_target = named_models["target"]
    anonymous_target = anonymous_models["target"]
    named_drafts = named_models["drafts"]
    anonymous_drafts = anonymous_models["drafts"]
    assert isinstance(named_target, dict) and isinstance(anonymous_target, dict)
    assert isinstance(named_drafts, list) and isinstance(anonymous_drafts, list)
    assert isinstance(named_drafts[0], dict) and isinstance(anonymous_drafts[0], dict)

    anonymous_target.pop("name")
    anonymous_target["name"] = "renamed-target"
    anonymous_drafts[0].pop("name")
    anonymous_suite.pop("name")
    anonymous["name"] = "renamed-campaign"
    anonymous_baseline = anonymous["baseline"]
    assert isinstance(anonymous_baseline, dict)
    anonymous_baseline["name"] = "renamed-baseline"
    anonymous_points = anonymous_suite["points"]
    assert isinstance(anonymous_points, list)
    for point in anonymous_points:
        assert isinstance(point, dict)
        point.pop("name")
    anonymous_evaluation = anonymous["optimization"]
    assert isinstance(anonymous_evaluation, dict)
    evaluation = anonymous_evaluation["evaluation"]
    assert isinstance(evaluation, dict)
    promotion = evaluation["promotion"]
    assert isinstance(promotion, dict)
    promotion["primary_points"] = [
        "isl128-osl32-c1-n8",
        "isl4096-osl512-c16-n64",
    ]
    profiling = anonymous_evaluation["profiling"]
    assert isinstance(profiling, dict)
    profiling["workload_point"] = "isl128-osl32-c1-n8"

    named_source.write_text(yaml.safe_dump(named, sort_keys=False), encoding="utf-8")
    anonymous_source.write_text(yaml.safe_dump(anonymous, sort_keys=False), encoding="utf-8")
    named_config = load_optimization_config(named_source)
    anonymous_config = load_optimization_config(anonymous_source)
    named_identity = scenario_identity(named_config, "same-hardware")
    anonymous_identity = scenario_identity(anonymous_config, "same-hardware")

    assert tuple(point.name for point in anonymous_config.workload_suite.points) == (
        "isl128-osl32-c1-n8",
        "isl4096-osl512-c16-n64",
    )
    assert named_identity.spec_digest == anonymous_identity.spec_digest
    assert named_identity.aliases != anonymous_identity.aliases

    changed = v3_managed_document(tmp_path)
    changed_suite = changed["workload_suite"]
    assert isinstance(changed_suite, dict)
    changed_points = changed_suite["points"]
    assert isinstance(changed_points, list) and isinstance(changed_points[0], dict)
    changed_points[0]["input_tokens"] = 256
    changed_source = tmp_path / "changed.yaml"
    changed_source.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")

    changed_identity = scenario_identity(load_optimization_config(changed_source), "same-hardware")
    assert changed_identity.spec_digest != named_identity.spec_digest
    assert changed_identity.compatibility_digest == named_identity.compatibility_digest


def test_launch_option_order_does_not_change_compiled_argv_or_identity(tmp_path: Path) -> None:
    first = v3_managed_document(tmp_path)
    second = v3_managed_document(tmp_path)
    second_target = second["target"]
    assert isinstance(second_target, dict)
    second_launch = second_target["launch"]
    assert isinstance(second_launch, dict)
    second_options = second_launch["options"]
    assert isinstance(second_options, dict)
    second_launch["options"] = dict(reversed(tuple(second_options.items())))

    first_source = tmp_path / "first-option-order.yaml"
    second_source = tmp_path / "second-option-order.yaml"
    first_source.write_text(yaml.safe_dump(first, sort_keys=False), encoding="utf-8")
    second_source.write_text(yaml.safe_dump(second, sort_keys=False), encoding="utf-8")
    first_config = load_optimization_config(first_source)
    second_config = load_optimization_config(second_source)

    assert first_config.target_launch_argv == second_config.target_launch_argv
    assert (
        scenario_identity(first_config, "same-hardware").spec_digest
        == scenario_identity(second_config, "same-hardware").spec_digest
    )


def test_launch_facets_follow_backend_and_speculative_option_families() -> None:
    assert derive_sglang_launch_facets(
        {
            "--future-collective-backend": "NEW_BACKEND",
            "--speculative-future-mode": "experimental",
            "--unclassified-kernel-switch": True,
            "--disabled-backend": False,
        }
    ) == {
        "backends": {"future_collective": "new_backend"},
        "speculative": {"future_mode": "experimental"},
    }


def test_unclassified_launch_option_changes_spec_but_not_compatibility_digest(
    tmp_path: Path,
) -> None:
    baseline = v3_managed_document(tmp_path)
    changed = v3_managed_document(tmp_path)
    target = changed["target"]
    assert isinstance(target, dict)
    launch = target["launch"]
    assert isinstance(launch, dict)
    options = launch["options"]
    assert isinstance(options, dict)
    options["--unclassified-kernel-switch"] = True
    baseline_source = tmp_path / "baseline-options.yaml"
    changed_source = tmp_path / "changed-options.yaml"
    baseline_source.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
    changed_source.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")

    baseline_identity = scenario_identity(
        load_optimization_config(baseline_source), "same-hardware"
    )
    changed_identity = scenario_identity(load_optimization_config(changed_source), "same-hardware")

    assert changed_identity.spec_digest != baseline_identity.spec_digest
    assert changed_identity.compatibility_digest == baseline_identity.compatibility_digest


def test_v3_rejects_removed_id_field(tmp_path: Path) -> None:
    document = v3_managed_document(tmp_path)
    models = document["models"]
    assert isinstance(models, dict)
    target = models["target"]
    assert isinstance(target, dict)
    target["id"] = target.pop("name")
    source = tmp_path / "legacy-id.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match=r"unknown field\(s\): id"):
        load_optimization_config(source)


def test_v3_rejects_removed_serving_field(tmp_path: Path) -> None:
    document = v3_managed_document(tmp_path)
    target = document["target"]
    assert isinstance(target, dict)
    target["serving"] = {
        "backends": {"kv_cache_dtype": "bfloat16"},
        "speculative": {"algorithm": "off"},
    }
    source = tmp_path / "campaign-v3.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match=r"unknown field\(s\): serving"):
        load_optimization_config(source)


def test_v3_managed_target_rejects_duplicate_baseline_target_parameters(
    tmp_path: Path,
) -> None:
    document = v3_managed_document(tmp_path)
    baseline = document["baseline"]
    assert isinstance(baseline, dict)
    baseline["target_parameters"] = {"tp_size": 8}
    source = tmp_path / "duplicate-target-parameters.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OptimizationConfigError, match=r"target\.launch\.options"):
        load_optimization_config(source)


def test_loads_complete_managed_target_config(tmp_path: Path) -> None:
    config = load_optimization_config(write_config(tmp_path, config_with_target(FULL_TARGET)))

    assert isinstance(config.target, ManagedTargetConfig)
    assert config.target.provider is Framework.SGLANG
    assert config.target.launch == TargetLaunchConfig(
        python="python3",
        python_options=("-u",),
        module="sglang.launch_server",
        options={
            "--disable-overlap": False,
            "--tp-size": 8,
            "--trust-remote-code": True,
        },
        extra_argv=("--custom-flag",),
        env={"SGLANG_LOG_LEVEL": "info", "OPTIONAL_SECRET": None},
    )
    assert config.target_launch_argv == (
        "python3",
        "-u",
        "-m",
        "sglang.launch_server",
        "--model-path",
        "test/model",
        "--served-model-name",
        "test/model",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--tp-size",
        "8",
        "--trust-remote-code",
        "--custom-flag",
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
            "    options: {}\n",
            "    options: {}\n    shell: true\n",
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
    ("launch_yaml", "message"),
    [
        ("    python_options: unsafe-command-string\n", "list"),
        ("    python_options: [-c]\n", r"\[\] or \[-u\]"),
        ('    python: "python\\0bad"\n', "NUL"),
        ("    python: bash\n", "Python executable"),
        ("    module: unsafe.server\n", "approved SGLang server"),
        ("    options:\n      --tp-size: [8]\n", "string, number, boolean, or null"),
    ],
)
def test_target_launch_rejects_unsafe_structured_values(
    tmp_path: Path, launch_yaml: str, message: str
) -> None:
    target = MINIMAL_TARGET.replace("    options: {}\n", launch_yaml, 1)

    with pytest.raises(OptimizationConfigError, match=message):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


def test_target_launch_rejects_removed_argv_field(tmp_path: Path) -> None:
    target = MINIMAL_TARGET.replace(
        "    options: {}\n",
        "    argv: [python3, -m, sglang.launch_server]\n",
        1,
    )

    with pytest.raises(OptimizationConfigError, match=r"unknown field\(s\): argv"):
        load_optimization_config(write_config(tmp_path, config_with_target(target)))


@pytest.mark.parametrize(
    "launch_yaml",
    [
        "    options:\n      --model-path: other/model\n",
        "    extra_argv: [--port, '40000']\n",
    ],
)
def test_target_launch_rejects_overrides_of_generated_options(
    tmp_path: Path, launch_yaml: str
) -> None:
    target = MINIMAL_TARGET.replace("    options: {}\n", launch_yaml, 1)

    with pytest.raises(OptimizationConfigError, match="generated"):
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
        ("provider: sglang_torch", "provider: nsys", "must be 'sglang_torch'"),
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


def test_execution_storage_and_workspace_root_are_host_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    contents = VALID_CONFIG.rsplit("execution:\n", 1)[0]
    config = load_optimization_config(write_config(tmp_path, contents))

    assert config.execution.artifacts_dir == tmp_path / ".euboulia"

    document = v3_managed_document(tmp_path)
    document.pop("execution")
    optimization = document["optimization"]
    assert isinstance(optimization, dict)
    workspace = optimization["workspace"]
    assert isinstance(workspace, dict)
    workspace.pop("root_dir")
    source = tmp_path / "host-independent.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    managed = load_optimization_config(source)

    assert managed.optimization.workspace is not None
    assert managed.optimization.workspace.root_dir == tmp_path / ".euboulia/worktrees"
