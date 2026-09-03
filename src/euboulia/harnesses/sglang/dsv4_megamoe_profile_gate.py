"""Non-scoring 8-rank trace gate for the DS-V4 SM90 MegaMoE kernel path."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from euboulia.harnesses.sglang.benchmark import (
    BenchmarkHarnessError,
    BenchmarkSettings,
    _flush_cache,
    _run_sample,
    _validate_sample,
    _validate_sharegpt_dataset,
)


class KernelPathGateError(RuntimeError):
    """Raised when profiling cannot prove the kernel path on all eight ranks."""


@dataclass(frozen=True, slots=True)
class KernelPathGateSettings:
    benchmark: BenchmarkSettings
    evidence_dir: Path
    target_artifact_dir: Path
    expected_ranks: int = 8
    kernel_pattern: bytes = b"fp8_mxfp4_mega_moe"
    profile_stop_timeout_seconds: float = 14_400.0

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> KernelPathGateSettings:
        values = os.environ if environ is None else environ
        evidence = Path(_required(values, "EUBOULIA_COMMAND_EVIDENCE_DIR"))
        target_artifacts = Path(_required(values, "EUBOULIA_TARGET_ARTIFACT_DIR"))
        benchmark = replace(
            BenchmarkSettings.from_environment(values),
            warmups=0,
            repetitions=1,
            evidence_dir=evidence / "benchmark",
        )
        if benchmark.dataset != "sharegpt":
            raise KernelPathGateError("kernel-path gate requires the exact ShareGPT dataset")
        return cls(
            benchmark=benchmark,
            evidence_dir=evidence,
            target_artifact_dir=target_artifacts,
            profile_stop_timeout_seconds=_positive_float(
                values, "EUBOULIA_PROFILE_STOP_TIMEOUT_SECONDS", 14_400.0
            ),
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        return None


def run_kernel_path_gate(settings: KernelPathGateSettings) -> dict[str, object]:
    evidence = settings.evidence_dir
    evidence.mkdir(parents=True, exist_ok=False)
    benchmark_evidence = evidence / "benchmark"
    benchmark_evidence.mkdir()
    _validate_sharegpt_dataset(settings.benchmark, benchmark_evidence)

    warmup_path = benchmark_evidence / "warmup.jsonl"
    warmup = _run_sample(settings.benchmark, warmup_path)
    _validate_sample(warmup, settings.benchmark, "kernel gate warmup")
    _flush_cache(settings.benchmark, evidence / "flush_before_profile.txt")

    profile_dir = evidence / "profiles"
    profile_dir.mkdir()
    profile_request = {
        "output_dir": str(profile_dir),
        "activities": ["CPU", "GPU"],
        "with_stack": False,
        "profile_by_stage": False,
        "merge_profiles": False,
        "profile_prefix": (
            f"dsv4_megamoe_isl{settings.benchmark.input_tokens}_"
            f"osl{settings.benchmark.output_tokens}"
        ),
    }
    (evidence / "profile_request.json").write_text(
        json.dumps(profile_request, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    started = False
    try:
        start_response = _post(
            settings.benchmark.endpoint + "/start_profile",
            json.dumps(profile_request).encode(),
            content_type="application/json",
            timeout=300.0,
        )
        (evidence / "start_profile_response.txt").write_bytes(start_response)
        started = True
        formal_path = benchmark_evidence / "profile-request.jsonl"
        formal = _run_sample(settings.benchmark, formal_path)
        _validate_sample(
            formal,
            settings.benchmark,
            "kernel gate profiled request",
            require_zero_cache=True,
        )
    except BaseException:
        if started:
            with suppress(BaseException):
                (evidence / "stop_profile_on_error.txt").write_bytes(
                    _post(
                        settings.benchmark.endpoint + "/stop_profile",
                        b"",
                        timeout=settings.profile_stop_timeout_seconds,
                    )
                )
        raise
    stop_response = _post(
        settings.benchmark.endpoint + "/stop_profile",
        b"",
        timeout=settings.profile_stop_timeout_seconds,
    )
    (evidence / "stop_profile_response.txt").write_bytes(stop_response)

    traces = sorted(profile_dir.glob("*.trace.json.gz"))
    if len(traces) != settings.expected_ranks:
        raise KernelPathGateError(
            f"expected {settings.expected_ranks} rank traces, observed {len(traces)}"
        )
    rank_matches: dict[int, Path] = {}
    evidence_lines: list[str] = []
    sha_lines: list[str] = []
    for trace in traces:
        match = re.search(r"TP-(\d+)", trace.name)
        if match is None:
            raise KernelPathGateError(f"trace filename has no TP rank: {trace.name}")
        rank = int(match.group(1))
        if rank in rank_matches:
            raise KernelPathGateError(f"duplicate TP rank trace: {rank}")
        try:
            contains_kernel = _gzip_contains(trace, settings.kernel_pattern)
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            raise KernelPathGateError(f"invalid trace {trace.name}: {exc}") from exc
        if not contains_kernel:
            raise KernelPathGateError(
                f"TP rank {rank} trace does not contain {settings.kernel_pattern.decode()}"
            )
        rank_matches[rank] = trace
        evidence_lines.append(
            f"PASS rank={rank} kernel={settings.kernel_pattern.decode()} trace={trace.name}"
        )
        sha_lines.append(f"{_sha256_file(trace)}  {trace.name}")
    expected = set(range(settings.expected_ranks))
    if set(rank_matches) != expected:
        raise KernelPathGateError(
            f"trace rank set is {sorted(rank_matches)}, expected {sorted(expected)}"
        )

    evidence_text = "\n".join(evidence_lines) + "\n"
    local_evidence = evidence / "megamoe_kernel_path_evidence.txt"
    local_evidence.write_text(evidence_text, encoding="utf-8")
    (evidence / "trace_sha256sum.txt").write_text(
        "\n".join(sha_lines) + "\n", encoding="utf-8"
    )
    target_raw = settings.target_artifact_dir / "raw"
    target_raw.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_evidence, target_raw / local_evidence.name)
    result = {
        "status": "passed",
        "kernel": settings.kernel_pattern.decode(),
        "rank_count": len(rank_matches),
        "ranks": sorted(rank_matches),
        "profiled_request_is_formal": False,
        "trace_dir": str(profile_dir),
    }
    (evidence / "kernel_path_gate.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def _post(
    url: str,
    data: bytes,
    *,
    timeout: float,
    content_type: str | None = None,
) -> bytes:
    headers = {"User-Agent": "euboulia-dsv4-kernel-gate/1"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(url, method="POST", data=data, headers=headers)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.getcode() < 300:
                raise KernelPathGateError(f"POST {url} returned HTTP {response.getcode()}")
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise KernelPathGateError(f"POST {url} failed: {exc}") from exc


def _gzip_contains(path: Path, pattern: bytes) -> bool:
    overlap = b""
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            combined = overlap + chunk
            if pattern in combined:
                return True
            overlap = combined[-max(0, len(pattern) - 1) :]
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise KernelPathGateError(f"{name} is required")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise KernelPathGateError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise KernelPathGateError(f"{name} must be a positive number")
    return value


def main() -> int:
    try:
        result = run_kernel_path_gate(KernelPathGateSettings.from_environment())
    except (BenchmarkHarnessError, KernelPathGateError) as exc:
        print(f"DS-V4 MegaMoE kernel-path gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
