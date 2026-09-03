"""Repeated, fail-closed SGLang serving benchmark harness."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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
    dataset_path: Path | None = None
    dataset_manifest_path: Path | None = None
    evidence_dir: Path | None = None
    flush_timeout_seconds: float = 60.0
    dataset_samples: int = 16

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BenchmarkSettings:
        values = os.environ if environ is None else environ
        dataset = _required(values, "EUBOULIA_DATASET").casefold()
        if dataset not in {"random", "sharegpt"}:
            raise BenchmarkHarnessError(
                "the built-in SGLang harness requires dataset=random or dataset=sharegpt"
            )
        dataset_path: Path | None = None
        manifest_path: Path | None = None
        dataset_samples = 16
        if dataset == "sharegpt":
            dataset_root = Path(_required(values, "EUBOULIA_SHAREGPT_DATASET_ROOT"))
            input_tokens = _positive_int(values, "EUBOULIA_INPUT_TOKENS")
            dataset_samples = _positive_int(
                values, "EUBOULIA_SHAREGPT_SAMPLES", default=16
            )
            dataset_path = (
                dataset_root / f"sharegpt_isl{input_tokens}_n{dataset_samples}.json"
            )
            manifest_path = Path(
                values.get("EUBOULIA_SHAREGPT_MANIFEST_PATH", str(dataset_root / "manifest.json"))
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
            seed=_nonnegative_int(
                values, "EUBOULIA_BENCHMARK_SEED", 1 if dataset == "sharegpt" else 42
            ),
            module=values.get(
                "EUBOULIA_SGLANG_BENCHMARK_MODULE",
                "sglang.bench_serving" if dataset == "sharegpt" else "sglang.benchmark.serving",
            ),
            dataset_path=dataset_path,
            dataset_manifest_path=manifest_path,
            evidence_dir=(
                Path(values["EUBOULIA_COMMAND_EVIDENCE_DIR"])
                if values.get("EUBOULIA_COMMAND_EVIDENCE_DIR")
                else None
            ),
            flush_timeout_seconds=_positive_float(
                values, "EUBOULIA_FLUSH_TIMEOUT_SECONDS", 60.0
            ),
            dataset_samples=dataset_samples,
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
        if self.dataset == "sharegpt":
            if self.dataset_path is None:
                raise BenchmarkHarnessError("a ShareGPT dataset path is required")
            if parsed.scheme != "http":
                raise BenchmarkHarnessError("the ShareGPT SGLang harness requires an HTTP endpoint")
            return (
                *common[:5],
                "--host",
                parsed.hostname,
                "--port",
                str(parsed.port or 80),
                *common[7:],
                "--dataset-path",
                str(self.dataset_path),
                "--sharegpt-output-len",
                str(self.output_tokens),
                "--sharegpt-context-len",
                str(self.input_tokens + self.output_tokens),
                "--num-prompts",
                str(self.num_prompts),
                "--max-concurrency",
                str(self.concurrency),
                "--request-rate",
                str(self.request_rate),
                "--warmup-requests",
                "0",
                "--cache-report",
                "--seed",
                str(self.seed),
                "--disable-tqdm",
                "--output-file",
                str(output_path),
                "--output-details",
            )
        return (
            *common,
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
    """Run a cache-clean workload and preserve every raw result and server snapshot."""

    execute = _run_sample if sample_runner is None else sample_runner
    measured: list[dict[str, object]] = []
    destination = _metrics_destination(settings.metrics_path)
    evidence_dir = _evidence_destination(settings.evidence_dir, destination)
    evidence_dir.mkdir(parents=True, exist_ok=False)
    if settings.dataset == "sharegpt":
        _validate_sharegpt_dataset(settings, evidence_dir)
    for index in range(settings.warmups):
        output = evidence_dir / f"warmup-{index + 1}.jsonl"
        sample = dict(execute(settings, output))
        _persist_injected_sample(output, sample)
        _validate_sample(sample, settings, f"warmup {index + 1}", require_zero_cache=False)
        if settings.dataset == "sharegpt":
            _flush_cache(settings, evidence_dir / f"flush-after-warmup-{index + 1}.txt")
    for index in range(settings.repetitions):
        round_id = index + 1
        if settings.dataset == "sharegpt":
            _flush_cache(settings, evidence_dir / f"flush-before-round-{round_id}.txt")
        output = evidence_dir / f"measured-{round_id}.jsonl"
        sample = dict(execute(settings, output))
        _persist_injected_sample(output, sample)
        _validate_sample(
            sample,
            settings,
            f"repetition {round_id}",
            require_zero_cache=settings.dataset == "sharegpt",
        )
        measured.append(sample)
        if settings.dataset == "sharegpt":
            _capture_snapshot(settings, evidence_dir, f"round-{round_id}")

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
    sample = dict(_read_last_json_object(output_path))
    if _cache_hit_rate(sample) is None:
        match = re.search(
            r"Cache hit rate:\s*([0-9.]+)%",
            log_path.read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            sample["_euboulia_cache_hit_rate_pct"] = float(match.group(1))
    return sample


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
    *,
    require_zero_cache: bool = False,
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
    if settings.dataset == "sharegpt":
        if sample.get("dataset_name") != "sharegpt":
            raise BenchmarkHarnessError(f"{label} is not a ShareGPT result")
        if sample.get("input_lens") != [settings.input_tokens] * settings.num_prompts:
            raise BenchmarkHarnessError(f"{label} does not have exact input lengths")
        if sample.get("output_lens") != [settings.output_tokens] * settings.num_prompts:
            raise BenchmarkHarnessError(f"{label} does not have exact output lengths")
        if require_zero_cache:
            cache_hit_rate = _cache_hit_rate(sample)
            if cache_hit_rate is None:
                raise BenchmarkHarnessError(f"{label} does not report cache hit rate")
            if cache_hit_rate != 0.0:
                raise BenchmarkHarnessError(
                    f"{label} cache hit rate is {cache_hit_rate:g}%, expected 0.0%"
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


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        return None


def _endpoint_request(settings: BenchmarkSettings, path: str, *, method: str = "GET") -> bytes:
    request = Request(
        settings.endpoint.rstrip("/") + path,
        method=method,
        data=b"" if method == "POST" else None,
        headers={"User-Agent": "euboulia-sglang-benchmark/1"},
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=settings.flush_timeout_seconds) as response:
            if not 200 <= response.getcode() < 300:
                raise BenchmarkHarnessError(f"{method} {path} returned HTTP {response.getcode()}")
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BenchmarkHarnessError(f"{method} {path} failed: {exc}") from exc


def _flush_cache(settings: BenchmarkSettings, evidence_path: Path) -> None:
    payload = _endpoint_request(
        settings,
        f"/flush_cache?timeout={settings.flush_timeout_seconds:g}",
        method="POST",
    )
    evidence_path.write_bytes(payload)
    if b"Cache flushed" not in payload:
        raise BenchmarkHarnessError(f"cache flush response is invalid: {evidence_path}")


def _capture_snapshot(settings: BenchmarkSettings, evidence_dir: Path, tag: str) -> None:
    (evidence_dir / f"server_info-{tag}.json").write_bytes(
        _endpoint_request(settings, "/server_info")
    )
    (evidence_dir / f"metrics-{tag}.prom").write_bytes(
        _endpoint_request(settings, "/metrics")
    )


def _cache_hit_rate(sample: Mapping[str, object]) -> float | None:
    cache = sample.get("cache_report")
    value: object = None
    if isinstance(cache, Mapping):
        value = cache.get("cache_hit_rate_pct")
    if value is None:
        value = sample.get("_euboulia_cache_hit_rate_pct")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


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


def _validate_sharegpt_dataset(settings: BenchmarkSettings, evidence_dir: Path) -> None:
    dataset_path = settings.dataset_path
    manifest_path = settings.dataset_manifest_path
    if dataset_path is None or manifest_path is None:
        raise BenchmarkHarnessError("ShareGPT dataset and manifest paths are required")
    try:
        dataset_bytes = dataset_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        dataset: object = json.loads(dataset_bytes)
        manifest: object = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkHarnessError(f"invalid ShareGPT dataset contract: {exc}") from exc
    if not isinstance(dataset, list) or len(dataset) != settings.dataset_samples:
        raise BenchmarkHarnessError(
            "ShareGPT dataset must contain exactly "
            f"{settings.dataset_samples} prompts"
        )
    if not isinstance(manifest, dict):
        raise BenchmarkHarnessError("ShareGPT manifest must be a JSON object")
    if manifest.get("seed") != settings.seed:
        raise BenchmarkHarnessError("ShareGPT manifest seed does not match the benchmark seed")
    if manifest.get("samples_per_length") != settings.dataset_samples:
        raise BenchmarkHarnessError("ShareGPT manifest sample count does not match the dataset")
    source_hash = manifest.get("source_sha256")
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise BenchmarkHarnessError("ShareGPT manifest has no valid source SHA256")
    datasets = manifest.get("datasets")
    entry = datasets.get(str(settings.input_tokens)) if isinstance(datasets, dict) else None
    if not isinstance(entry, dict):
        raise BenchmarkHarnessError(
            f"ShareGPT manifest has no ISL {settings.input_tokens} dataset entry"
        )
    expected_hash = entry.get("dataset_sha256")
    actual_hash = hashlib.sha256(dataset_bytes).hexdigest()
    if expected_hash != actual_hash:
        raise BenchmarkHarnessError("ShareGPT dataset SHA256 does not match its manifest")
    samples = entry.get("samples")
    if not isinstance(samples, list) or len(samples) != settings.dataset_samples:
        raise BenchmarkHarnessError("ShareGPT manifest does not describe every prompt")
    prompt_hashes: set[str] = set()
    for index, (row, sample) in enumerate(zip(dataset, samples, strict=True)):
        if (
            not isinstance(sample, dict)
            or sample.get("sample_index") != index
            or sample.get("input_tokens") != settings.input_tokens
        ):
            raise BenchmarkHarnessError("ShareGPT manifest contains invalid sample metadata")
        prompt = _sharegpt_prompt(row, index)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        token_hash = sample.get("prompt_token_ids_sha256")
        if sample.get("prompt_utf8_sha256") != prompt_hash or not (
            isinstance(token_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", token_hash) is not None
        ):
            raise BenchmarkHarnessError("ShareGPT prompt hashes do not match the manifest")
        if prompt_hash in prompt_hashes:
            raise BenchmarkHarnessError("ShareGPT dataset contains duplicate prompts")
        prompt_hashes.add(prompt_hash)
    (evidence_dir / "sharegpt_manifest.json").write_bytes(manifest_bytes)
    (evidence_dir / "sharegpt_dataset.sha256").write_text(
        f"{actual_hash}  {dataset_path.name}\n", encoding="utf-8"
    )


def _sharegpt_prompt(row: object, index: int) -> str:
    if not isinstance(row, Mapping):
        raise BenchmarkHarnessError(f"ShareGPT row {index} must be an object")
    conversations = row.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise BenchmarkHarnessError(f"ShareGPT row {index} has no conversation")
    first = conversations[0]
    prompt = first.get("value") if isinstance(first, Mapping) else None
    if not isinstance(prompt, str) or not prompt:
        raise BenchmarkHarnessError(f"ShareGPT row {index} has no human prompt")
    return prompt


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


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise BenchmarkHarnessError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise BenchmarkHarnessError(f"{name} must be a positive number")
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
