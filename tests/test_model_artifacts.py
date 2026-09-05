from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from euboulia.optimization import model_artifacts
from euboulia.optimization.config import ModelArtifactConfig, ModelDownloadConfig
from euboulia.optimization.model_artifacts import (
    ModelPreparationError,
    ensure_local_model,
    prepare_model,
)

REVISION = "a" * 40


def _write_model(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"model_type": "test"}')
    (path / "tokenizer.json").write_text('{"version": "1.0"}')
    (path / "model.safetensors").write_bytes(b"test weights")


def _model(path: Path, provider: str = "modelscope", timeout: float = 10) -> ModelArtifactConfig:
    return ModelArtifactConfig(
        name="target",
        path=str(path),
        served_name="test",
        revision=REVISION,
        model_id="example/model",
        download=ModelDownloadConfig(provider, timeout),
    )


def test_complete_local_model_needs_no_download_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_model(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("complete local models must not access the network or import a downloader")

    monkeypatch.setattr(model_artifacts, "_download_snapshot", forbidden)
    assert ensure_local_model("example/model", "modelscope", REVISION, tmp_path) == "reused"
    assert not (tmp_path / ".euboulia-model.json").exists()


@pytest.mark.parametrize("provider", ["modelscope", "hf_mirror"])
def test_pinned_download_routes_to_selected_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
):
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        _write_model(Path(kwargs["local_dir"]))

    sdk = SimpleNamespace(snapshot_download=download)
    imports = []

    def import_sdk(name):
        imports.append(name)
        return sdk

    monkeypatch.setattr(model_artifacts.importlib, "import_module", import_sdk)
    path = tmp_path / "model"
    assert ensure_local_model("example/model", provider, REVISION, path) == "downloaded"
    assert ensure_local_model("example/model", provider, REVISION, path) == "reused"
    expected = {"revision": REVISION, "local_dir": str(path)}
    if provider == "modelscope":
        expected["model_id"] = "example/model"
        assert imports == ["modelscope"]
    else:
        expected.update(repo_id="example/model", endpoint="https://hf-mirror.com")
        assert imports == ["huggingface_hub"]
    assert calls == [expected]


def test_failed_download_is_resumed_even_if_some_files_look_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    def download(model_id, provider, revision, path):
        calls.append(path)
        _write_model(path)
        if len(calls) == 1:
            raise ConnectionError("transfer interrupted")

    monkeypatch.setattr(model_artifacts, "_download_snapshot", download)
    path = tmp_path / "model"
    with pytest.raises(ConnectionError):
        ensure_local_model("example/model", "modelscope", REVISION, path)
    assert json.loads((path / ".euboulia-model.json").read_text())["status"] == "downloading"
    assert ensure_local_model("example/model", "modelscope", REVISION, path) == "downloaded"
    assert len(calls) == 2


def test_download_with_missing_shard_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def download(model_id, provider, revision, path):
        _write_model(path)
        (path / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"layer": "missing-00002.safetensors"}})
        )

    monkeypatch.setattr(model_artifacts, "_download_snapshot", download)
    path = tmp_path / "model"
    with pytest.raises(ModelPreparationError, match="missing or invalid weight shard"):
        ensure_local_model("example/model", "modelscope", REVISION, path)
    assert json.loads((path / ".euboulia-model.json").read_text())["status"] == "downloading"


def test_cache_identity_mismatch_does_not_overwrite_weights(tmp_path: Path):
    _write_model(tmp_path)
    (tmp_path / ".euboulia-model.json").write_text(
        json.dumps(
            {
                "model_id": "example/other",
                "provider": "modelscope",
                "revision": REVISION,
                "status": "complete",
            }
        )
    )
    with pytest.raises(ModelPreparationError, match="another model"):
        ensure_local_model("example/model", "modelscope", REVISION, tmp_path)
    assert (tmp_path / "model.safetensors").read_bytes() == b"test weights"


def test_incomplete_untracked_directory_is_not_overwritten(tmp_path: Path):
    (tmp_path / "config.json").write_text('{"model_type": "test"}')
    with pytest.raises(ModelPreparationError, match="select an empty download directory"):
        ensure_local_model("example/model", "modelscope", REVISION, tmp_path)
    assert not (tmp_path / ".euboulia-model.json").exists()


def _stub_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    stub = tmp_path / "sdk"
    stub.mkdir()
    (stub / "huggingface_hub.py").write_text(source)
    src = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(stub), str(src))))


def test_bounded_download_child_uses_mirror_and_keeps_credentials_out_of_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_sdk(
        tmp_path,
        monkeypatch,
        """
import json, os
from pathlib import Path
assert os.environ['HF_ENDPOINT'] == 'https://hf-mirror.com'
assert os.environ['HF_TOKEN'] == 'test-credential'
def snapshot_download(**kwargs):
    assert kwargs['endpoint'] == 'https://hf-mirror.com'
    path = Path(kwargs['local_dir'])
    (path / 'config.json').write_text('{"model_type":"test"}')
    (path / 'tokenizer.json').write_text('{}')
    (path / 'model.safetensors').write_bytes(b'weights')
""",
    )
    monkeypatch.setenv("HF_TOKEN", "test-credential")
    evidence = tmp_path / "evidence"
    prepare_model(_model(tmp_path / "model", "hf_mirror"), evidence)
    manifest = json.loads((evidence / "model-target.json").read_text())
    assert manifest["succeeded"] is True
    assert "HF_TOKEN" in manifest["environment_keys"]
    assert "test-credential" not in (evidence / "model-target.json").read_text()


def test_download_child_timeout_blocks_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_sdk(
        tmp_path, monkeypatch, "import time\ndef snapshot_download(**kwargs): time.sleep(10)\n"
    )
    evidence = tmp_path / "evidence"
    with pytest.raises(ModelPreparationError, match="timed out"):
        prepare_model(_model(tmp_path / "model", "hf_mirror", timeout=0.2), evidence)
    assert json.loads((evidence / "model-target.json").read_text())["timed_out"] is True


def test_download_sdk_error_is_retained_in_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_sdk(
        tmp_path, monkeypatch, 'def snapshot_download(**kwargs): raise RuntimeError("404 model")'
    )
    evidence = tmp_path / "evidence"
    with pytest.raises(ModelPreparationError, match="exited with status 1"):
        prepare_model(_model(tmp_path / "model", "hf_mirror"), evidence)
    manifest = json.loads((evidence / "model-target.json").read_text())
    assert "404 model" in Path(manifest["stderr_path"]).read_text()


def test_concurrent_preparations_download_shared_model_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    def download(model_id, provider, revision, path):
        calls.append(path)
        time.sleep(0.1)
        _write_model(path)

    monkeypatch.setattr(model_artifacts, "_download_snapshot", download)
    path = tmp_path / "model"
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [
            executor.submit(ensure_local_model, "example/model", "modelscope", REVISION, path)
            for _ in range(2)
        ]
        assert sorted(task.result() for task in tasks) == ["downloaded", "reused"]
    assert calls == [path]
