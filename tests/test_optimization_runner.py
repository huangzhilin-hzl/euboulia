from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from euboulia.optimization.config import load_optimization_config
from euboulia.optimization.contracts import Capability, RunState
from euboulia.optimization.events import EventLedger, EventType
from euboulia.optimization.memory import SQLiteMemoryStore
from euboulia.optimization.runner import OptimizationRunner, OptimizationRuntimeError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _project(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "euboulia@example.invalid")
    _git(repository, "config", "user.name", "Euboulia Tests")
    (repository / "value.txt").write_text("old\n", encoding="utf-8")
    _git(repository, "add", "value.txt")
    _git(repository, "commit", "-m", "baseline")

    patch = tmp_path / "candidate.diff"
    patch.write_text(
        """diff --git a/value.txt b/value.txt
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
""",
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """schema_version: 1
entries:
  - id: batch-launches
    title: Batch short launches
    rationale: Reduce repeated launch overhead.
    triggers: [launch]
    patch: candidate.diff
    predicted_metric: throughput
    risk: low
""",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text(
        json.dumps(
            {
                "traceEvents": [
                    {
                        "name": "decode_kernel",
                        "cat": "kernel",
                        "ph": "X",
                        "ts": index * 10,
                        "dur": 5,
                        "pid": 1,
                        "tid": 2,
                    }
                    for index in range(110)
                ]
            }
        ),
        encoding="utf-8",
    )
    correctness_source = (
        "import pathlib; raise SystemExit(0 if pathlib.Path('value.txt').read_text() "
        "== 'new\\n' else 1)"
    )
    benchmark_source = (
        "import json,pathlib; pathlib.Path('metrics.json').write_text("
        "json.dumps({'throughput': 110.0}))"
    )
    config = {
        "schema_version": 2,
        "name": "runner-test",
        "framework": "vllm",
        "workload": {
            "name": "fixed",
            "model": "test/model",
            "input_tokens": 128,
            "output_tokens": 32,
            "concurrency": 4,
            "num_prompts": 16,
            "endpoint": "http://127.0.0.1:8000",
            "dataset": "random",
        },
        "benchmark": {"mode": "serve", "result_filename": "result.json"},
        "baseline": {
            "id": "baseline",
            "source_revision": "HEAD",
            "target_parameters": {},
        },
        "optimization": {
            "profiles": {
                "provider": "imported",
                "artifacts": [{"path": str(trace), "source": "torch_chrome_trace"}],
            },
            "planner": {
                "provider": "rules",
                "patch_catalog": str(catalog),
                "max_proposals_per_iteration": 1,
                "reject_duplicate_diffs": True,
            },
            "workspace": {
                "repository": str(repository),
                "root_dir": str(tmp_path / "worktrees"),
                "max_changed_files": 2,
                "max_changed_lines": 20,
            },
            "evaluation": {
                "metric": "throughput",
                "direction": "maximize",
                "baseline_value": 100.0,
                "metrics_path": "metrics.json",
                "min_relative_improvement": 0.05,
                "tiers": [
                    {
                        "kind": "correctness",
                        "warmups": 0,
                        "repetitions": 1,
                        "timeout_seconds": 10,
                        "commands": [
                            {
                                "name": "correctness",
                                "argv": [sys.executable, "-c", correctness_source],
                            }
                        ],
                    },
                    {
                        "kind": "performance",
                        "warmups": 1,
                        "repetitions": 3,
                        "timeout_seconds": 10,
                        "commands": [
                            {
                                "name": "benchmark",
                                "argv": [sys.executable, "-c", benchmark_source],
                            }
                        ],
                    },
                ],
            },
            "budget": {
                "max_iterations": 2,
                "max_wall_time_seconds": 60,
                "max_consecutive_failures": 2,
                "no_improvement_patience": 2,
                "max_profile_bytes": 1_000_000,
            },
        },
        "execution": {"artifacts_dir": str(tmp_path / "artifacts")},
    }
    config_path = tmp_path / "optimization.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def test_plan_is_read_only_and_run_waits_at_capability_boundary(tmp_path: Path) -> None:
    config = load_optimization_config(_project(tmp_path))
    runner = OptimizationRunner()
    workspace = config.optimization.workspace
    assert workspace is not None

    plan = runner.plan(config)

    assert plan.proposals[0].catalog_entry_id == "batch-launches"
    assert not config.execution.artifacts_dir.exists()
    assert not workspace.root_dir.exists()

    result = runner.run(config, run_id="waiting-run")

    assert result.run_state is RunState.WAITING_FOR_APPROVAL
    assert result.waiting_for_approval is True
    assert not workspace.root_dir.exists()
    assert EventLedger(config.execution.event_ledger).latest("waiting-run").event_type is (
        EventType.APPROVAL_REQUESTED
    )
    with pytest.raises(OptimizationRuntimeError, match="resume is not implemented"):
        runner.run(config, run_id="waiting-run")


def test_external_service_mode_excludes_server_argument_changes(tmp_path: Path) -> None:
    config_path = _project(tmp_path)
    (tmp_path / "catalog.yaml").write_text(
        """schema_version: 1
entries:
  - id: managed-only-arguments
    title: Managed target arguments
    rationale: Requires ownership of the server launch.
    triggers: [launch]
    server_args:
      set:
        --max-running-requests: 64
""",
        encoding="utf-8",
    )
    config = load_optimization_config(config_path)

    plan = OptimizationRunner().plan(config)

    assert plan.proposals == ()
    assert any("server-argument changes are ineligible" in item for item in plan.warnings)


def test_runner_applies_in_detached_workspace_and_records_accepted_memory(
    tmp_path: Path,
) -> None:
    config = load_optimization_config(_project(tmp_path))
    workspace = config.optimization.workspace
    assert workspace is not None
    repository = workspace.repository

    result = OptimizationRunner().run(
        config,
        frozenset({Capability.WORKSPACE_WRITE, Capability.BENCHMARK_EXECUTION}),
        run_id="accepted-run",
    )

    assert result.run_state is RunState.COMPLETED
    assert len(result.outcomes) == 1
    assert result.outcomes[0].accepted is True
    assert (repository / "value.txt").read_text(encoding="utf-8") == "old\n"
    candidate = workspace.root_dir / "accepted-run/iteration-001/worktree/value.txt"
    assert candidate.read_text(encoding="utf-8") == "new\n"
    assert len(SQLiteMemoryStore(config.execution.memory)) == 1
    events = EventLedger(config.execution.event_ledger).by_run("accepted-run")
    event_types = {event.event_type for event in events}
    assert {
        EventType.PROFILE_COMPLETED,
        EventType.ANALYSIS_COMPLETED,
        EventType.PATCH_APPLIED,
        EventType.EVALUATION_COMPLETED,
        EventType.CHAMPION_UPDATED,
        EventType.RUN_COMPLETED,
    } <= event_types
