import json
import os
from pathlib import Path

import pytest
import yaml
from test_optimization_config import write_input_template

from euboulia.control import ControlError, ControlStore, TaskManager, controller_log_path
from euboulia.optimization.config import (
    dump_resolved_optimization_config,
    load_optimization_config,
    resolve_optimization_config,
)
from euboulia.remote import KubernetesTargetSupervisor, RemoteExecutionError


def _runtime(tmp_path: Path) -> Path:
    template = tmp_path / "pod-template.yaml"
    template.write_text(
        "apiVersion: v1\n"
        "kind: Pod\n"
        "metadata: {}\n"
        "spec:\n"
        "  containers:\n"
        "    - name: runtime\n"
        "      image: placeholder.invalid/image\n",
        encoding="utf-8",
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "storage": {"root": str(tmp_path / "local-state")},
                "executors": {
                    "gpu": {
                        "type": "kubernetes",
                        "namespace": "private-inference",
                        "pod_template": str(template),
                        "container": "runtime",
                        "project_dir": "/workspace/euboulia",
                        "scratch_dir": "/scratch/euboulia",
                        "local_project_dir": str(tmp_path),
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _lock(tmp_path: Path) -> tuple[Path, Path]:
    template, values = write_input_template(tmp_path)
    resolution = resolve_optimization_config(template, values)
    assert resolution.config is not None
    lock = tmp_path / "recipe.lock.yaml"
    lock.write_text(
        dump_resolved_optimization_config(resolution.config, destination=lock),
        encoding="utf-8",
    )
    return lock, values


def test_submission_keeps_only_a_private_immutable_recipe_lock(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source_lock, values = _lock(tmp_path)
    manager = TaskManager(runtime)

    run = manager.submit(
        recipe=source_lock,
        executor="gpu",
        node="worker-8",
        name="baseline",
    )

    assert run.status == "queued"
    assert run.phase == "submitted"
    assert run.name == "baseline"
    assert run.recipe_path != source_lock
    assert run.recipe_path.is_file()
    assert run.recipe_path.stat().st_mode & 0o777 == 0o600
    submitted = load_optimization_config(run.recipe_path)
    source = load_optimization_config(source_lock)
    assert submitted.baseline.source_revision == source.baseline.source_revision
    assert submitted.input_bindings == source.input_bindings == {}
    assert submitted.target is not None and source.target is not None
    assert submitted.target.runtime.expected == source.target.runtime.expected
    assert values.name not in run.recipe_path.read_text(encoding="utf-8")
    assert not (run.recipe_path.parent / values.name).exists()
    assert manager.store.path.stat().st_mode & 0o777 == 0o600

    cancelled = manager.cancel(run.run_uid)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True
    assert [event["status"] for event in manager.store.events(run.run_uid)] == [
        "queued",
        "cancelled",
    ]


def test_control_store_reopens_persisted_history(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source_lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    run = manager.submit(recipe=source_lock, executor="gpu", node="worker-8")

    reopened = ControlStore(tmp_path / "local-state")

    assert reopened.get(run.run_uid) == run
    assert reopened.list() == (run,)


def test_reconcile_exposes_live_supervisor_detail(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source_lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    submitted = manager.submit(recipe=source_lock, executor="gpu", node="worker-8")
    running = manager.store.claim_next()
    assert running is not None and running.run_uid == submitted.run_uid
    run_dir = tmp_path / "local-state" / "runs" / running.run_uid
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_uid": running.run_uid,
                "status": "running",
                "phase": "allocating",
                "detail": "Pod phase=Pending: Unschedulable",
                "infrastructure_state": "pod_pending",
                "artifact_state": "pending",
            }
        ),
        encoding="utf-8",
    )
    manager._child_exit_code = lambda run_uid, pid: None  # type: ignore[method-assign]

    manager.reconcile()

    observed = manager.store.get(running.run_uid)
    assert observed is not None
    assert observed.status == "running"
    assert observed.detail == "Pod phase=Pending: Unschedulable"
    assert observed.infrastructure_state == "pod_pending"


def test_control_database_schema_is_replaced_directly(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "state")
    with store._connect() as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.execute("ALTER TABLE control_runs ADD COLUMN obsolete TEXT")

    replaced = ControlStore(tmp_path / "state")

    with replaced._connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = [row[1] for row in connection.execute("PRAGMA table_info(control_runs)")]
    assert version == ControlStore.SCHEMA_VERSION
    assert tuple(columns) == ControlStore.SCHEMA_COLUMNS
    assert replaced.list() == ()


def test_reconcile_repairs_deleted_infrastructure_for_a_terminal_run(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source_lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    run = manager.submit(recipe=source_lock, executor="gpu", node="worker-8")
    failed = manager.store.update(
        run.run_uid,
        status="failed",
        phase="failed",
        infrastructure_state="pod_retained",
        artifact_state="partial",
        detail="owned Pod retained for recovery",
        exit_code=1,
        finished_at="2026-09-04T07:00:00+00:00",
    )
    run_dir = tmp_path / "local-state" / "runs" / run.run_uid
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_uid": run.run_uid,
                "status": "failed",
                "infrastructure_state": "pod_retained",
                "cleanup": "deleted",
            }
        ),
        encoding="utf-8",
    )

    manager.reconcile()

    reconciled = manager.store.get(run.run_uid)
    assert reconciled is not None
    assert reconciled.status == "failed"
    assert reconciled.phase == "failed"
    assert reconciled.exit_code == failed.exit_code == 1
    assert reconciled.infrastructure_state == "pod_deleted"
    assert reconciled.detail == "owned Pod deleted after terminal run"
    assert manager.store.events(run.run_uid)[-1]["infrastructure_state"] == "pod_deleted"


def _failed_run(tmp_path: Path):
    manager = TaskManager(_runtime(tmp_path))
    lock, _ = _lock(tmp_path)
    submitted = manager.submit(recipe=lock, executor="gpu", node="worker-8", name="failed run")
    run = manager.store.update(submitted.run_uid, status="failed", phase="failed")
    run_dir = manager.runtime.storage.runs_dir / run.run_uid
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_uid": run.run_uid, "status": "failed", "cleanup": "not_created"})
    )
    (run_dir / "result.txt").write_text("failed result")
    log = controller_log_path(manager.runtime.storage.root, run.run_uid)
    log.parent.mkdir(parents=True)
    log.write_text("failure log")
    return manager, run, run_dir


def test_delete_failed_removes_only_the_selected_run_and_persists(tmp_path: Path) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    other = manager.submit(recipe=tmp_path / "recipe.lock.yaml", executor="gpu", node="worker-8")
    memory = manager.runtime.storage.memory_path
    memory.write_text("shared memory")

    manager.delete_failed(run.run_uid)

    reopened = ControlStore(manager.runtime.storage.root)
    assert reopened.get(run.run_uid) is None
    assert reopened.events(run.run_uid) == ()
    assert not run_dir.exists()
    assert not run.recipe_path.parent.exists()
    assert not controller_log_path(manager.runtime.storage.root, run.run_uid).exists()
    assert reopened.get(other.run_uid) == other
    assert other.recipe_path.exists()
    assert (tmp_path / "recipe.lock.yaml").exists()
    assert memory.read_text() == "shared memory"
    manager.reconcile()
    assert manager.store.get(run.run_uid) is None


def test_delete_failed_cleans_pod_before_removing_ownership_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    manager.store.update(run.run_uid, infrastructure_state="pod_retained")
    calls = []

    def cleanup(supervisor, uid):
        assert uid == run.run_uid
        assert (run_dir / "run.json").is_file()
        assert supervisor.executor.namespace == "private-inference"
        assert manager.store.get(uid) is not None
        calls.append(uid)
        record_path = run_dir / "run.json"
        record = json.loads(record_path.read_text())
        record["cleanup"] = "deleted"
        record_path.write_text(json.dumps(record))

    monkeypatch.setattr(KubernetesTargetSupervisor, "cleanup", cleanup)

    manager.cleanup_pod(run.run_uid)

    assert (run_dir / "result.txt").exists()
    assert manager.store.get(run.run_uid).infrastructure_state == "pod_deleted"
    manager.delete_failed(run.run_uid)
    assert calls == [run.run_uid]
    assert not run_dir.exists()
    assert manager.store.get(run.run_uid) is None


def test_cleanup_failure_keeps_experiment_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    manager.store.update(run.run_uid, infrastructure_state="pod_retained")

    def fail_cleanup(supervisor, uid):
        raise RemoteExecutionError("immutable Pod identity changed")

    monkeypatch.setattr(KubernetesTargetSupervisor, "cleanup", fail_cleanup)
    with pytest.raises(RemoteExecutionError, match="identity changed"):
        manager.delete_failed(run.run_uid)
    assert manager.store.get(run.run_uid).infrastructure_state == "pod_retained"
    assert (run_dir / "run.json").exists()
    assert (run_dir / "result.txt").exists()
    assert run.recipe_path.exists()
    assert controller_log_path(manager.runtime.storage.root, run.run_uid).exists()


@pytest.mark.parametrize("status", ["queued", "running", "disconnected", "completed", "cancelled"])
def test_delete_experiment_rejects_non_failed_runs(tmp_path: Path, status: str) -> None:
    manager = TaskManager(_runtime(tmp_path))
    lock, _ = _lock(tmp_path)
    run = manager.submit(recipe=lock, executor="gpu", node="worker-8")
    if status in {"disconnected", "completed"}:
        manager.store.update(run.run_uid, status="running")
    manager.store.update(run.run_uid, status=status)
    with pytest.raises(ControlError, match="only failed"):
        manager.delete_failed(run.run_uid)
    if status in {"queued", "running", "disconnected"}:
        with pytest.raises(ControlError, match="stop the experiment"):
            manager.cleanup_pod(run.run_uid)
    assert run.recipe_path.exists()


def test_terminal_but_live_controller_cannot_be_deleted(tmp_path: Path) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    manager.store.update(run.run_uid, pid=os.getpid())
    with pytest.raises(ControlError, match="still exiting"):
        manager.delete_failed(run.run_uid)
    with pytest.raises(ControlError, match="still exiting"):
        manager.cleanup_pod(run.run_uid)
    assert run_dir.exists()


def test_delete_rolls_back_files_when_index_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, run, run_dir = _failed_run(tmp_path)

    def fail_delete(uid):
        raise ControlError("index unavailable")

    monkeypatch.setattr(manager.store, "delete_failed", fail_delete)
    with pytest.raises(ControlError, match="index unavailable"):
        manager.delete_failed(run.run_uid)
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "result.txt").read_text() == "failed result"
    assert run.recipe_path.is_file()
    assert controller_log_path(manager.runtime.storage.root, run.run_uid).is_file()
    assert not list(manager.runtime.storage.root.glob(".delete-*"))


def test_delete_rejects_redirected_run_directory(tmp_path: Path) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    outside = tmp_path / "keep-this-data"
    run_dir.rename(outside)
    run_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ControlError, match="redirected run path"):
        manager.delete_failed(run.run_uid)
    assert (outside / "result.txt").exists()
    assert manager.store.get(run.run_uid) is not None
