"""Repeated, fail-closed SGLang serving benchmark harness."""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
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
    module: str = "sglang.benchmark.serving"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BenchmarkSettings:
        values = os.environ if environ is None else environ
        dataset = _required(values, "EUBOULIA_DATASET").casefold()
        if dataset != "random":
            raise BenchmarkHarnessError(
                "the built-in SGLang fixed-length harness currently requires dataset=random"
            )
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
            random_range_ratio=_ratio(values, "EUBOULIA_RANDOM_RANGE_RATIO", 1.0),
            seed=_nonnegative_int(values, "EUBOULIA_BENCHMARK_SEED", 42),
            module=values.get("EUBOULIA_SGLANG_BENCHMARK_MODULE", "sglang.benchmark.serving"),
        )

    def command(self, output_path: Path) -> tuple[str, ...]:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BenchmarkHarnessError("EUBOULIA_TARGET_ENDPOINT must be an HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BenchmarkHarnessError(
                "EUBOULIA_TARGET_ENDPOINT must be a base URL without path, query, or fragment"
            )
        return (
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
            "--random-input-len",
            str(self.input_tokens),
            "--random-output-len",
            str(self.output_tokens),
            "--random-range-ratio",
            str(self.random_range_ratio),
            "--num-prompts",
            str(self.num_prompts),
            "--max-concurrency",
            str(self.concurrency),
            "--request-rate",
            str(self.request_rate),
            "--temperature",
            "0",
            "--seed",
            str(self.seed),
            "--flush-cache",
            "--disable-tqdm",
            "--output-file",
            str(output_path),
        )


SampleRunner = Callable[[BenchmarkSettings, Path], Mapping[str, object]]


def run_benchmark(
    settings: BenchmarkSettings,
    *,
    sample_runner: SampleRunner | None = None,
) -> dict[str, object]:
    """Run warmups and repetitions, then write median metrics atomically enough for MVP."""

    execute = _run_sample if sample_runner is None else sample_runner
    measured: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix=".euboulia-sglang-bench-", dir=Path.cwd()) as raw:
        scratch = Path(raw)
        for index in range(settings.warmups):
            sample = dict(execute(settings, scratch / f"warmup-{index + 1}.jsonl"))
            _validate_sample(sample, settings, f"warmup {index + 1}")
        for index in range(settings.repetitions):
            sample = dict(execute(settings, scratch / f"measured-{index + 1}.jsonl"))
            _validate_sample(sample, settings, f"repetition {index + 1}")
            measured.append(sample)

    metrics = _aggregate_numeric_metrics(measured)
    if "output_throughput" not in metrics:
        raise BenchmarkHarnessError("measured results contain no output_throughput")
    payload: dict[str, object] = {
        "aggregation": "median",
        "metrics": metrics,
        "repetitions": settings.repetitions,
        "warmups": settings.warmups,
        "samples": measured,
    }
    destination = settings.metrics_path
    if destination.is_absolute() or ".." in destination.parts:
        raise BenchmarkHarnessError("EUBOULIA_METRICS_PATH must remain inside the worktree")
    worktree = Path.cwd().resolve()
    destination = (worktree / destination).resolve()
    if not destination.is_relative_to(worktree):
        raise BenchmarkHarnessError("EUBOULIA_METRICS_PATH must remain inside the worktree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return payload


def _run_sample(settings: BenchmarkSettings, output_path: Path) -> Mapping[str, object]:
    completed = subprocess.run(settings.command(output_path), check=False, shell=False)
    if completed.returncode != 0:
        raise BenchmarkHarnessError(f"SGLang benchmark exited with status {completed.returncode}")
    return _read_last_json_object(output_path)


def _read_last_json_object(path: Path) -> Mapping[str, object]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        value: object = json.loads(lines[-1])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
        raise BenchmarkHarnessError(f"invalid benchmark output {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkHarnessError(f"benchmark output {path} must contain a JSON object")
    return cast(dict[str, object], value)


def _validate_sample(sample: Mapping[str, object], settings: BenchmarkSettings, label: str) -> None:
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


def _aggregate_numeric_metrics(samples: Sequence[Mapping[str, object]]) -> dict[str, float]:
    if not samples:
        raise BenchmarkHarnessError("at least one measured result is required")
    flattened = [_numeric_metrics(sample) for sample in samples]
    common = set(flattened[0])
    for sample in flattened[1:]:
        common.intersection_update(sample)
    return {
        key: float(statistics.median(sample[key] for sample in flattened)) for key in sorted(common)
    }


def _numeric_metrics(value: Mapping[str, object], prefix: str = "") -> dict[str, float]:
    metrics: dict[str, float] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key:
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


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise BenchmarkHarnessError(f"{name} is required")
    return value


def _positive_int(environ: Mapping[str, str], name: str) -> int:
    raw = environ.get(name, "")
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


def _ratio(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise BenchmarkHarnessError(f"{name} must be between 0 and 1") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise BenchmarkHarnessError(f"{name} must be between 0 and 1")
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
