"""Prepare pinned local models before building or starting an inference service."""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import sys
from pathlib import Path

from euboulia.execution import DEFAULT_ENV_ALLOWLIST, CommandExecutor
from euboulia.optimization.config import ModelArtifactConfig

HF_MIRROR_ENDPOINT = "https://hf-mirror.com"
_MARKER = ".euboulia-model.json"
_DOWNLOAD_ENV = DEFAULT_ENV_ALLOWLIST | {
    "HF_HOME",
    "HF_TOKEN",
    "HF_TOKEN_PATH",
    "HF_HUB_DISABLE_XET",
    "MODELSCOPE_CACHE",
    "MODELSCOPE_API_TOKEN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
}


class ModelPreparationError(ValueError):
    """A declared model could not be prepared; service startup must stop."""


def prepare_model(model: ModelArtifactConfig, evidence_dir: Path) -> None:
    """Run model preparation in a bounded child with per-run download logs."""

    download = model.download
    if download is None:
        return
    if model.model_id is None:
        raise ModelPreparationError(f"models.{model.name}.model_id is required for downloads")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result = CommandExecutor(evidence_dir).run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--model-id",
            model.model_id,
            "--provider",
            download.provider,
            "--revision",
            model.revision,
            "--path",
            model.path,
        ],
        env_allowlist=_DOWNLOAD_ENV,
        # Set this before importing the HF SDK, including its transfer backends.
        env_overrides=(
            {"HF_ENDPOINT": HF_MIRROR_ENDPOINT} if download.provider == "hf_mirror" else None
        ),
        timeout_seconds=download.timeout_seconds,
        artifact_prefix=f"model-{model.name}",
    )
    manifest = evidence_dir / f"model-{model.name}.json"
    manifest.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if not result.succeeded:
        reason = "timed out" if result.timed_out else f"exited with status {result.returncode}"
        raise ModelPreparationError(
            f"model {model.model_id!r} preparation {reason}; inspect {result.stderr_path}"
        )


def _nonempty_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return bool(handle.read(1))
    except OSError:
        return False


def _local_model_issue(path: Path) -> str | None:
    """Check config, tokenizer and weight shards without loading tensor payloads."""

    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing or unreadable config.json"
    if not isinstance(config, dict) or not config:
        return "invalid config.json"
    tokenizer_files = ("tokenizer.json", "tokenizer.model", "spiece.model", "vocab.txt")
    if not any(_nonempty_file(path / name) for name in tokenizer_files) and not (
        _nonempty_file(path / "vocab.json") and _nonempty_file(path / "merges.txt")
    ):
        return "missing tokenizer files"
    indexes = [
        index
        for name in ("model.safetensors.index.json", "pytorch_model.bin.index.json")
        if (index := path / name).exists()
    ]
    if indexes:
        for index in indexes:
            try:
                data = json.loads(index.read_text(encoding="utf-8"))
                shards = data.get("weight_map") if isinstance(data, dict) else None
            except (OSError, ValueError):
                return f"invalid weight index {index.name}"
            if not isinstance(shards, dict) or not shards:
                return f"invalid weight index {index.name}"
            if not all(isinstance(filename, str) for filename in shards.values()):
                return f"invalid weight filenames in {index.name}"
            for filename in set(shards.values()):
                if (
                    not isinstance(filename, str)
                    or Path(filename).is_absolute()
                    or ".." in Path(filename).parts
                    or not _nonempty_file(path / filename)
                ):
                    return f"missing or invalid weight shard in {index.name}: {filename!r}"
        return None
    if any(_nonempty_file(path / name) for name in ("model.safetensors", "pytorch_model.bin")):
        return None
    return "missing model weights or weight index"


def _write_marker(path: Path, identity: dict[str, str], status: str) -> None:
    temporary = path / f"{_MARKER}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({**identity, "status": status}) + "\n", encoding="utf-8")
    temporary.replace(path / _MARKER)


def _reusable(path: Path, identity: dict[str, str]) -> bool:
    marker_path = path / _MARKER
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelPreparationError(
                f"cannot read model download identity at {marker_path}"
            ) from exc
        if not isinstance(marker, dict) or any(marker.get(k) != v for k, v in identity.items()):
            raise ModelPreparationError(
                f"{path} belongs to another model ID, provider or revision; use a different path"
            )
        if marker.get("status") != "complete":
            return False
    return _local_model_issue(path) is None


def _download_snapshot(model_id: str, provider: str, revision: str, path: Path) -> None:
    module_name = "modelscope" if provider == "modelscope" else "huggingface_hub"
    try:
        sdk = importlib.import_module(module_name)
    except ImportError as exc:
        raise ModelPreparationError(
            f"install {module_name} in the worker image to download from {provider}"
        ) from exc
    if provider == "modelscope":
        sdk.snapshot_download(model_id=model_id, revision=revision, local_dir=str(path))
    else:
        sdk.snapshot_download(
            repo_id=model_id, revision=revision, local_dir=str(path), endpoint=HF_MIRROR_ENDPOINT
        )


def ensure_local_model(model_id: str, provider: str, revision: str, path: Path) -> str:
    """Reuse complete local data or resume a pinned download under a filesystem lock."""

    identity = {"model_id": model_id, "provider": provider, "revision": revision}
    if _reusable(path, identity):
        return "reused"
    if path.is_symlink():
        raise ModelPreparationError(f"incomplete model at symlink {path}; fix its target first")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not unlink the lock: queued processes must keep locking the same inode.
    with (path.parent / f".{path.name}.euboulia.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _reusable(path, identity):
            return "reused"
        if path.is_dir() and any(path.iterdir()) and not (path / _MARKER).exists():
            raise ModelPreparationError(
                f"existing model directory {path} is incomplete: {_local_model_issue(path)}; "
                "repair it or select an empty download directory"
            )
        path.mkdir(exist_ok=True)
        _write_marker(path, identity, "downloading")
        print(f"Downloading {model_id}@{revision} from {provider} to {path}", flush=True)
        _download_snapshot(model_id, provider, revision, path)
        issue = _local_model_issue(path)
        if issue is not None:
            raise ModelPreparationError(f"downloaded model {model_id!r} is incomplete: {issue}")
        _write_marker(path, identity, "complete")
        return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--provider", required=True, choices=("modelscope", "hf_mirror"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args()
    try:
        status = ensure_local_model(args.model_id, args.provider, args.revision, args.path)
    except Exception as exc:
        print(f"Model preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": status, "model_id": args.model_id, "path": str(args.path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
