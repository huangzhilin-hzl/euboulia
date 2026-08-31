"""Framework-neutral benchmark adapter contracts and JSON parsing helpers."""

from __future__ import annotations

import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar


class BenchmarkType(StrEnum):
    """Stable benchmark modes exposed by the supported framework CLIs."""

    SERVE = "serve"
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    SWEEP = "sweep"


@dataclass(frozen=True, slots=True)
class AdapterCommand:
    """A benchmark command description; constructing it has no side effects."""

    framework: str
    benchmark_type: BenchmarkType
    argv: tuple[str, ...]
    result_path: Path
    result_is_directory: bool = False
    manages_service_lifecycle: bool = False

    def as_argv(self) -> list[str]:
        """Return a mutable argv list suitable for ``CommandExecutor.run``."""

        return list(self.argv)

    def to_dict(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "benchmark_type": self.benchmark_type.value,
            "argv": list(self.argv),
            "result_path": str(self.result_path),
            "result_is_directory": self.result_is_directory,
            "manages_service_lifecycle": self.manages_service_lifecycle,
        }


@dataclass(frozen=True, slots=True)
class ParsedBenchmark:
    """Normalized numeric metrics plus the lossless framework payload."""

    framework: str
    benchmark_type: BenchmarkType
    metrics: dict[str, float]
    raw: object
    selected_record: dict[str, object]
    records: tuple[dict[str, object], ...]
    source_path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "benchmark_type": self.benchmark_type.value,
            "metrics": dict(self.metrics),
            "raw": self.raw,
            "selected_record": dict(self.selected_record),
            "records": [dict(record) for record in self.records],
            "source_path": str(self.source_path),
        }


class AdapterError(ValueError):
    """Raised when a benchmark command or result cannot be represented safely."""


class BaseAdapter(ABC):
    """Base class for stable, public benchmark CLI adapters."""

    framework: ClassVar[str]

    @abstractmethod
    def build_command(
        self,
        mode: str | BenchmarkType,
        workload: Mapping[str, object],
        parameters: Mapping[str, object],
        base_args: Sequence[str],
        result_path: str | Path,
    ) -> AdapterCommand:
        """Translate framework-neutral campaign inputs into a benchmark argv."""

    def parse_result(
        self,
        path: str | Path,
        benchmark_type: str | BenchmarkType = BenchmarkType.SERVE,
    ) -> ParsedBenchmark:
        """Parse JSON or append-style JSONL without importing a framework package."""

        source_path = Path(path)
        payload, records = _read_json_records(source_path)
        selected = records[-1]
        metrics = extract_numeric_metrics(selected)
        _add_canonical_metrics(metrics, selected)
        return ParsedBenchmark(
            framework=self.framework,
            benchmark_type=coerce_benchmark_type(benchmark_type),
            metrics=metrics,
            raw=payload,
            selected_record=dict(selected),
            records=tuple(dict(record) for record in records),
            source_path=source_path,
        )


# Compatibility-friendly descriptive alias for callers that prefer the longer name.
BenchmarkAdapter = BaseAdapter


def coerce_benchmark_type(mode: str | BenchmarkType) -> BenchmarkType:
    if isinstance(mode, BenchmarkType):
        return mode
    try:
        return BenchmarkType(mode.strip().lower())
    except (AttributeError, ValueError) as exc:
        choices = ", ".join(item.value for item in BenchmarkType)
        raise AdapterError(
            f"unsupported benchmark mode {mode!r}; expected one of {choices}"
        ) from exc


def append_base_args(argv: list[str], base_args: Sequence[str]) -> None:
    """Append user-approved argv tokens while rejecting command strings and NULs."""

    if isinstance(base_args, str | bytes):
        raise AdapterError("base_args must be an argv sequence, not a command string")
    for index, arg in enumerate(base_args):
        if not isinstance(arg, str):
            raise AdapterError(f"base_args[{index}] must be a string")
        if "\x00" in arg:
            raise AdapterError(f"base_args[{index}] contains a NUL byte")
        argv.append(arg)


_OPTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def append_parameter_args(
    argv: list[str],
    parameters: Mapping[str, object],
    *,
    reserved: frozenset[str] = frozenset(),
) -> None:
    """Encode a mapping as deterministic CLI options.

    Mapping keys use Python-style underscores and are emitted with dashes.  ``True``
    emits a flag, ``False`` and ``None`` omit it, sequences use one option followed
    by all values, and nested mappings are encoded as compact JSON.
    """

    if not isinstance(parameters, Mapping):
        raise AdapterError("parameters must be a mapping")
    names: list[str] = []
    for name in parameters:
        if not isinstance(name, str) or _OPTION_NAME.fullmatch(name) is None:
            raise AdapterError(f"invalid benchmark parameter name: {name!r}")
        names.append(name)
    for name in sorted(names):
        normalized_name = name.replace("-", "_")
        if normalized_name in reserved:
            raise AdapterError(f"benchmark parameter {name!r} is managed by the adapter")
        value = parameters[name]
        if value is None or value is False:
            continue
        flag = "--" + name.replace("_", "-")
        argv.append(flag)
        if value is True:
            continue
        if isinstance(value, Mapping):
            try:
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise AdapterError(
                    f"mapping value for benchmark parameter {name!r} must be JSON-compatible"
                ) from exc
            argv.append(encoded)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            if not value:
                raise AdapterError(f"sequence value for {name!r} must not be empty")
            argv.extend(_scalar_cli_value(item, name) for item in value)
        else:
            argv.append(_scalar_cli_value(value, name))


def require_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"workload.{key} must be a non-empty string")
    return value


def optional_string(mapping: Mapping[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"workload.{key} must be a non-empty string when set")
    return value


def positive_int(mapping: Mapping[str, object], key: str, default: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError(f"workload.{key} must be a positive integer")
    return value


def cli_number(value: int | float | str, name: str) -> str:
    if isinstance(value, bool):
        raise AdapterError(f"{name} must be a number or supported CLI literal")
    if isinstance(value, float) and not math.isfinite(value):
        raise AdapterError(f"{name} must be finite")
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise AdapterError(f"{name} must be a number or supported CLI literal")


def extract_numeric_metrics(payload: Mapping[str, object], prefix: str = "") -> dict[str, float]:
    """Flatten finite JSON numeric scalars; large latency arrays stay in ``raw``."""

    metrics: dict[str, float] = {}
    for raw_key, value in payload.items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            numeric = float(value)
            if math.isfinite(numeric):
                metrics[key] = numeric
        elif isinstance(value, Mapping):
            metrics.update(extract_numeric_metrics(value, key))
    return metrics


def _read_json_records(path: Path) -> tuple[object, list[dict[str, object]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"cannot read benchmark result {path}: {exc}") from exc
    if not text.strip():
        raise AdapterError(f"benchmark result is empty: {path}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _parse_json_lines(text, path)

    if isinstance(payload, Mapping):
        records = [dict(payload)]
    elif (
        isinstance(payload, list) and payload and all(isinstance(item, Mapping) for item in payload)
    ):
        records = [dict(item) for item in payload]
    else:
        raise AdapterError(
            f"benchmark result {path} must contain an object or non-empty object list"
        )
    return payload, records


def _parse_json_lines(text: str, path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"invalid JSON in {path} at line {line_number}: {exc.msg}") from exc
        if not isinstance(item, Mapping):
            raise AdapterError(f"JSONL record {line_number} in {path} must be an object")
        records.append(dict(item))
    if not records:
        raise AdapterError(f"benchmark result is empty: {path}")
    return records


def _add_canonical_metrics(metrics: dict[str, float], selected: Mapping[str, object]) -> None:
    canonical_keys = (
        "completed",
        "failed",
        "request_throughput",
        "input_throughput",
        "output_throughput",
        "total_token_throughput",
        "request_goodput",
    )
    for key in canonical_keys:
        if key not in metrics:
            value = _find_numeric_leaf(selected, key)
            if value is not None:
                metrics[key] = value

    if "duration_seconds" not in metrics:
        for key in ("duration", "benchmark_duration", "elapsed_time", "elapsed_seconds"):
            value = _find_numeric_leaf(selected, key)
            if value is not None:
                metrics["duration_seconds"] = value
                break

    if "success_rate" in metrics:
        return
    completed = _find_numeric_leaf(selected, "completed")
    failed = _find_numeric_leaf(selected, "failed")
    requested = _find_numeric_leaf(selected, "num_prompts")
    denominator: float | None = None
    if completed is not None and failed is not None:
        denominator = completed + failed
    elif completed is not None and requested is not None:
        denominator = requested
    if completed is not None and denominator is not None and denominator > 0:
        metrics["success_rate"] = completed / denominator


def _find_numeric_leaf(payload: Mapping[str, object], wanted: str) -> float | None:
    value = payload.get(wanted)
    if not isinstance(value, bool) and isinstance(value, int | float):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    for nested in payload.values():
        if isinstance(nested, Mapping):
            found = _find_numeric_leaf(nested, wanted)
            if found is not None:
                return found
    return None


def _scalar_cli_value(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise AdapterError(f"unsupported CLI value for benchmark parameter {name!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise AdapterError(f"benchmark parameter {name!r} must be finite")
    text = str(value)
    if "\x00" in text:
        raise AdapterError(f"benchmark parameter {name!r} contains a NUL byte")
    return text


__all__ = [
    "AdapterCommand",
    "AdapterError",
    "BaseAdapter",
    "BenchmarkAdapter",
    "BenchmarkType",
    "ParsedBenchmark",
    "append_base_args",
    "append_parameter_args",
    "cli_number",
    "coerce_benchmark_type",
    "extract_numeric_metrics",
    "optional_string",
    "positive_int",
    "require_string",
]
