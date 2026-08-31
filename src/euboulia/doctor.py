"""Read-only environment diagnostics."""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    available: bool
    detail: str
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _command_check(name: str, *, required: bool = False) -> DoctorCheck:
    path = shutil.which(name)
    return DoctorCheck(
        name=name,
        available=path is not None,
        detail=path or "not found",
        required=required,
    )


def _module_check(name: str) -> DoctorCheck:
    try:
        available = importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False
    return DoctorCheck(
        name=f"python:{name}",
        available=available,
        detail="importable" if available else "not installed",
    )


def run_doctor() -> tuple[DoctorCheck, ...]:
    """Inspect local tooling without importing GPU frameworks or changing state."""

    python_ok = sys.version_info >= (3, 11)
    checks = [
        DoctorCheck(
            name="python",
            available=python_ok,
            detail=platform.python_version(),
            required=True,
        ),
        DoctorCheck(
            name="platform",
            available=True,
            detail=f"{platform.system()} {platform.machine()}",
        ),
        _command_check("git", required=True),
        _command_check("vllm"),
        _module_check("sglang"),
        _command_check("nvidia-smi"),
        _command_check("nsys"),
        _command_check("ncu"),
    ]
    return tuple(checks)


def required_checks_pass(checks: tuple[DoctorCheck, ...]) -> bool:
    return all(check.available for check in checks if check.required)


__all__ = ["DoctorCheck", "required_checks_pass", "run_doctor"]
