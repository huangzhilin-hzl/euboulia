import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

import pytest

import euboulia.remote as remote
from euboulia.execution import ExecutionResult
from euboulia.optimization.events import EventLedger, EventType


def _executor(tmp_path: Path) -> remote.KubernetesExecutorConfig:
    return remote.KubernetesExecutorConfig(
        name="h20-pod",
        namespace="inference",
        pod="dsv4-h20",
        container="runtime",
        project_dir=PurePosixPath("/workspace/euboulia"),
        scratch_dir=PurePosixPath("/home/admin/.cache/euboulia"),
        local_project_dir=tmp_path,
    )


def test_runtime_config_separates_local_storage_from_pod_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        """
storage:
  root: ./local-results
  sync:
    raw_profiles: on_demand
executors:
  h20-pod:
    type: kubernetes
    namespace: inference
    pod: dsv4-h20
    container: runtime
    project_dir: /workspace/euboulia
    scratch_dir: /home/admin/.cache/euboulia
    local_project_dir: .
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = remote.load_host_runtime_config(config_path)
    executor = config.executor("h20-pod")

    assert config.storage.root == (tmp_path / "local-results").resolve()
    assert config.storage.memory_path == (tmp_path / "local-results/memory.sqlite3").resolve()
    assert config.storage.sync.raw_profiles == "on_demand"
    assert executor.project_dir == PurePosixPath("/workspace/euboulia")
    assert executor.scratch_dir == PurePosixPath("/home/admin/.cache/euboulia")


@pytest.mark.parametrize(("worker_passed", "returncode"), [(True, 0), (False, 1)])
def test_remote_supervisor_keeps_completed_results_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_passed: bool,
    returncode: int,
) -> None:
    run_uid = "run-01HF7YAT000000000000000000"
    recipe = tmp_path / "scenario.yaml"
    recipe.write_text("name: test\n", encoding="utf-8")
    storage = remote.LocalStorageConfig(root=tmp_path / "results")
    supervisor = remote.KubernetesTargetSupervisor(_executor(tmp_path), storage)
    monkeypatch.setattr(remote, "new_run_uid", lambda: run_uid)

    def fake_worker(*args: object, **kwargs: object) -> ExecutionResult:
        local_run_dir = kwargs["local_run_dir"]
        assert isinstance(local_run_dir, Path)
        control = local_run_dir / "control"
        control.mkdir(parents=True)
        stdout = control / "worker.stdout.log"
        stderr = control / "worker.stderr.log"
        stdout.write_text(
            json.dumps({"run_uid": run_uid, "passed": worker_passed}) + "\n",
            encoding="utf-8",
        )
        stderr.write_text("", encoding="utf-8")
        return ExecutionResult(
            command_id="worker",
            argv=("kubectl", "exec"),
            cwd=None,
            returncode=returncode,
            started_at="2026-09-04T00:00:00+00:00",
            finished_at="2026-09-04T00:01:00+00:00",
            duration_seconds=60.0,
            stdout_path=stdout,
            stderr_path=stderr,
            environment_keys=(),
        )

    def fake_pull(
        remote_run_dir: PurePosixPath,
        destination: Path,
        *,
        include_raw_profiles: bool | None = None,
    ) -> remote.SyncSummary:
        assert include_raw_profiles is None
        destination.mkdir(parents=True)
        validation = destination / "target-validation/validation.json"
        validation.parent.mkdir(parents=True)
        validation.write_text('{"passed": true}\n', encoding="utf-8")
        validation_sha256 = hashlib.sha256(validation.read_bytes()).hexdigest()
        (destination / "artifact-index.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_uid": run_uid,
                    "artifacts": [
                        {
                            "path": "target-validation/validation.json",
                            "sha256": validation_sha256,
                            "size_bytes": validation.stat().st_size,
                            "retention": "local",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return remote.SyncSummary(destination, remote_run_dir, added=2)

    monkeypatch.setattr(supervisor, "_run_worker", fake_worker)
    monkeypatch.setattr(supervisor, "_pull_artifacts", fake_pull)

    result = supervisor.run(recipe, values=None, name="baseline")

    assert result.status == "completed"
    assert result.passed is worker_passed
    assert result.local_run_dir == storage.runs_dir / run_uid
    assert result.summary_path.is_file()
    assert result.artifact_manifest_path.is_file()
    assert result.memory_path.is_file()
    events = EventLedger(result.events_path, create_parents=False).read_all()
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
    ]
    manifest = json.loads(result.artifact_manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["synced"] is True
    assert manifest["artifacts"][0]["remote_uri"].startswith(
        "kubernetes://inference/dsv4-h20/runtime/"
    )


def test_worker_artifact_index_marks_raw_profiles_remote_only(tmp_path: Path) -> None:
    run_uid = "run-01HF7YAT000000000000000000"
    summary = tmp_path / "target-validation/profile/summary.json"
    raw = tmp_path / "target-validation/profile/raw/rank-0.json"
    summary.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    summary.write_text("{}\n", encoding="utf-8")
    raw.write_text("{}\n", encoding="utf-8")

    index_path = remote.write_worker_artifact_index(tmp_path, run_uid)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    retention = {item["path"]: item["retention"] for item in payload["artifacts"]}

    assert retention["target-validation/profile/summary.json"] == "local"
    assert retention["target-validation/profile/raw/rank-0.json"] == "remote_only"


def test_artifact_manifest_rejects_a_corrupted_snapshot(tmp_path: Path) -> None:
    run_uid = "run-01HF7YAT000000000000000000"
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = snapshot / "result.json"
    result.write_text('{"passed": true}\n', encoding="utf-8")
    (snapshot / "artifact-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_uid": run_uid,
                "artifacts": [
                    {
                        "path": "result.json",
                        "sha256": "0" * 64,
                        "size_bytes": result.stat().st_size,
                        "retention": "local",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(remote.RemoteExecutionError, match="digest mismatch"):
        remote._write_local_artifact_manifest(
            snapshot,
            tmp_path / "manifest.json",
            executor=_executor(tmp_path),
            remote_run_dir=PurePosixPath(
                "/home/admin/.cache/euboulia/runs/" + run_uid
            ),
            run_uid=run_uid,
        )


def test_tar_extraction_rejects_paths_outside_snapshot(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        body = b"escape"
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    buffer.seek(0)

    with pytest.raises(remote.RemoteExecutionError, match="unsafe artifact path"):
        remote._extract_safe_tar(buffer, tmp_path / "snapshot")

    assert not (tmp_path / "outside.txt").exists()
