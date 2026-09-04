from pathlib import Path

import pytest
import yaml

from euboulia.optimization.config import load_optimization_config
from euboulia.optimization.runner import OptimizationRunner, OptimizationRuntimeError


def _external_config(tmp_path: Path) -> Path:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("schema_version: 1\nentries: []\n", encoding="utf-8")
    document = {
        "schema_version": 2,
        "name": "external-target-is-not-supported",
        "framework": "sglang",
        "workload": {
            "name": "fixed",
            "model": "test/model",
            "input_tokens": 128,
            "output_tokens": 32,
            "concurrency": 1,
            "num_prompts": 1,
            "endpoint": "http://127.0.0.1:30000",
        },
        "benchmark": {"mode": "serve"},
        "baseline": {"source_revision": "HEAD"},
        "optimization": {
            "profiling": {
                "provider": "sglang_torch",
                "workload_point": "fixed",
            },
            "planner": {"provider": "rules", "patch_catalog": str(catalog)},
            "workspace": {
                "repository": str(tmp_path),
                "root_dir": str(tmp_path / "worktrees"),
            },
            "evaluation": {
                "metric": "throughput",
                "direction": "maximize",
                "metrics_path": "metrics.json",
                "tiers": [
                    {
                        "kind": "correctness",
                        "timeout_seconds": 10,
                        "commands": [{"name": "correct", "argv": ["true"]}],
                    },
                    {
                        "kind": "performance",
                        "timeout_seconds": 10,
                        "commands": [{"name": "bench", "argv": ["true"]}],
                    },
                ],
            },
            "budget": {"max_iterations": 1, "max_wall_time_seconds": 10},
        },
        "execution": {"artifacts_dir": str(tmp_path / "artifacts")},
    }
    path = tmp_path / "external.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_plan_rejects_external_profile_compatibility_path(tmp_path: Path) -> None:
    config = load_optimization_config(_external_config(tmp_path))

    with pytest.raises(OptimizationRuntimeError, match="managed target"):
        OptimizationRunner().plan(config)

    assert not config.execution.artifacts_dir.exists()


def test_run_rejects_external_profile_before_writing(tmp_path: Path) -> None:
    config = load_optimization_config(_external_config(tmp_path))

    with pytest.raises(OptimizationRuntimeError, match="managed SGLang target"):
        OptimizationRunner().run(config, name="external")

    assert not config.execution.artifacts_dir.exists()
