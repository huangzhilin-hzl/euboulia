import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

import pytest
import yaml

import euboulia.remote as remote
from euboulia.execution import ExecutionResult
from euboulia.optimization.config import load_optimization_config
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


def test_worker_recipe_is_resolved_and_contains_no_host_storage_or_values_path(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    values = tmp_path / "private" / "values.yaml"
    values.parent.mkdir()
    values.write_text(
        yaml.safe_dump(
            {
                "container_image": "registry.example/dsv4@sha256:" + "a" * 64,
                "deepgemm_revision": "c" * 40,
                "deepgemm_repository": "https://example.invalid/deepgemm.git",
                "deepgemm_ref": "refs/heads/deepgemm-test",
                "lm_eval_version": "0.4.9.2",
                "model_revision": "d" * 40,
                "sglang_revision": "b" * 40,
                "sglang_repository": "https://example.invalid/sglang.git",
                "sglang_ref": "refs/heads/sglang-test",
            }
        ),
        encoding="utf-8",
    )
    config = load_optimization_config(
        repository / "examples/scenarios/dsv4-megamoe.yaml",
        values,
    )
    supervisor = remote.KubernetesTargetSupervisor(
        _executor(repository),
        remote.LocalStorageConfig(root=tmp_path / "results"),
    )

    document = yaml.safe_load(supervisor._worker_recipe(config))

    assert "inputs" not in document
    assert "execution" not in document
    assert "root_dir" not in document["optimization"]["workspace"]
    assert document["optimization"]["planner"]["patch_catalog"] == (
        "/workspace/euboulia/examples/scenarios/dsv4-megamoe-catalog.yaml"
    )
    assert document["target"]["runtime"]["expected"]["container"]["image"] == (
        "registry.example/dsv4@sha256:" + "a" * 64
    )
    assert document["sources"]["sglang"]["ref"] == "refs/heads/sglang-test"
    assert str(values) not in supervisor._worker_recipe(config)


def test_worker_recipe_maps_relative_project_paths_to_the_pod_checkout(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    document = yaml.safe_load(
        (repository / "examples/scenarios/dsv4-megamoe.yaml").read_text(encoding="utf-8")
    )
    document["optimization"]["planner"]["patch_catalog"] = "catalog.yaml"
    document["optimization"]["workspace"].pop("source")
    document["optimization"]["workspace"]["repository"] = "SGLang"
    document["target"]["runtime"]["expected"]["components"]["sglang"].pop("source")
    document["target"]["runtime"]["expected"]["components"]["sglang"]["path"] = "SGLang"
    recipe = tmp_path / "scenario.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    values = tmp_path / "values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "container_image": "registry.example/dsv4@sha256:" + "a" * 64,
                "deepgemm_revision": "c" * 40,
                "deepgemm_repository": "https://example.invalid/deepgemm.git",
                "deepgemm_ref": "refs/heads/deepgemm-test",
                "lm_eval_version": "0.4.9.2",
                "model_revision": "d" * 40,
                "sglang_revision": "b" * 40,
                "sglang_repository": "https://example.invalid/sglang.git",
                "sglang_ref": "refs/heads/sglang-test",
            }
        ),
        encoding="utf-8",
    )
    config = load_optimization_config(recipe, values)
    supervisor = remote.KubernetesTargetSupervisor(
        _executor(tmp_path),
        remote.LocalStorageConfig(root=tmp_path / "results"),
    )

    worker = yaml.safe_load(supervisor._worker_recipe(config))

    assert worker["optimization"]["planner"]["patch_catalog"] == (
        "/workspace/euboulia/catalog.yaml"
    )
    assert worker["optimization"]["workspace"]["repository"] == ("/workspace/euboulia/SGLang")
    assert (
        worker["target"]["runtime"]["expected"]["components"]["sglang"]["path"]
        == "/workspace/euboulia/SGLang"
    )


def test_stage_recipe_streams_only_the_lock_and_verifies_its_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = remote.KubernetesTargetSupervisor(
        _executor(tmp_path),
        remote.LocalStorageConfig(root=tmp_path / "results"),
    )
    run_uid = "run-01HF7YAT000000000000000000"
    remote_run_dir = supervisor.executor.scratch_dir / "runs" / run_uid
    remote_recipe = remote_run_dir / "inputs" / "recipe.lock.yaml"
    contents = b"schema_version: 3\nname: private-test\n"
    expected_sha256 = hashlib.sha256(contents).hexdigest()
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        observed["argv"] = argv
        observed.update(kwargs)
        return remote.subprocess.CompletedProcess(
            argv,
            0,
            stdout=(expected_sha256 + "\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    local_run_dir = tmp_path / "local-run"
    local_run_dir.mkdir(mode=0o700)

    supervisor._stage_recipe(
        remote_run_dir,
        remote_recipe,
        contents,
        expected_sha256,
        local_run_dir,
    )

    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[:6] == [
        "kubectl",
        "--namespace",
        "inference",
        "exec",
        "--stdin",
        "dsv4-h20",
    ]
    assert str(remote_recipe) == argv[-1]
    assert observed["input"] == contents
    assert observed["shell"] is False
    assert "values" not in " ".join(argv)
    assert (local_run_dir / "control" / "recipe-stage.stdout.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("worker_passed", "returncode"), [(True, 0), (False, 1)])
def test_remote_supervisor_keeps_completed_results_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_passed: bool,
    returncode: int,
) -> None:
    run_uid = "run-01HF7YAT000000000000000000"
    storage = remote.LocalStorageConfig(root=tmp_path / "results")
    supervisor = remote.KubernetesTargetSupervisor(_executor(tmp_path), storage)
    monkeypatch.setattr(remote, "new_run_uid", lambda: run_uid)
    monkeypatch.setattr(
        supervisor,
        "_worker_recipe",
        lambda config: "schema_version: 3\nname: private-test\n",
    )

    def fake_stage(
        remote_run_dir: PurePosixPath,
        remote_recipe: PurePosixPath,
        contents: bytes,
        expected_sha256: str,
        local_run_dir: Path,
    ) -> None:
        assert remote_recipe == remote_run_dir / "inputs/recipe.lock.yaml"
        assert contents == b"schema_version: 3\nname: private-test\n"
        assert expected_sha256 == hashlib.sha256(contents).hexdigest()
        assert local_run_dir == storage.runs_dir / run_uid

    monkeypatch.setattr(supervisor, "_stage_recipe", fake_stage)

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

    result = supervisor.run(object(), name="baseline")  # type: ignore[arg-type]

    assert result.status == "completed"
    assert result.passed is worker_passed
    assert result.local_run_dir == storage.runs_dir / run_uid
    assert result.summary_path.is_file()
    assert result.artifact_manifest_path.is_file()
    assert result.memory_path.is_file()
    local_recipe = result.local_run_dir / "inputs/recipe.lock.yaml"
    assert local_recipe.read_text(encoding="utf-8") == ("schema_version: 3\nname: private-test\n")
    assert local_recipe.stat().st_mode & 0o777 == 0o600
    run_record = json.loads((result.local_run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_record["local_recipe"] == str(local_recipe)
    assert run_record["remote_recipe"] == str(
        supervisor.executor.scratch_dir / "runs" / run_uid / "inputs/recipe.lock.yaml"
    )
    assert run_record["recipe_sha256"] == hashlib.sha256(local_recipe.read_bytes()).hexdigest()
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
            remote_run_dir=PurePosixPath("/home/admin/.cache/euboulia/runs/" + run_uid),
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
