"""Cache-clean GSM8K accuracy gate for a managed SGLang service."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class AccuracyHarnessError(RuntimeError):
    """Raised when GSM8K execution or its evidence violates the contract."""


@dataclass(frozen=True, slots=True)
class GSM8KSettings:
    endpoint: str
    evidence_dir: Path
    num_examples: int = 200
    num_shots: int = 8
    num_threads: int = 64
    temperature: int = 0
    max_tokens: int = 512
    flush_timeout_seconds: float = 60.0
    dsv4_report_dir: Path | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> GSM8KSettings:
        values = os.environ if environ is None else environ
        return cls(
            endpoint=_required(values, "EUBOULIA_TARGET_ENDPOINT").rstrip("/"),
            evidence_dir=Path(_required(values, "EUBOULIA_COMMAND_EVIDENCE_DIR")),
            num_examples=_positive_int(values, "EUBOULIA_GSM8K_NUM_EXAMPLES", 200),
            num_shots=_nonnegative_int(values, "EUBOULIA_GSM8K_NUM_SHOTS", 8),
            num_threads=_positive_int(values, "EUBOULIA_GSM8K_NUM_THREADS", 64),
            max_tokens=_positive_int(values, "EUBOULIA_GSM8K_MAX_TOKENS", 512),
            dsv4_report_dir=(
                Path(_required(values, "EUBOULIA_TARGET_ARTIFACT_DIR"))
                if values.get("EUBOULIA_DSV4_MEGAMOE_REPORT") == "1"
                else None
            ),
        )

    def command(self) -> tuple[str, ...]:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise AccuracyHarnessError("EUBOULIA_TARGET_ENDPOINT must be an HTTP base URL")
        return (
            sys.executable,
            "-m",
            "sglang.test.run_eval",
            "--eval-name",
            "gsm8k",
            "--host",
            parsed.hostname,
            "--port",
            str(parsed.port or 80),
            "--api",
            "chat",
            "--num-examples",
            str(self.num_examples),
            "--num-shots",
            str(self.num_shots),
            "--num-threads",
            str(self.num_threads),
            "--temperature",
            str(self.temperature),
            "--max-tokens",
            str(self.max_tokens),
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


def run_gsm8k(settings: GSM8KSettings) -> dict[str, object]:
    evidence = settings.evidence_dir
    evidence.mkdir(parents=True, exist_ok=False)
    flush = _request(
        settings.endpoint + f"/flush_cache?timeout={settings.flush_timeout_seconds:g}",
        method="POST",
        timeout=settings.flush_timeout_seconds,
    )
    (evidence / "flush_gsm8k.txt").write_bytes(flush)
    if b"Cache flushed" not in flush:
        raise AccuracyHarnessError("GSM8K cache flush response is invalid")

    temporary_root = Path(tempfile.gettempdir())
    before = _gsm8k_files(temporary_root)
    log_path = evidence / "gsm8k.log"
    with log_path.open("xb") as log:
        completed = subprocess.run(
            settings.command(),
            check=False,
            shell=False,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if completed.returncode != 0:
        raise AccuracyHarnessError(
            f"GSM8K exited with status {completed.returncode}; log: {log_path}"
        )
    changed = _new_or_changed_gsm8k_files(temporary_root, before)
    if len(changed) != 1:
        raise AccuracyHarnessError(
            f"expected exactly one fresh GSM8K result JSON, observed {len(changed)}"
        )
    try:
        raw: object = json.loads(changed[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccuracyHarnessError(f"invalid GSM8K result: {exc}") from exc
    if not isinstance(raw, dict):
        raise AccuracyHarnessError("GSM8K result must be a JSON object")
    score = raw.get("score")
    if (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(float(score))
    ):
        raise AccuracyHarnessError("GSM8K result does not contain a finite numeric score")
    destination = evidence / "gsm8k_result.json"
    shutil.copy2(changed[0], destination)
    result = {
        "status": "passed",
        "score": float(score),
        "num_examples": settings.num_examples,
        "num_shots": settings.num_shots,
        "num_threads": settings.num_threads,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "result_path": str(destination),
    }
    if settings.dsv4_report_dir is not None:
        from euboulia.harnesses.sglang.dsv4_megamoe_report import write_report

        target_raw = settings.dsv4_report_dir / "raw"
        target_logs = settings.dsv4_report_dir / "logs"
        target_raw.mkdir(parents=True, exist_ok=True)
        target_logs.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, target_raw / "gsm8k_result.json")
        shutil.copy2(log_path, target_logs / "gsm8k.log")
        result["scenario_report"] = write_report(
            Path.cwd(), settings.dsv4_report_dir, result
        )
    (evidence / "accuracy_gate.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def _request(url: str, *, method: str, timeout: float) -> bytes:
    request = Request(
        url,
        method=method,
        data=b"" if method == "POST" else None,
        headers={"User-Agent": "euboulia-gsm8k/1"},
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.getcode() < 300:
                raise AccuracyHarnessError(f"{method} {url} returned HTTP {response.getcode()}")
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise AccuracyHarnessError(f"{method} {url} failed: {exc}") from exc


def _gsm8k_files(root: Path) -> dict[Path, tuple[int, int]]:
    result: dict[Path, tuple[int, int]] = {}
    for path in root.glob("gsm8k_*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path] = (stat.st_mtime_ns, stat.st_size)
    return result


def _new_or_changed_gsm8k_files(
    root: Path, before: Mapping[Path, tuple[int, int]]
) -> list[Path]:
    after = _gsm8k_files(root)
    return sorted(path for path, fingerprint in after.items() if before.get(path) != fingerprint)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise AccuracyHarnessError(f"{name} is required")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    value = _nonnegative_int(environ, name, default)
    if value <= 0:
        raise AccuracyHarnessError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise AccuracyHarnessError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise AccuracyHarnessError(f"{name} must be a non-negative integer")
    return value


def main() -> int:
    try:
        result = run_gsm8k(GSM8KSettings.from_environment())
    except AccuracyHarnessError as exc:
        print(f"GSM8K accuracy gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
