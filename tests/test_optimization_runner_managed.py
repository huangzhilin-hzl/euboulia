from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from euboulia.execution import ExecutionResult
from euboulia.optimization.config import OptimizationConfig, load_optimization_config
from euboulia.optimization.contracts import Capability, IterationState, RunState
from euboulia.optimization.evaluator import TieredEvaluator
from euboulia.optimization.events import EventLedger, EventType
from euboulia.optimization.runner import OptimizationRunner
from euboulia.optimization.target import (
    BuildSpec,
    ServiceHandle,
    TargetChangeSet,
    TargetSpec,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

_ALL_MANAGED_CAPABILITIES = frozenset(
    {
        Capability.WORKSPACE_WRITE,
        Capability.BENCHMARK_EXECUTION,
        Capability.BUILD_EXECUTION,
        Capability.OWNED_SERVICE_LIFECYCLE,
    }
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _managed_project(tmp_path: Path) -> OptimizationConfig:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "euboulia@example.invalid")
    _git(repository, "config", "user.name", "Euboulia Tests")
    (repository / "source.txt").write_text("same pinned source\n", encoding="utf-8")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-m", "baseline")
    source_revision = _git(repository, "rev-parse", "HEAD")

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
    catalog = tmp_path / "changes.yaml"
    catalog.write_text(
        """schema_version: 1
entries:
  - id: scheduler-arguments
    title: Tune scheduler arguments
    rationale: Match scheduler concurrency to the observed launch pressure.
    triggers: [launch]
    predicted_metric: throughput
    risk: low
    server_args:
      set:
        --max-running-requests: 64
        --schedule-policy: lpm
      remove: [--disable-cuda-graph]
""",
        encoding="utf-8",
    )

    correctness_source = (
        "import os; "
        "assert os.environ['EUBOULIA_TARGET_ENDPOINT'] == 'http://127.0.0.1:30000'; "
        "assert os.environ['EUBOULIA_TARGET_ROLE'] in "
        "{'baseline', 'candidate', 'baseline-validation'}; "
        "assert os.environ['EUBOULIA_FRAMEWORK'] == 'sglang'; "
        "assert os.environ['EUBOULIA_MODEL'] == 'test/model'; "
        "assert os.environ['EUBOULIA_INPUT_TOKENS'] == '128'; "
        "assert os.environ['EUBOULIA_OUTPUT_TOKENS'] == '32'; "
        "assert os.environ['EUBOULIA_CONCURRENCY'] == '4'; "
        "assert os.environ['EUBOULIA_NUM_PROMPTS'] == '16'; "
        "assert os.environ['EUBOULIA_DATASET'] == 'random'; "
        "assert os.environ['EUBOULIA_METRICS_PATH'] == 'metrics.json'"
    )
    benchmark_source = (
        "import json,os,pathlib; "
        "role=os.environ['EUBOULIA_TARGET_ROLE']; "
        "value=100.0 if role=='baseline' else 106.0; "
        "pathlib.Path('metrics.json').write_text(json.dumps({'throughput': value}))"
    )
    config = {
        "schema_version": 2,
        "name": "managed-runner-test",
        "framework": "sglang",
        "target": {
            "provider": "sglang",
            "launch": {
                "python": sys.executable,
                "module": "sglang.launch_server",
                "options": {
                    "--schedule-policy": "fcfs",
                    "--disable-cuda-graph": True,
                },
            },
            "readiness": {
                "url": "http://127.0.0.1:30000/health_generate",
                "timeout_seconds": 1,
                "interval_seconds": 0.01,
            },
            "shutdown_timeout_seconds": 1,
            "gpus": [0],
            "build": {
                "commands": [
                    {
                        "name": "declared-build",
                        "argv": [sys.executable, "-c", "pass"],
                        "timeout_seconds": 1,
                    }
                ]
            },
            "provenance": {"source": "user-reviewed-test-declaration"},
        },
        "workload": {
            "name": "fixed",
            "model": "test/model",
            "input_tokens": 128,
            "output_tokens": 32,
            "concurrency": 4,
            "num_prompts": 16,
            "endpoint": "http://127.0.0.1:30000",
            "dataset": "random",
        },
        "benchmark": {"mode": "serve", "result_filename": "result.json"},
        "baseline": {
            "name": "baseline",
            "source_revision": source_revision,
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
                "baseline_value": 1000.0,
                "metrics_path": "metrics.json",
                "min_relative_improvement": 0.05,
                "tiers": [
                    {
                        "kind": "correctness",
                        "warmups": 0,
                        "repetitions": 1,
                        "timeout_seconds": 2,
                        "commands": [
                            {
                                "name": "correctness",
                                "argv": [sys.executable, "-c", correctness_source],
                            }
                        ],
                    },
                    {
                        "kind": "performance",
                        "warmups": 0,
                        "repetitions": 1,
                        "timeout_seconds": 2,
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
                "max_iterations": 1,
                "max_wall_time_seconds": 30,
                "max_consecutive_failures": 1,
                "no_improvement_patience": 1,
                "max_profile_bytes": 1_000_000,
            },
        },
        "execution": {"artifacts_dir": str(tmp_path / "artifacts")},
    }
    config_path = tmp_path / "optimization.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return load_optimization_config(config_path)


def _managed_v3_project(tmp_path: Path) -> OptimizationConfig:
    _managed_project(tmp_path)
    config_path = tmp_path / "optimization.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workload = document.pop("workload")
    assert isinstance(workload, dict)
    document["schema_version"] = 3
    document["models"] = {
        "target": {
            "name": "target",
            "path": workload["model"],
            "served_name": workload["model"],
            "revision": "e" * 40,
        }
    }
    document["endpoint"] = workload["endpoint"]
    document["workload_suite"] = {
        "name": "two-point-suite",
        "dataset": "random",
        "request_rate": "inf",
        "points": [
            {
                "name": "short-c4",
                "input_tokens": 128,
                "output_tokens": 32,
                "concurrency": 4,
                "num_prompts": 16,
            },
            {
                "name": "long-c8",
                "input_tokens": 512,
                "output_tokens": 64,
                "concurrency": 8,
                "num_prompts": 32,
            },
        ],
    }
    target = document["target"]
    assert isinstance(target, dict)
    target.pop("provenance")
    baseline = document["baseline"]
    assert isinstance(baseline, dict)
    source_revision = baseline["source_revision"]
    assert isinstance(source_revision, str)
    document["inputs"] = {
        "container_image": {"type": "container_digest", "required": True},
        "sglang_revision": {"type": "git_commit", "required": True},
    }
    baseline["source_revision"] = "${sglang_revision}"
    target["runtime"] = {
        "expected": {
            "container": {
                "image": "${container_image}",
            },
            "components": {
                "python": {"version": platform.python_version()},
                "sglang": {"revision": "${sglang_revision}", "dirty": False},
            },
        },
        "capture": {"require_observed": False, "fail_on_mismatch": True},
    }
    evaluation = document["optimization"]["evaluation"]
    evaluation.pop("baseline_value")
    evaluation.pop("min_relative_improvement")
    evaluation["promotion"] = {
        "primary_points": ["short-c4", "long-c8"],
        "min_relative_improvement": 0.05,
        "max_regression_per_point": 0.0,
        "noise_tolerance": 0.0,
    }
    correctness = evaluation["tiers"][0]["commands"][0]
    correctness["argv"] = [
        sys.executable,
        "-c",
        (
            "import os; "
            "assert os.environ['EUBOULIA_WORKLOAD_POINT_NAME']=='short-c4'; "
            "assert os.environ['EUBOULIA_MODEL_SERVED_NAME']=='test/model'"
        ),
    ]
    benchmark = evaluation["tiers"][1]["commands"][0]
    benchmark["argv"] = [
        sys.executable,
        "-c",
        (
            "import json,os,pathlib; "
            "role=os.environ['EUBOULIA_TARGET_ROLE']; "
            "value=100.0 if role=='baseline' else 106.0; "
            "path=pathlib.Path(os.environ['EUBOULIA_METRICS_PATH']); "
            "path.parent.mkdir(parents=True,exist_ok=True); "
            "path.write_text(json.dumps({'throughput':value}))"
        ),
    ]
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    values_path = tmp_path / "values.yaml"
    values_path.write_text(
        yaml.safe_dump(
            {
                "container_image": "registry.example/test@sha256:" + "a" * 64,
                "sglang_revision": source_revision,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return load_optimization_config(config_path, values_path)


class FakeTargetController:
    def __init__(
        self,
        *,
        fail_readiness_role: str | None = None,
        fail_stop_role: str | None = None,
    ) -> None:
        self.fail_readiness_role = fail_readiness_role
        self.fail_stop_role = fail_stop_role
        self.calls: list[str] = []
        self.build_workspaces: dict[str, Path] = {}
        self.start_workspaces: dict[str, Path] = {}
        self.change_sets: dict[str, TargetChangeSet] = {}
        self.argv: dict[str, tuple[str, ...]] = {}
        self.handles: dict[str, ServiceHandle] = {}
        self.stop_counts: Counter[str] = Counter()

    @staticmethod
    def _role(workspace: Path) -> str:
        role = Path(workspace).parent.name
        if role not in {"baseline", "candidate"}:
            raise AssertionError(f"unexpected managed worktree role: {role}")
        return role

    def build(
        self,
        workspace: Path,
        spec: BuildSpec,
        evidence_dir: Path,
    ) -> tuple[ExecutionResult, ...]:
        role = self._role(workspace)
        self.calls.append(f"build:{role}")
        self.build_workspaces[role] = Path(workspace)
        assert len(spec.commands) == 1
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "fake-build.json").write_text(
            json.dumps({"role": role, "workspace": str(workspace)}),
            encoding="utf-8",
        )
        return ()

    def start(
        self,
        workspace: Path,
        spec: TargetSpec,
        change_set: TargetChangeSet,
        evidence_dir: Path,
        *,
        run_id: str,
        trial_id: str,
    ) -> ServiceHandle:
        role = self._role(workspace)
        self.calls.append(f"start:{role}")
        self.start_workspaces[role] = Path(workspace)
        self.change_sets[role] = change_set
        self.argv[role] = change_set.arg_patch.apply(spec.launch_argv)
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence / "stdout.log"
        stderr_path = evidence / "stderr.log"
        manifest_path = evidence / "service.json"
        stdout_path.write_text(f"fake {role} stdout\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        manifest_path.write_text(json.dumps({"role": role}), encoding="utf-8")
        sequence = len(self.handles) + 1
        handle = ServiceHandle(
            handle_id=f"fake-{role}-{sequence}",
            pid=10_000 + sequence,
            process_group_id=10_000 + sequence,
            process_start_identity=f"fake-start-{sequence}",
            run_id=run_id,
            trial_id=trial_id,
            argv_digest=f"fake-argv-{sequence}",
            endpoint=spec.endpoint,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            manifest_path=manifest_path,
            started_at="2026-01-01T00:00:00Z",
        )
        self.handles[role] = handle
        return handle

    def wait_ready(self, handle: ServiceHandle) -> None:
        role = handle.handle_id.split("-")[1]
        self.calls.append(f"ready:{role}")
        if role == self.fail_readiness_role:
            raise RuntimeError(f"fake {role} readiness failure")

    def stop(self, handle: ServiceHandle) -> None:
        role = handle.handle_id.split("-")[1]
        self.calls.append(f"stop:{role}")
        self.stop_counts[role] += 1
        if role == self.fail_stop_role:
            raise RuntimeError(f"fake {role} stop failure")


def test_managed_args_only_trial_uses_fresh_services_and_measured_baseline(
    tmp_path: Path,
) -> None:
    config = _managed_project(tmp_path)
    controller = FakeTargetController()

    result = OptimizationRunner(controller).run(
        config,
        _ALL_MANAGED_CAPABILITIES,
        run_id="managed-accepted",
    )

    assert result.run_state is RunState.COMPLETED
    assert result.outcomes[0].accepted is True
    assert result.outcomes[0].relative_improvement == pytest.approx(0.06)
    assert result.outcomes[0].metadata["baseline_evaluation"]["objective_value"] == 100.0
    assert config.optimization.evaluation.baseline_value == 1000.0
    assert controller.calls == [
        "build:baseline",
        "start:baseline",
        "ready:baseline",
        "stop:baseline",
        "build:candidate",
        "start:candidate",
        "ready:candidate",
        "stop:candidate",
    ]
    assert controller.build_workspaces["baseline"] != controller.build_workspaces["candidate"]
    assert controller.start_workspaces == controller.build_workspaces
    assert controller.handles["baseline"] is not controller.handles["candidate"]
    assert controller.handles["baseline"].handle_id != controller.handles["candidate"].handle_id
    assert controller.change_sets["baseline"] == TargetChangeSet()
    assert controller.change_sets["candidate"].source_patch_path is None
    assert controller.argv["baseline"] == (
        sys.executable,
        "-m",
        "sglang.launch_server",
        "--model-path",
        "test/model",
        "--served-model-name",
        "test/model",
        "--host",
        "127.0.0.1",
        "--port",
        "30000",
        "--disable-cuda-graph",
        "--schedule-policy",
        "fcfs",
    )
    assert controller.argv["candidate"] == (
        sys.executable,
        "-m",
        "sglang.launch_server",
        "--model-path",
        "test/model",
        "--served-model-name",
        "test/model",
        "--host",
        "127.0.0.1",
        "--port",
        "30000",
        "--schedule-policy",
        "lpm",
        "--max-running-requests",
        "64",
    )
    assert controller.stop_counts == Counter({"baseline": 1, "candidate": 1})
    service_events = [
        (event.event_type, event.payload.get("role"))
        for event in EventLedger(config.execution.event_ledger).by_run(result.run_id)
        if event.event_type
        in {
            EventType.SERVICE_STARTING,
            EventType.SERVICE_STARTED,
            EventType.SERVICE_READY,
            EventType.SERVICE_STOPPING,
            EventType.SERVICE_STOPPED,
        }
    ]
    assert service_events == [
        (EventType.SERVICE_STARTING, "baseline"),
        (EventType.SERVICE_STARTED, "baseline"),
        (EventType.SERVICE_READY, "baseline"),
        (EventType.SERVICE_STOPPING, "baseline"),
        (EventType.SERVICE_STOPPED, "baseline"),
        (EventType.SERVICE_STARTING, "candidate"),
        (EventType.SERVICE_STARTED, "candidate"),
        (EventType.SERVICE_READY, "candidate"),
        (EventType.SERVICE_STOPPING, "candidate"),
        (EventType.SERVICE_STOPPED, "candidate"),
    ]


def test_target_validation_runs_one_baseline_without_candidate(tmp_path: Path) -> None:
    config = _managed_project(tmp_path)
    controller = FakeTargetController()

    result = OptimizationRunner(controller).validate_baseline(
        config,
        _ALL_MANAGED_CAPABILITIES,
        run_id="baseline-only",
    )

    assert result.passed is True
    assert result.run_uid.startswith("run-")
    assert result.run_uid != result.run_id
    assert result.evaluation.objective_value == 106.0
    assert controller.calls == [
        "build:baseline",
        "start:baseline",
        "ready:baseline",
        "stop:baseline",
    ]
    assert (result.artifact_dir / "validation.json").is_file()
    resolved_recipe = result.artifact_dir / "resolved-recipe.yaml"
    assert resolved_recipe.is_file()
    resolved_text = resolved_recipe.read_text(encoding="utf-8")
    assert "inputs:" not in resolved_text
    assert "${" not in resolved_text
    assert (result.artifact_dir / "logs" / "server.log").is_file()


def test_managed_v3_runs_two_points_per_service_and_captures_runtime(
    tmp_path: Path,
) -> None:
    config = _managed_v3_project(tmp_path)
    controller = FakeTargetController()

    result = OptimizationRunner(controller).run(
        config,
        _ALL_MANAGED_CAPABILITIES,
        run_id="managed-v3-suite",
    )

    assert result.outcomes[0].accepted is True
    assert result.outcomes[0].relative_improvement == pytest.approx(0.06)
    baseline = result.outcomes[0].metadata["baseline_evaluation"]
    candidate = result.outcomes[0].metadata["evaluation"]
    assert baseline["objective_values"] == {"short-c4": 100.0, "long-c8": 100.0}
    assert candidate["objective_values"] == {"short-c4": 106.0, "long-c8": 106.0}
    assert candidate["relative_improvements"] == pytest.approx({"short-c4": 0.06, "long-c8": 0.06})
    second_stages = candidate["point_results"]["long-c8"]["stages"]
    correctness_stage = next(stage for stage in second_stages if stage["stage"] == "correctness")
    assert correctness_stage["executions"] == []
    events = EventLedger(config.execution.event_ledger).by_run(result.run_id)
    runtime_events = [
        event for event in events if event.event_type is EventType.RUNTIME_PROVENANCE_CAPTURED
    ]
    assert len(runtime_events) == 1
    assert runtime_events[0].payload["valid"] is True
    assert controller.calls.count("start:baseline") == 1
    assert controller.calls.count("start:candidate") == 1


def test_managed_composite_change_applies_patch_and_argument_overlay(tmp_path: Path) -> None:
    config = _managed_project(tmp_path)
    patch = tmp_path / "candidate.diff"
    patch.write_text(
        """diff --git a/source.txt b/source.txt
--- a/source.txt
+++ b/source.txt
@@ -1 +1 @@
-same pinned source
+candidate source
""",
        encoding="utf-8",
    )
    (tmp_path / "changes.yaml").write_text(
        """schema_version: 1
entries:
  - id: composite-change
    title: Composite change
    rationale: Exercise source and launch changes atomically.
    triggers: [launch]
    patch: candidate.diff
    server_args:
      set:
        --schedule-policy: lpm
""",
        encoding="utf-8",
    )
    controller = FakeTargetController()

    result = OptimizationRunner(controller).run(
        config,
        _ALL_MANAGED_CAPABILITIES,
        run_id="managed-composite",
    )

    assert result.outcomes[0].accepted is True
    workspace = config.optimization.workspace
    assert workspace is not None
    candidate = workspace.root_dir / "managed-composite/iteration-001/candidate/worktree"
    assert (candidate / "source.txt").read_text(encoding="utf-8") == "candidate source\n"
    assert (workspace.repository / "source.txt").read_text(encoding="utf-8") == (
        "same pinned source\n"
    )
    change_set = controller.change_sets["candidate"]
    assert change_set.source_patch_path is not None
    assert change_set.source_patch_path.is_file()
    assert "--schedule-policy" in controller.argv["candidate"]
    assert "lpm" in controller.argv["candidate"]


@pytest.mark.parametrize(
    "missing",
    [Capability.OWNED_SERVICE_LIFECYCLE, Capability.BUILD_EXECUTION],
)
def test_missing_managed_capability_waits_before_workspace_or_controller_call(
    tmp_path: Path,
    missing: Capability,
) -> None:
    config = _managed_project(tmp_path)
    controller = FakeTargetController()
    workspace = config.optimization.workspace
    assert workspace is not None

    result = OptimizationRunner(controller).run(
        config,
        _ALL_MANAGED_CAPABILITIES - {missing},
        run_id=f"missing-{missing.value}",
    )

    assert result.run_state is RunState.WAITING_FOR_APPROVAL
    assert result.iteration_state is IterationState.WAITING_FOR_APPROVAL
    assert controller.calls == []
    assert not workspace.root_dir.exists()
    latest = EventLedger(config.execution.event_ledger).latest(result.run_id)
    assert latest is not None
    assert latest.event_type is EventType.APPROVAL_REQUESTED
    assert latest.payload["missing_capabilities"] == [missing.value]


@pytest.mark.parametrize("failure_stage", ["readiness", "evaluation"])
def test_managed_candidate_failure_stops_current_handle_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    config = _managed_project(tmp_path)
    controller = FakeTargetController(
        fail_readiness_role="candidate" if failure_stage == "readiness" else None
    )
    expected_message = "fake candidate readiness failure"
    if failure_stage == "evaluation":
        real_execute = TieredEvaluator.execute
        execution_count = 0

        def fail_candidate_evaluation(self: TieredEvaluator, authorization: object) -> object:
            nonlocal execution_count
            execution_count += 1
            if execution_count == 2:
                raise RuntimeError("fake candidate evaluation failure")
            return real_execute(self, authorization)  # type: ignore[arg-type]

        monkeypatch.setattr(TieredEvaluator, "execute", fail_candidate_evaluation)
        expected_message = "fake candidate evaluation failure"

    with pytest.raises(RuntimeError, match=expected_message):
        OptimizationRunner(controller).run(
            config,
            _ALL_MANAGED_CAPABILITIES,
            run_id=f"candidate-{failure_stage}-failure",
        )

    assert controller.stop_counts == Counter({"baseline": 1, "candidate": 1})
    assert controller.calls.count("stop:candidate") == 1
    assert controller.handles["baseline"] is not controller.handles["candidate"]


def test_baseline_stop_failure_prevents_candidate_start(tmp_path: Path) -> None:
    config = _managed_project(tmp_path)
    controller = FakeTargetController(fail_stop_role="baseline")
    workspace = config.optimization.workspace
    assert workspace is not None

    with pytest.raises(RuntimeError, match="fake baseline stop failure"):
        OptimizationRunner(controller).run(
            config,
            _ALL_MANAGED_CAPABILITIES,
            run_id="baseline-stop-failure",
        )

    assert controller.calls == [
        "build:baseline",
        "start:baseline",
        "ready:baseline",
        "stop:baseline",
    ]
    assert controller.stop_counts == Counter({"baseline": 1})
    assert set(controller.handles) == {"baseline"}
    candidate_root = workspace.root_dir / "baseline-stop-failure/iteration-001/candidate"
    assert not candidate_root.exists()
