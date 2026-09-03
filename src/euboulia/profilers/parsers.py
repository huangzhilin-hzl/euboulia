"""Offline parsers for PyTorch Chrome, Nsight Systems, and Nsight Compute exports."""

from __future__ import annotations

import csv
import gzip
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .models import MetricValue, Observation, ProfileSource, SubjectKind


class ProfileParseError(ValueError):
    """Raised when a profiler artifact lacks the minimum semantic structure."""


_UNIT_TO_NS = {
    "ns": 1.0,
    "nanosecond": 1.0,
    "nanoseconds": 1.0,
    "us": 1_000.0,
    "µs": 1_000.0,
    "microsecond": 1_000.0,
    "microseconds": 1_000.0,
    "ms": 1_000_000.0,
    "millisecond": 1_000_000.0,
    "milliseconds": 1_000_000.0,
    "s": 1_000_000_000.0,
    "sec": 1_000_000_000.0,
    "second": 1_000_000_000.0,
    "seconds": 1_000_000_000.0,
}


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProfileParseError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProfileParseError(f"{field_name} must be finite")
    return result


def _to_ns(value: float, unit: str, field_name: str) -> int:
    multiplier = _UNIT_TO_NS.get(unit.strip().lower())
    if multiplier is None:
        raise ProfileParseError(f"unsupported {field_name} time unit: {unit!r}")
    result = value * multiplier
    if not math.isfinite(result) or result < 0:
        raise ProfileParseError(f"{field_name} must be finite and non-negative")
    return round(result)


def parse_torch_chrome_trace(
    path: str | Path, *, timestamp_unit: str = "us"
) -> tuple[Observation, ...]:
    """Parse complete-duration events from a PyTorch/Kineto Chrome trace.

    Chrome trace ``ts`` and ``dur`` are conventionally microseconds.  The root
    ``displayTimeUnit`` controls the viewer display and is intentionally not used
    as a raw timestamp scale.  Callers may override ``timestamp_unit`` for a
    non-standard producer.
    """

    return tuple(iter_torch_chrome_trace(path, timestamp_unit=timestamp_unit))


def iter_torch_chrome_trace(
    path: str | Path, *, timestamp_unit: str = "us"
) -> Iterator[Observation]:
    """Yield observations without materializing a multi-gigabyte trace document."""

    source_path = Path(path)
    try:
        with _open_trace(source_path) as handle:
            for index, event in enumerate(_iter_chrome_events(handle)):
                observation = _torch_observation(
                    event,
                    index=index,
                    source_path=source_path,
                    timestamp_unit=timestamp_unit,
                )
                if observation is not None:
                    yield observation
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileParseError(f"cannot stream Chrome trace {source_path}: {exc}") from exc


def _torch_observation(
    event: object,
    *,
    index: int,
    source_path: Path,
    timestamp_unit: str,
) -> Observation | None:
    if not isinstance(event, Mapping) or event.get("ph") != "X":
        return None
    name = event.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        start = _finite_number(event.get("ts"), f"traceEvents[{index}].ts")
        duration = _finite_number(event.get("dur"), f"traceEvents[{index}].dur")
        start_ns = _to_ns(start, timestamp_unit, "timestamp")
        duration_ns = _to_ns(duration, timestamp_unit, "duration")
    except ProfileParseError:
        # Metadata-like complete events occasionally omit a usable duration;
        # they are not observations and should not poison the whole artifact.
        return None

    category = str(event.get("cat", ""))
    args = event.get("args")
    safe_args = _safe_json_mapping(args if isinstance(args, Mapping) else {})
    correlation_ids: dict[str, str] = {}
    for key in ("External id", "External Id", "Correlation ID", "correlation", "id"):
        item = safe_args.get(key)
        if isinstance(item, str | int):
            correlation_ids[_header_name(key)] = str(item)
    return Observation(
        observation_id=f"torch:{index}",
        source=ProfileSource.TORCH_CHROME_TRACE,
        subject_kind=_trace_subject(category, name),
        name=name,
        artifact=str(source_path),
        start_ns=start_ns,
        duration_ns=duration_ns,
        count=1,
        process_id=_identity(event.get("pid")),
        thread_id=_identity(event.get("tid")),
        device=_first_identity(safe_args, "Device", "device", "Device Id"),
        stream=_first_identity(safe_args, "stream", "Stream", "stream id"),
        rank=_first_identity(safe_args, "rank", "Rank"),
        phase=_first_identity(safe_args, "phase", "Phase"),
        native_category=category or None,
        correlation_ids=correlation_ids,
        dimensions=safe_args,
    )


def _iter_chrome_events(handle: TextIO) -> Iterator[object]:
    """Incrementally decode a top-level array or an object's ``traceEvents`` array."""

    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    eof = False

    def read_more() -> None:
        nonlocal buffer, position, eof
        if position:
            buffer = buffer[position:]
            position = 0
        chunk = handle.read(1024 * 1024)
        if chunk:
            buffer += chunk
        else:
            eof = True

    read_more()
    while not eof and not buffer.strip():
        read_more()
    stripped = buffer.lstrip()
    position = len(buffer) - len(stripped)
    if position >= len(buffer):
        raise ProfileParseError("Chrome trace is empty")
    if buffer[position] == "[":
        position += 1
    elif buffer[position] == "{":
        marker = '"traceEvents"'
        while True:
            marker_index = buffer.find(marker, position)
            if marker_index >= 0:
                colon_index = buffer.find(":", marker_index + len(marker))
                array_index = -1 if colon_index < 0 else buffer.find("[", colon_index + 1)
                if array_index >= 0:
                    position = array_index + 1
                    break
            if eof:
                raise ProfileParseError("Chrome trace object does not contain traceEvents")
            if len(buffer) > 64 * 1024 * 1024:
                raise ProfileParseError("Chrome trace preamble exceeds 64 MiB")
            chunk = handle.read(1024 * 1024)
            if chunk:
                buffer += chunk
            else:
                eof = True
    else:
        raise ProfileParseError("Chrome trace must be an event array or contain traceEvents")

    while True:
        while True:
            while position < len(buffer) and (
                buffer[position].isspace() or buffer[position] == ","
            ):
                position += 1
            if position < len(buffer):
                break
            if eof:
                raise ProfileParseError("Chrome trace event array is incomplete")
            read_more()
        if buffer[position] == "]":
            return
        try:
            event, end = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError as exc:
            if eof:
                raise ProfileParseError(f"invalid Chrome trace event: {exc}") from exc
            if len(buffer) - position > 64 * 1024 * 1024:
                raise ProfileParseError("one Chrome trace event exceeds 64 MiB") from exc
            read_more()
            continue
        position = end
        yield event


def _open_trace(path: Path) -> TextIO:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def _trace_subject(category: str, name: str) -> SubjectKind:
    category_text = category.lower()
    text = f"{category} {name}".lower()
    # A runtime API can contain "Kernel" in its function name; the activity
    # category is therefore stronger evidence than a name substring.
    if any(token in category_text for token in ("cuda_runtime", "cuda_driver", "cuda_sync")):
        return SubjectKind.RUNTIME_API
    if any(token in text for token in ("memcpy", "memory copy")):
        return SubjectKind.MEMORY_TRANSFER
    if "memset" in text:
        return SubjectKind.MEMORY_SET
    if any(token in text for token in ("kernel", "concurrent_kernel", "gpu_op")):
        return SubjectKind.GPU_KERNEL
    if any(token in text for token in ("cuda_runtime", "cuda_driver", "cuda_sync")):
        return SubjectKind.RUNTIME_API
    if any(token in text for token in ("user_annotation", "nvtx", "record_function")):
        return SubjectKind.NVTX_RANGE
    if any(token in text for token in ("cpu_op", "python", "cpu")):
        return SubjectKind.CPU_OP
    return SubjectKind.UNKNOWN


def _identity(value: object) -> str | None:
    if isinstance(value, str | int) and str(value).strip():
        return str(value)
    return None


def _first_identity(value: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        result = _identity(value.get(name))
        if result is not None:
            return result
    return None


def _safe_json_mapping(value: Mapping[object, object]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        try:
            encoded = json.dumps(item, allow_nan=False)
            result[key] = json.loads(encoded)
        except (TypeError, ValueError):
            continue
    return result


_PAREN_UNIT = re.compile(r"\(([^()]*)\)\s*$")
_NON_ALNUM = re.compile(r"[^a-z0-9%]+")


def _header_name(value: str) -> str:
    without_unit = _PAREN_UNIT.sub("", value.strip().lower())
    return _NON_ALNUM.sub(" ", without_unit).strip()


def _header_unit(value: str, default: str = "") -> str:
    match = _PAREN_UNIT.search(value.strip())
    return match.group(1).strip() if match else default


def _column(header: Sequence[str], *aliases: str) -> int | None:
    normalized = [_header_name(item) for item in header]
    choices = {_header_name(item) for item in aliases}
    for index, name in enumerate(normalized):
        if name in choices:
            return index
    return None


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _csv_number(value: str, field_name: str) -> float:
    normalized = value.strip().replace(",", "").replace("%", "")
    if not normalized:
        raise ProfileParseError(f"{field_name} is empty")
    try:
        result = float(normalized)
    except ValueError as exc:
        raise ProfileParseError(f"{field_name} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise ProfileParseError(f"{field_name} must be finite")
    return result


def _csv_rows(path: Path) -> list[list[str]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ProfileParseError(f"cannot read CSV {path}: {exc}") from exc


_NSYS_REPORTS = {
    "cuda_gpu_kern_sum": (SubjectKind.GPU_KERNEL, ("instances", "count")),
    "cuda_api_sum": (SubjectKind.RUNTIME_API, ("num calls", "calls", "instances")),
    "cuda_gpu_mem_time_sum": (SubjectKind.MEMORY_TRANSFER, ("count", "instances")),
}


def parse_nsys_stats_csv(
    path: str | Path, *, report: str, default_time_unit: str = "ns"
) -> tuple[Observation, ...]:
    """Parse one official ``nsys stats --format csv`` report by header meaning.

    Supported summary reports are ``cuda_gpu_kern_sum``, ``cuda_api_sum``, and
    ``cuda_gpu_mem_time_sum``; ``cuda_gpu_trace`` is accepted for per-event
    timelines.  Column order is deliberately ignored.
    """

    source_path = Path(path)
    rows = _csv_rows(source_path)
    if report == "cuda_gpu_trace":
        return _parse_nsys_trace(rows, source_path, default_time_unit)
    spec = _NSYS_REPORTS.get(report)
    if spec is None:
        choices = ", ".join((*_NSYS_REPORTS, "cuda_gpu_trace"))
        raise ProfileParseError(f"unsupported nsys report {report!r}; expected one of {choices}")
    subject_kind, count_aliases = spec
    header_index = _find_nsys_header(rows, summary=True)
    header = rows[header_index]
    total_index = _column(header, "total time")
    name_index = _column(header, "name", "operation")
    count_index = _column(header, *count_aliases)
    if total_index is None or name_index is None or count_index is None:
        raise ProfileParseError(f"{report} lacks Total Time, count, or Name/Operation columns")

    duration_columns = {
        "average_duration_ns": _column(header, "avg", "average"),
        "median_duration_ns": _column(header, "med", "median"),
        "minimum_duration_ns": _column(header, "min", "minimum"),
        "maximum_duration_ns": _column(header, "max", "maximum"),
        "stddev_duration_ns": _column(header, "stddev", "standard deviation"),
    }
    share_index = _column(header, "time %", "time")
    observations: list[Observation] = []
    for row_number, row in _data_rows(rows, header_index + 1):
        name = _cell(row, name_index)
        if not name:
            continue
        total_text = _cell(row, total_index)
        count_text = _cell(row, count_index)
        if not total_text or not count_text:
            continue
        total_ns = _to_ns(
            _csv_number(total_text, "Total Time"),
            _header_unit(header[total_index], default_time_unit),
            "Total Time",
        )
        count_value = _csv_number(count_text, "count")
        if count_value < 0 or not count_value.is_integer():
            raise ProfileParseError("nsys count must be a non-negative integer")
        metrics: dict[str, MetricValue] = {
            "total_duration_ns": MetricValue(total_ns, "ns", header[total_index])
        }
        for key, index in duration_columns.items():
            text = _cell(row, index)
            if index is not None and text:
                value_ns = _to_ns(
                    _csv_number(text, header[index]),
                    _header_unit(header[index], default_time_unit),
                    header[index],
                )
                metrics[key] = MetricValue(value_ns, "ns", header[index])
        share_text = _cell(row, share_index)
        if share_index is not None and share_text:
            metrics["report_share_pct"] = MetricValue(
                _csv_number(share_text, header[share_index]), "%", header[share_index]
            )
        observations.append(
            Observation(
                observation_id=f"nsys:{report}:{row_number}",
                source=ProfileSource.NSYS_STATS_CSV,
                subject_kind=subject_kind,
                name=name,
                artifact=str(source_path),
                duration_ns=total_ns,
                count=int(count_value),
                metrics=metrics,
                native_category=report,
                warnings=(
                    "report_share_pct is relative to rows in this nsys report, not wall time",
                ),
            )
        )
    if not observations:
        raise ProfileParseError(f"{report} contains no parseable data rows")
    return tuple(observations)


def _find_nsys_header(rows: Sequence[Sequence[str]], *, summary: bool) -> int:
    for index, row in enumerate(rows):
        names = {_header_name(item) for item in row}
        has_name = "name" in names or "operation" in names
        required = "total time" if summary else "duration"
        if has_name and required in names:
            return index
    kind = "summary" if summary else "trace"
    raise ProfileParseError(f"cannot find an nsys {kind} header")


def _data_rows(rows: Sequence[Sequence[str]], start: int) -> Iterable[tuple[int, Sequence[str]]]:
    seen_data = False
    for index in range(start, len(rows)):
        row = rows[index]
        if not any(cell.strip() for cell in row):
            if seen_data:
                break
            continue
        names = {_header_name(item) for item in row}
        if seen_data and ("total time" in names or "duration" in names) and "name" in names:
            break
        seen_data = True
        yield index + 1, row


def _parse_nsys_trace(
    rows: Sequence[Sequence[str]], source_path: Path, default_time_unit: str
) -> tuple[Observation, ...]:
    header_index = _find_nsys_header(rows, summary=False)
    header = rows[header_index]
    start_index = _column(header, "start")
    duration_index = _column(header, "duration")
    name_index = _column(header, "name")
    if start_index is None or duration_index is None or name_index is None:
        raise ProfileParseError("cuda_gpu_trace lacks Start, Duration, or Name columns")
    device_index = _column(header, "device", "device id")
    stream_index = _column(header, "stream", "stream id")
    correlation_index = _column(header, "corrid", "correlation id")
    observations: list[Observation] = []
    for row_number, row in _data_rows(rows, header_index + 1):
        name = _cell(row, name_index)
        if not name:
            continue
        start_ns = _to_ns(
            _csv_number(_cell(row, start_index), "Start"),
            _header_unit(header[start_index], default_time_unit),
            "Start",
        )
        duration_ns = _to_ns(
            _csv_number(_cell(row, duration_index), "Duration"),
            _header_unit(header[duration_index], default_time_unit),
            "Duration",
        )
        correlation = _cell(row, correlation_index)
        observations.append(
            Observation(
                observation_id=f"nsys:cuda_gpu_trace:{row_number}",
                source=ProfileSource.NSYS_STATS_CSV,
                subject_kind=_trace_subject("kernel", name),
                name=name,
                artifact=str(source_path),
                start_ns=start_ns,
                duration_ns=duration_ns,
                count=1,
                device=_cell(row, device_index) or None,
                stream=_cell(row, stream_index) or None,
                native_category="cuda_gpu_trace",
                correlation_ids={"correlation_id": correlation} if correlation else {},
            )
        )
    if not observations:
        raise ProfileParseError("cuda_gpu_trace contains no parseable data rows")
    return tuple(observations)


_NCU_METRIC_ALIASES = {
    "gpu__time_duration.sum": "gpu_duration_ns",
    "sm__throughput.avg.pct_of_peak_sustained_elapsed": "sm_throughput_pct",
    "gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed": ("memory_throughput_pct"),
    "dram__throughput.avg.pct_of_peak_sustained_elapsed": "dram_throughput_pct",
    "sm__warps_active.avg.pct_of_peak_sustained_active": "achieved_occupancy_pct",
    "launch__occupancy_limit_registers": "occupancy_limit_registers",
    "launch__occupancy_limit_shared_mem": "occupancy_limit_shared_memory",
    "launch__occupancy_limit_warps": "occupancy_limit_warps",
}


def parse_ncu_csv(path: str | Path) -> tuple[Observation, ...]:
    """Parse ``ncu --page raw --csv --print-metric-name name`` output.

    Metrics are grouped by the launch identity columns that exist in the export.
    Unknown native metrics are preserved rather than rejected because metric
    availability is GPU- and Nsight-version-dependent.
    """

    source_path = Path(path)
    rows = _csv_rows(source_path)
    header_index = _find_ncu_header(rows)
    header = rows[header_index]
    metric_name_index = _column(header, "metric name")
    metric_unit_index = _column(header, "metric unit")
    metric_value_index = _column(header, "metric value")
    assert metric_name_index is not None
    assert metric_unit_index is not None
    assert metric_value_index is not None

    identity_aliases = (
        "id",
        "process id",
        "process name",
        "host name",
        "context",
        "stream",
        "kernel name",
    )
    identity_indexes = {
        alias: index for alias in identity_aliases if (index := _column(header, alias)) is not None
    }
    carried: dict[str, str] = {}
    groups: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    synthetic_group = 0
    for row_number, row in _data_rows(rows, header_index + 1):
        metric_name = _cell(row, metric_name_index)
        if not metric_name:
            continue
        for name, index in identity_indexes.items():
            text = _cell(row, index)
            if text:
                carried[name] = text
        identity = tuple((name, carried[name]) for name in identity_aliases if name in carried)
        if not identity:
            identity = (("synthetic", str(synthetic_group)),)
        group = groups.setdefault(
            identity,
            {
                "name": carried.get("kernel name"),
                "metrics": {},
                "warnings": [],
                "rows": [],
                "identity": dict(identity),
            },
        )
        group["rows"].append(row_number)
        unit = _cell(row, metric_unit_index)
        value_text = _cell(row, metric_value_index)
        if metric_name == "launch__kernel_name" and value_text:
            group["name"] = value_text
            continue
        try:
            numeric_value = _csv_number(value_text, metric_name)
        except ProfileParseError:
            group["warnings"].append(f"skipped non-numeric metric {metric_name!r}")
            continue
        canonical_name = _NCU_METRIC_ALIASES.get(metric_name, metric_name)
        normalized_unit = _normalize_ncu_unit(unit)
        if canonical_name == "gpu_duration_ns":
            numeric_value = _to_ns(numeric_value, normalized_unit or "ns", metric_name)
            normalized_unit = "ns"
        metric_key = _unique_metric_key(group["metrics"], canonical_name)
        group["metrics"][metric_key] = MetricValue(
            numeric_value, normalized_unit, native_name=metric_name
        )

    observations: list[Observation] = []
    for index, (_, group) in enumerate(groups.items()):
        metrics: dict[str, MetricValue] = group["metrics"]
        name = group["name"] or f"kernel-launch-{index}"
        duration_metric = metrics.get("gpu_duration_ns")
        identity = group["identity"]
        observations.append(
            Observation(
                observation_id=f"ncu:{index}",
                source=ProfileSource.NCU_CSV,
                subject_kind=SubjectKind.GPU_KERNEL,
                name=name,
                artifact=str(source_path),
                duration_ns=round(duration_metric.value) if duration_metric else None,
                count=1,
                metrics=metrics,
                process_id=identity.get("process id"),
                device=None,
                stream=identity.get("stream"),
                native_category="ncu_raw",
                dimensions={
                    key.replace(" ", "_"): value
                    for key, value in identity.items()
                    if key != "synthetic"
                },
                warnings=tuple(group["warnings"]),
            )
        )
    if not observations:
        raise ProfileParseError("NCU CSV contains no metric rows")
    return tuple(observations)


def _find_ncu_header(rows: Sequence[Sequence[str]]) -> int:
    required = {"metric name", "metric unit", "metric value"}
    for index, row in enumerate(rows):
        if required.issubset({_header_name(item) for item in row}):
            return index
    raise ProfileParseError("NCU CSV lacks Metric Name, Metric Unit, or Metric Value columns")


def _normalize_ncu_unit(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {
        "nsecond": "ns",
        "nanosecond": "ns",
        "usecond": "us",
        "microsecond": "us",
        "msecond": "ms",
        "millisecond": "ms",
        "second": "s",
        "percent": "%",
    }
    return aliases.get(normalized, value.strip())


def _unique_metric_key(metrics: Mapping[str, object], preferred: str) -> str:
    if preferred not in metrics:
        return preferred
    suffix = 2
    while f"{preferred}#{suffix}" in metrics:
        suffix += 1
    return f"{preferred}#{suffix}"


def parse_profile(
    path: str | Path,
    source: ProfileSource | str,
    *,
    nsys_report: str | None = None,
    timestamp_unit: str = "us",
) -> tuple[Observation, ...]:
    """Dispatch an offline artifact to its explicit parser."""

    try:
        normalized = source if isinstance(source, ProfileSource) else ProfileSource(source)
    except ValueError as exc:
        raise ProfileParseError(f"unsupported profile source: {source!r}") from exc
    if normalized is ProfileSource.TORCH_CHROME_TRACE:
        return parse_torch_chrome_trace(path, timestamp_unit=timestamp_unit)
    if normalized is ProfileSource.NSYS_STATS_CSV:
        if not nsys_report:
            raise ProfileParseError("nsys_report is required for an nsys stats CSV")
        return parse_nsys_stats_csv(path, report=nsys_report)
    return parse_ncu_csv(path)


__all__ = [
    "ProfileParseError",
    "parse_ncu_csv",
    "parse_nsys_stats_csv",
    "parse_profile",
    "parse_torch_chrome_trace",
]
