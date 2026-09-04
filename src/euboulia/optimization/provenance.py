"""Capture and validate the runtime identity used by managed optimization trials."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from euboulia.models import JSONValue
from euboulia.optimization.config import RuntimeComponentConfig, RuntimeProvenanceConfig
from euboulia.optimization.contracts import utc_now

_DISTRIBUTIONS = {
    "sglang": ("sglang",),
    "torch": ("torch",),
    "triton": ("triton",),
    "flashinfer": ("flashinfer-python", "flashinfer"),
    "deepgemm": ("deep_gemm", "deepgemm"),
    "deepep": ("deep_ep", "deepep"),
    "sgl-kernel": ("sgl-kernel", "sgl_kernel"),
    "lm-eval": ("lm_eval", "lm-eval"),
}
_CUDA_VERSION = re.compile(r"CUDA Version:\s*([0-9.]+)")


class RuntimeProvenanceError(RuntimeError):
    """Raised when observed runtime identity violates a declared expectation."""


@dataclass(frozen=True, slots=True)
class RuntimeProvenanceRecord:
    expected: dict[str, JSONValue]
    observed: dict[str, JSONValue]
    mismatches: tuple[str, ...]
    unobserved: tuple[str, ...]
    collected_at: str

    @property
    def valid(self) -> bool:
        return not self.mismatches

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": 1,
            "expected": self.expected,
            "observed": self.observed,
            "mismatches": list(self.mismatches),
            "unobserved": list(self.unobserved),
            "valid": self.valid,
            "collected_at": self.collected_at,
        }


def capture_runtime_provenance(
    config: RuntimeProvenanceConfig,
    *,
    repository: Path,
    source_paths: Mapping[str, Path] | None = None,
) -> RuntimeProvenanceRecord:
    """Collect only declared component identities and compare exact known fields."""

    expected = _expected_payload(config, source_paths=source_paths)
    if not config.capture.collect_observed:
        return RuntimeProvenanceRecord(
            expected=expected,
            observed={"collection": "disabled"},
            mismatches=(),
            unobserved=tuple(f"components.{name}" for name in config.expected.components),
            collected_at=utc_now(),
        )
    observed: dict[str, JSONValue] = {
        "host": {
            "node": platform.node(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "gpus": _gpu_inventory(),
        "components": {},
    }
    mismatches: list[str] = []
    unobserved: list[str] = []
    observed_components = observed["components"]
    assert isinstance(observed_components, dict)

    for name, component in config.expected.components.items():
        component_observed = _observe_component(
            name,
            component,
            repository,
            source_paths=source_paths,
        )
        observed_components[name] = component_observed
        for field_name in ("version", "revision", "digest", "dirty"):
            expected_value = getattr(component, field_name)
            if expected_value is None:
                continue
            observed_value = component_observed.get(field_name)
            qualified_name = f"components.{name}.{field_name}"
            if observed_value is None:
                unobserved.append(qualified_name)
                if config.capture.require_observed:
                    mismatches.append(
                        f"{qualified_name}: expected {expected_value!r}, not observed"
                    )
            elif observed_value != expected_value:
                mismatches.append(
                    f"{qualified_name}: expected {expected_value!r}, observed {observed_value!r}"
                )

    if config.expected.container is not None:
        observed["container"] = {"observation": "declared_only"}
        if config.capture.require_observed:
            unobserved.append("container.digest")
            mismatches.append("container.digest: cannot be observed from the managed process")

    return RuntimeProvenanceRecord(
        expected=expected,
        observed=observed,
        mismatches=tuple(mismatches),
        unobserved=tuple(unobserved),
        collected_at=utc_now(),
    )


def validate_runtime_provenance(
    config: RuntimeProvenanceConfig,
    record: RuntimeProvenanceRecord,
) -> None:
    if config.capture.fail_on_mismatch and record.mismatches:
        raise RuntimeProvenanceError("runtime provenance mismatch: " + "; ".join(record.mismatches))


def validate_declared_hardware(
    declared: Mapping[str, JSONValue],
    record: RuntimeProvenanceRecord,
) -> None:
    """Validate portable hardware declarations against the captured host inventory."""

    mismatches: list[str] = []
    node_count = declared.get("node_count")
    if node_count is not None and node_count != 1:
        mismatches.append(
            f"hardware.node_count: local managed execution provides 1, expected {node_count!r}"
        )

    raw_gpus = record.observed.get("gpus")
    gpus = raw_gpus if isinstance(raw_gpus, list) else []
    accelerator_count = declared.get("accelerator_count")
    if accelerator_count is not None and accelerator_count != len(gpus):
        mismatches.append(
            f"hardware.accelerator_count: expected {accelerator_count!r}, observed {len(gpus)}"
        )

    accelerator = declared.get("accelerator")
    if accelerator is not None:
        expected_name = _canonical_hardware_name(accelerator)
        observed_names = [item.get("name") for item in gpus if isinstance(item, dict)]
        invalid_names = [
            name for name in observed_names if _canonical_hardware_name(name) != expected_name
        ]
        if not observed_names or invalid_names:
            mismatches.append(
                f"hardware.accelerator: expected {accelerator!r}, observed {observed_names!r}"
            )

    if mismatches:
        raise RuntimeProvenanceError("declared hardware mismatch: " + "; ".join(mismatches))


def _canonical_hardware_name(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def write_runtime_provenance(record: RuntimeProvenanceRecord, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def hardware_identity(record: RuntimeProvenanceRecord | None, gpu_ids: tuple[str, ...]) -> str:
    payload: dict[str, object] = {
        "node": platform.node(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "gpu_ids": list(gpu_ids),
    }
    if record is not None:
        payload["gpus"] = record.observed.get("gpus", [])
        payload["runtime"] = record.expected
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"host-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _expected_payload(
    config: RuntimeProvenanceConfig,
    *,
    source_paths: Mapping[str, Path] | None = None,
) -> dict[str, JSONValue]:
    container: JSONValue = None
    if config.expected.container is not None:
        container = {
            "image": config.expected.container.image,
            "digest": config.expected.container.digest,
        }
    components: dict[str, JSONValue] = {}
    for name, component in config.expected.components.items():
        resolved_path = component.path
        if component.source is not None and source_paths is not None:
            resolved_path = source_paths.get(component.source)
        components[name] = {
            "version": component.version,
            "revision": component.revision,
            "digest": component.digest,
            "path": None if resolved_path is None else str(resolved_path),
            "source": component.source,
            "dirty": component.dirty,
            "metadata": dict(component.metadata),
        }
    return {"container": container, "components": components}


def _observe_component(
    name: str,
    component: RuntimeComponentConfig,
    repository: Path,
    *,
    source_paths: Mapping[str, Path] | None = None,
) -> dict[str, JSONValue]:
    observed: dict[str, JSONValue] = {}
    normalized = name.casefold()
    if normalized == "python":
        observed["version"] = platform.python_version()
    else:
        version = _distribution_version(normalized)
        if version is not None:
            observed["version"] = version
    path = component.path
    if component.source is not None and source_paths is not None:
        path = source_paths.get(component.source)
    if path is None and normalized == "sglang":
        path = repository
    if path is not None:
        observed["path"] = str(path)
        if path.is_file():
            observed["digest"] = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        elif path.is_dir():
            revision = _git(path, "rev-parse", "HEAD")
            dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
            if revision is not None:
                observed["revision"] = revision
            if dirty is not None:
                observed["dirty"] = bool(dirty)
    if normalized == "cuda":
        cuda_version = _cuda_version()
        if cuda_version is not None:
            observed["version"] = cuda_version
    return observed


def _distribution_version(name: str) -> str | None:
    candidates = _DISTRIBUTIONS.get(name, (name,))
    for distribution in candidates:
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _git(path: Path, *arguments: str) -> str | None:
    if shutil.which("git") is None:
        return None
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _gpu_inventory() -> list[JSONValue]:
    if shutil.which("nvidia-smi") is None:
        return []
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
    )
    if completed.returncode != 0:
        return []
    result: list[JSONValue] = []
    for line in completed.stdout.splitlines():
        values = [value.strip() for value in line.split(",", maxsplit=4)]
        if len(values) == 5:
            result.append(
                {
                    "index": values[0],
                    "uuid": values[1],
                    "name": values[2],
                    "driver_version": values[3],
                    "memory_total_mib": values[4],
                }
            )
    return result


def _cuda_version() -> str | None:
    if shutil.which("nvidia-smi") is None:
        return None
    completed = subprocess.run(
        ["nvidia-smi"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
    )
    if completed.returncode != 0:
        return None
    match = _CUDA_VERSION.search(completed.stdout)
    return None if match is None else match.group(1)


__all__ = [
    "RuntimeProvenanceError",
    "RuntimeProvenanceRecord",
    "capture_runtime_provenance",
    "hardware_identity",
    "validate_declared_hardware",
    "validate_runtime_provenance",
    "write_runtime_provenance",
]
