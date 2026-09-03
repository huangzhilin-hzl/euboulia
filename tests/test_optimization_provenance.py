import json
import platform
from pathlib import Path

import pytest

from euboulia.optimization.config import (
    RuntimeCaptureConfig,
    RuntimeComponentConfig,
    RuntimeExpectedConfig,
    RuntimeProvenanceConfig,
)
from euboulia.optimization.provenance import (
    RuntimeProvenanceError,
    RuntimeProvenanceRecord,
    capture_runtime_provenance,
    hardware_identity,
    validate_declared_hardware,
    validate_runtime_provenance,
    write_runtime_provenance,
)


def _runtime(version: str) -> RuntimeProvenanceConfig:
    return RuntimeProvenanceConfig(
        expected=RuntimeExpectedConfig(
            components={"python": RuntimeComponentConfig(version=version)}
        ),
        capture=RuntimeCaptureConfig(require_observed=True),
    )


def test_runtime_provenance_captures_validates_and_writes_evidence(tmp_path: Path) -> None:
    config = _runtime(platform.python_version())

    record = capture_runtime_provenance(config, repository=tmp_path)
    validate_runtime_provenance(config, record)
    destination = write_runtime_provenance(record, tmp_path / "runtime.json")

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["valid"] is True
    assert payload["observed"]["components"]["python"]["version"] == platform.python_version()
    assert hardware_identity(record, ("0", "1")) == hardware_identity(record, ("0", "1"))


def test_runtime_provenance_fails_on_observed_mismatch(tmp_path: Path) -> None:
    config = _runtime("0.0.0")
    record = capture_runtime_provenance(config, repository=tmp_path)

    assert record.valid is False
    with pytest.raises(RuntimeProvenanceError, match=r"components\.python\.version"):
        validate_runtime_provenance(config, record)


def test_declared_hardware_validation_is_scenario_independent() -> None:
    record = RuntimeProvenanceRecord(
        expected={},
        observed={
            "gpus": [
                {"index": "0", "name": "NVIDIA H20"},
                {"index": "1", "name": "NVIDIA H20"},
            ]
        },
        mismatches=(),
        unobserved=(),
        collected_at="2026-01-01T00:00:00+00:00",
    )

    validate_declared_hardware(
        {"accelerator": "NVIDIA-H20", "accelerator_count": 2, "node_count": 1},
        record,
    )
    with pytest.raises(RuntimeProvenanceError, match="accelerator_count"):
        validate_declared_hardware(
            {"accelerator": "NVIDIA-H20", "accelerator_count": 8},
            record,
        )
