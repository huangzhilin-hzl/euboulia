import json
from pathlib import Path

import yaml
from test_optimization_config import write_input_template

from euboulia.control import ControlStore, TaskManager
from euboulia.optimization.config import (
    dump_resolved_optimization_config,
    load_optimization_config,
    resolve_optimization_config,
)


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
