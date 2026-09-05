"""Repeated, fail-closed SGLang serving benchmark harness."""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit


class BenchmarkHarnessError(RuntimeError):
    """Raised when a benchmark command or result violates the harness contract."""


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    """Fixed workload and execution settings supplied by the runner."""

    endpoint: str
    model: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_prompts: int
    dataset: str
    warmups: int
    repetitions: int
    metrics_path: Path
    request_rate: str | float = "inf"
    backend: str = "sglang"
    random_range_ratio: float = 1.0
    seed: int = 42
    flush_cache: bool = True
    module: str = "sglang.bench_serving"
    dataset_path: Path | None = None
    evidence_dir: Path | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BenchmarkSettings:
        values = os.environ if environ is None else environ
        parameters = _benchmark_parameters(values)
        dataset = _required(values, "EUBOULIA_DATASET").casefold()
        if dataset not in {"random", "random-ids", "sharegpt"}:
            raise BenchmarkHarnessError(
                "the built-in SGLang harness requires dataset=random, random-ids, or sharegpt"
            )
        dataset_path_value = values.get("EUBOULIA_DATASET_PATH")
        dataset_path = Path(dataset_path_value) if dataset_path_value else None
        if dataset == "sharegpt" and dataset_path is None:
            raise BenchmarkHarnessError("EUBOULIA_DATASET_PATH is required for dataset=sharegpt")
        return cls(
            endpoint=_required(values, "EUBOULIA_TARGET_ENDPOINT"),
            model=values.get("EUBOULIA_MODEL_SERVED_NAME") or _required(values, "EUBOULIA_MODEL"),
            input_tokens=_positive_int(values, "EUBOULIA_INPUT_TOKENS"),
            output_tokens=_positive_int(values, "EUBOULIA_OUTPUT_TOKENS"),
            concurrency=_positive_int(values, "EUBOULIA_CONCURRENCY"),
            num_prompts=_positive_int(values, "EUBOULIA_NUM_PROMPTS"),
            dataset=dataset,
            warmups=_nonnegative_int(values, "EUBOULIA_WARMUPS", 0),
            repetitions=_positive_int(values, "EUBOULIA_REPETITIONS"),
            metrics_path=Path(values.get("EUBOULIA_METRICS_PATH", "euboulia-result.json")),
            request_rate=_request_rate(values, "EUBOULIA_REQUEST_RATE", "inf"),
            backend=values.get("EUBOULIA_SGLANG_BENCHMARK_BACKEND", "sglang"),
            random_range_ratio=_parameter_ratio(parameters, "random_range_ratio", 1.0),
            seed=_parameter_nonnegative_int(parameters, "seed", 42),
            flush_cache=_parameter_bool(parameters, "flush_cache", True),
            module=values.get("EUBOULIA_SGLANG_BENCHMARK_MODULE", "sglang.bench_serving"),
            dataset_path=dataset_path,
            evidence_dir=(
                Path(values["EUBOULIA_COMMAND_EVIDENCE_DIR"])
                if values.get("EUBOULIA_COMMAND_EVIDENCE_DIR")
                else None
            ),
        )

    def command(self, output_path: Path) -> tuple[str, ...]:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BenchmarkHarnessError("EUBOULIA_TARGET_ENDPOINT must be an HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BenchmarkHarnessError(
                "EUBOULIA_TARGET_ENDPOINT must be a base URL without path, query, or fragment"
            )
        common = (
            sys.executable,
            "-m",
            self.module,
            "--backend",
            self.backend,
            "--base-url",
            self.endpoint.rstrip("/"),
            "--model",
            self.model,
            "--dataset-name",
            self.dataset,
        )
        dataset_args: tuple[str, ...]
        if self.dataset in {"random", "random-ids"}:
            dataset_args = (
                "--random-input-len",
                str(self.input_tokens),
                "--random-output-len",
                str(self.output_tokens),
                "--random-range-ratio",
                str(self.random_range_ratio),
            )
        else:
            if self.dataset_path is None:
                raise BenchmarkHarnessError("a dataset path is required for dataset=sharegpt")
            dataset_args = (
                "--dataset-path",
                str(self.dataset_path),
                "--sharegpt-output-len",
                str(self.output_tokens),
                "--sharegpt-context-len",
                str(self.input_tokens + self.output_tokens),
            )
        cache_args = ("--flush-cache",) if self.flush_cache else ()
        return (
            *common,
            *dataset_args,
            "--num-prompts",
            str(self.num_prompts),
            "--max-concurrency",
            str(self.concurrency),
            "--request-rate",
            str(self.request_rate),
            "--seed",
            str(self.seed),
            *cache_args,
            "--disable-tqdm",
            "--output-file",
            str(output_path),
            "--output-details",
        )


SampleRunner = Callable[[BenchmarkSettings, Path], Mapping[str, object]]


def run_benchmark(
    settings: BenchmarkSettings,
    *,
    sample_runner: SampleRunner | None = None,
) -> dict[str, object]:
    """Run a cache-clean workload and preserve every raw benchmark result."""

    execute = _run_sample if sample_runner is None else sample_runner
    measured: list[dict[str, object]] = []
    destination = _metrics_destination(settings.metrics_path)
    evidence_dir = _evidence_destination(settings.evidence_dir, destination)
    evidence_dir.mkdir(parents=True, exist_ok=False)
    for index in range(settings.warmups):
        output = evidence_dir / f"warmup-{index + 1}.jsonl"
        sample = dict(execute(settings, output))
        _persist_injected_sample(output, sample)
        _validate_sample(sample, settings, f"warmup {index + 1}")
    for index in range(settings.repetitions):
        round_id = index + 1
        output = evidence_dir / f"measured-{round_id}.jsonl"
        sample = dict(execute(settings, output))
        _persist_injected_sample(output, sample)
        _validate_sample(sample, settings, f"repetition {round_id}")
        measured.append(sample)

    metrics = _aggregate_numeric_metrics(measured)
    if "output_throughput" not in metrics:
        raise BenchmarkHarnessError("measured results contain no output_throughput")
    payload: dict[str, object] = {
        "aggregation": "median",
        "metrics": metrics,
        "average_metrics": _aggregate_numeric_metrics(measured, aggregation="mean"),
        "best_metrics": _best_numeric_metrics(measured),
        "repetitions": settings.repetitions,
        "warmups": settings.warmups,
        "dataset": settings.dataset,
        "evidence_dir": str(evidence_dir),
        "samples": measured,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    _write_evidence_manifest(evidence_dir, settings)
    return payload


def _run_sample(settings: BenchmarkSettings, output_path: Path) -> Mapping[str, object]:
    log_path = output_path.with_suffix(".log")
    with log_path.open("xb") as log:
        completed = subprocess.run(
            settings.command(output_path),
            check=False,
            shell=False,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if completed.returncode != 0:
        raise BenchmarkHarnessError(
            f"SGLang benchmark exited with status {completed.returncode}; log: {log_path}"
        )
    return dict(_read_last_json_object(output_path))


def _read_last_json_object(path: Path) -> Mapping[str, object]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        value: object = json.loads(lines[-1])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
        raise BenchmarkHarnessError(f"invalid benchmark output {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkHarnessError(f"benchmark output {path} must contain a JSON object")
    return cast(dict[str, object], value)


def _validate_sample(
    sample: Mapping[str, object],
    settings: BenchmarkSettings,
    label: str,
) -> None:
    completed = sample.get("completed")
    if isinstance(completed, bool) or not isinstance(completed, int | float):
        raise BenchmarkHarnessError(f"{label} does not report numeric completed requests")
    if int(completed) != settings.num_prompts or float(completed) != int(completed):
        raise BenchmarkHarnessError(
            f"{label} completed {completed:g}/{settings.num_prompts} requests"
        )
    errors = sample.get("errors")
    if isinstance(errors, list) and any(error for error in errors):
        raise BenchmarkHarnessError(f"{label} reports one or more request errors")
    throughput = sample.get("output_throughput")
    if (
        isinstance(throughput, bool)
        or not isinstance(throughput, int | float)
        or not math.isfinite(float(throughput))
        or float(throughput) <= 0
    ):
        raise BenchmarkHarnessError(f"{label} has invalid output_throughput")
    if settings.dataset in {"random", "random-ids"} and settings.random_range_ratio == 1.0:
        for key, expected in (
            ("input_lens", settings.input_tokens),
            ("output_lens", settings.output_tokens),
        ):
            lengths = sample.get(key)
            if not isinstance(lengths, list) or len(lengths) != settings.num_prompts:
                raise BenchmarkHarnessError(
                    f"{label} must report {settings.num_prompts} values in {key}"
                )
            if any(isinstance(value, bool) or value != expected for value in lengths):
                raise BenchmarkHarnessError(
                    f"{label} {key} must all equal the declared length {expected}"
                )


def _aggregate_numeric_metrics(
    samples: Sequence[Mapping[str, object]], *, aggregation: str = "median"
) -> dict[str, float]:
    if not samples:
        raise BenchmarkHarnessError("at least one measured result is required")
    flattened = [_numeric_metrics(sample) for sample in samples]
    common = set(flattened[0])
    for sample in flattened[1:]:
        common.intersection_update(sample)
    operation = statistics.median if aggregation == "median" else statistics.mean
    return {key: float(operation(sample[key] for sample in flattened)) for key in sorted(common)}


def _best_numeric_metrics(samples: Sequence[Mapping[str, object]]) -> dict[str, float]:
    flattened = [_numeric_metrics(sample) for sample in samples]
    common = set(flattened[0])
    for sample in flattened[1:]:
        common.intersection_update(sample)
    result: dict[str, float] = {}
    for key in sorted(common):
        values = [sample[key] for sample in flattened]
        minimize = any(token in key.casefold() for token in ("latency", "ttft", "tpot"))
        result[key] = float(min(values) if minimize else max(values))
    return result


def _numeric_metrics(value: Mapping[str, object], prefix: str = "") -> dict[str, float]:
    metrics: dict[str, float] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key:
            continue
        if key.startswith("_euboulia_"):
            continue
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(child, bool) or child is None or isinstance(child, str | list):
            continue
        if isinstance(child, int | float):
            number = float(child)
            if math.isfinite(number):
                metrics[name] = number
        elif isinstance(child, Mapping):
            metrics.update(_numeric_metrics(cast(Mapping[str, object], child), name))
    return metrics


def _metrics_destination(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise BenchmarkHarnessError("EUBOULIA_METRICS_PATH must remain inside the worktree")
    worktree = Path.cwd().resolve()
    destination = (worktree / path).resolve()
    if not destination.is_relative_to(worktree):
        raise BenchmarkHarnessError("EUBOULIA_METRICS_PATH must remain inside the worktree")
    return destination


def _evidence_destination(configured: Path | None, metrics_path: Path) -> Path:
    if configured is None:
        return metrics_path.parent / f"{metrics_path.stem}-evidence"
    candidate = configured if configured.is_absolute() else Path.cwd() / configured
    return candidate.resolve()


def _persist_injected_sample(output_path: Path, sample: Mapping[str, object]) -> None:
    if not output_path.exists():
        output_path.write_text(json.dumps(sample, sort_keys=True) + "\n", encoding="utf-8")


def _write_evidence_manifest(evidence_dir: Path, settings: BenchmarkSettings) -> None:
    files = []
    for path in sorted(evidence_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and not path.is_symlink() and path.name != "evidence-manifest.json":
            files.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    payload = {
        "schema_version": 1,
        "dataset": settings.dataset,
        "input_tokens": settings.input_tokens,
        "output_tokens": settings.output_tokens,
        "concurrency": settings.concurrency,
        "num_prompts": settings.num_prompts,
        "warmups": settings.warmups,
        "repetitions": settings.repetitions,
        "seed": settings.seed,
        "random_range_ratio": settings.random_range_ratio,
        "flush_cache": settings.flush_cache,
        "files": files,
    }
    (evidence_dir / "evidence-manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise BenchmarkHarnessError(f"{name} is required")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = environ.get(name, "" if default is None else str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BenchmarkHarnessError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise BenchmarkHarnessError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BenchmarkHarnessError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise BenchmarkHarnessError(f"{name} must be a non-negative integer")
    return value


def _benchmark_parameters(environ: Mapping[str, str]) -> Mapping[str, object]:
    raw = environ.get("EUBOULIA_BENCHMARK_PARAMETERS", "{}").strip()
    try:
        value: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkHarnessError(
            "EUBOULIA_BENCHMARK_PARAMETERS must be a JSON object"
        ) from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BenchmarkHarnessError(
            "EUBOULIA_BENCHMARK_PARAMETERS must be a JSON object"
        )
    unknown = sorted(set(value) - {"flush_cache", "random_range_ratio", "seed"})
    if unknown:
        raise BenchmarkHarnessError(
            "unsupported benchmark parameter(s): " + ", ".join(unknown)
        )
    return cast(dict[str, object], value)


def _parameter_nonnegative_int(
    parameters: Mapping[str, object], name: str, default: int
) -> int:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkHarnessError(f"benchmark parameter {name} must be a non-negative integer")
    return value


def _parameter_ratio(
    parameters: Mapping[str, object], name: str, default: float
) -> float:
    value = parameters.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise BenchmarkHarnessError(f"benchmark parameter {name} must be between 0 and 1")
    return float(value)


def _parameter_bool(
    parameters: Mapping[str, object], name: str, default: bool
) -> bool:
    value = parameters.get(name, default)
    if not isinstance(value, bool):
        raise BenchmarkHarnessError(f"benchmark parameter {name} must be a boolean")
    return value


def _request_rate(environ: Mapping[str, str], name: str, default: str | float) -> str | float:
    raw = environ.get(name, str(default)).strip().casefold()
    if raw == "inf":
        return "inf"
    try:
        value = float(raw)
    except ValueError as exc:
        raise BenchmarkHarnessError(f"{name} must be 'inf' or a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise BenchmarkHarnessError(f"{name} must be 'inf' or a positive number")
    return value


def main() -> int:
    """CLI entrypoint used by optimization performance commands."""

    try:
        payload = run_benchmark(BenchmarkSettings.from_environment())
    except BenchmarkHarnessError as exc:
        print(f"SGLang benchmark failed: {exc}", file=sys.stderr)
        return 1
    metrics = cast(dict[str, float], payload["metrics"])
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main tests
    raise SystemExit(main())
