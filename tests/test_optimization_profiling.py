import gzip
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from euboulia.execution import ExecutionResult
from euboulia.optimization.config import ProfileProvider, SGLangProfilingConfig
from euboulia.optimization.contracts import Capability, ProfileRequest, StageContext
from euboulia.optimization.profiling import ProfileCaptureError, RuleAnalyzer, SGLangProfiler


class _LocalProfiler(SGLangProfiler):
    raw_dir: Path | None = None
    start_payload: dict[str, object] | None = None

    def _post_json(self, endpoint: str, path: str, payload: dict[str, object]) -> None:
        del endpoint
        if path == "/start_profile":
            self.start_payload = payload
            self.raw_dir = Path(str(payload["output_dir"]))


class _AutoStoppingProfiler(_LocalProfiler):
    stop_calls = 0

    def _post_json(self, endpoint: str, path: str, payload: dict[str, object]) -> None:
        if path == "/stop_profile":
            self.stop_calls += 1
            raise ProfileCaptureError("HTTP 500: Profiling is not in progress")
        super()._post_json(endpoint, path, payload)


def _trace(path: Path, *, kernel: str = "fp8_mxfp4_mega_moe") -> None:
    events = [
        {
            "name": kernel,
            "cat": "kernel",
            "ph": "X",
            "ts": index * 10,
            "dur": 5,
            "pid": 1,
            "tid": 2,
        }
        for index in range(110)
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump({"traceEvents": events}, handle)


def _execution(tmp_path: Path) -> ExecutionResult:
    stdout = tmp_path / "workload.stdout.log"
    stderr = tmp_path / "workload.stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ExecutionResult(
        command_id="profile-workload",
        argv=("benchmark",),
        cwd=tmp_path,
        returncode=0,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        stdout_path=stdout,
        stderr_path=stderr,
        environment_keys=(),
    )


def _context(tmp_path: Path) -> StageContext:
    return StageContext(
        run_uid="run",
        iteration_id="iteration",
        artifact_dir=tmp_path,
        authorizations=frozenset(
            {Capability.PROFILE_EXECUTION, Capability.BENCHMARK_EXECUTION}
        ),
    )


def _config(**overrides: object) -> SGLangProfilingConfig:
    values = {
        "provider": ProfileProvider.SGLANG_TORCH,
        "workload_point": "profile-point",
        "warmup_runs": 0,
        "settle_timeout_seconds": 2,
        "min_free_disk_bytes": 1_000_000,
        "max_raw_bytes": 1_000_000,
        "max_summary_rows": 100,
        "expected_rank_traces": 2,
        "required_kernel_pattern": "fp8_mxfp4_mega_moe",
    }
    values.update(overrides)
    return SGLangProfilingConfig(**values)  # type: ignore[arg-type]


def test_active_profiler_streams_summary_validates_ranks_and_evicts_raw(
    tmp_path: Path,
) -> None:
    profiler = _AutoStoppingProfiler(_config())

    def workload() -> ExecutionResult:
        assert profiler.raw_dir is not None
        _trace(profiler.raw_dir / "rank-0.trace.json.gz")
        _trace(profiler.raw_dir / "rank-1.trace.json.gz")
        return _execution(tmp_path)

    profile = profiler.capture(
        ProfileRequest("champion", "abc", "workload", max_bytes=1_000_000),
        _context(tmp_path),
        endpoint="http://127.0.0.1:30000",
        run_workload=workload,
    )
    report = RuleAnalyzer(profiler).analyze(profile, (), _context(tmp_path))

    assert profile.provider == "sglang_torch"
    assert profiler.stop_calls == 0
    assert profiler.start_payload is not None
    assert profiler.start_payload["num_steps"] == 3
    assert profiler.start_payload["with_stack"] is False
    assert profiler.start_payload["record_shapes"] is False
    assert profile.metrics["raw_observation_count"] == 220
    assert profile.metrics["summary_row_count"] == 2
    assert profile.metadata["raw_retained"] is False
    assert not tuple((tmp_path / "profile/raw").glob("*.trace.json.gz"))
    assert (tmp_path / "profile/summary.json").is_file()
    assert (tmp_path / "profile/manifest.json").is_file()
    assert any(finding.category == "launch" for finding in report.findings)


def test_cleanup_stop_failure_does_not_mask_failed_workload(tmp_path: Path) -> None:
    profiler = _AutoStoppingProfiler(_config())
    with pytest.raises(ProfileCaptureError, match="profile workload failed"):
        profiler.capture(
            ProfileRequest("champion", "abc", "workload", max_bytes=1_000_000),
            _context(tmp_path),
            endpoint="http://127.0.0.1:30000",
            run_workload=lambda: replace(_execution(tmp_path), returncode=7),
        )
    assert profiler.stop_calls == 1


def test_missing_auto_stop_traces_fail_and_attempt_cleanup(tmp_path: Path) -> None:
    profiler = _AutoStoppingProfiler(_config(settle_timeout_seconds=0.01))
    with pytest.raises(ProfileCaptureError, match="produced no trace"):
        profiler.capture(
            ProfileRequest("champion", "abc", "workload", max_bytes=1_000_000),
            _context(tmp_path),
            endpoint="http://127.0.0.1:30000",
            run_workload=lambda: _execution(tmp_path),
        )
    assert profiler.stop_calls == 1


def test_profile_control_error_includes_bounded_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise HTTPError(
            "http://localhost/start_profile", 500, "Internal Server Error", {},
            io.BytesIO(b"Profiling already active " + b"x" * 5000),
        )

    monkeypatch.setattr(
        "euboulia.optimization.profiling.build_opener",
        lambda *args: SimpleNamespace(open=fail),
    )
    with pytest.raises(ProfileCaptureError, match="Profiling already active") as raised:
        SGLangProfiler(_config())._post_json("http://localhost", "/start_profile", {})
    assert len(str(raised.value)) < 4300


def test_active_profiler_keeps_failed_capture_for_diagnosis(tmp_path: Path) -> None:
    profiler = _LocalProfiler(_config(max_raw_bytes=1))

    def workload() -> ExecutionResult:
        assert profiler.raw_dir is not None
        _trace(profiler.raw_dir / "rank-0.trace.json.gz")
        _trace(profiler.raw_dir / "rank-1.trace.json.gz")
        return _execution(tmp_path)

    with pytest.raises(ProfileCaptureError, match="raw byte budget"):
        profiler.capture(
            ProfileRequest("champion", "abc", "workload", max_bytes=1),
            _context(tmp_path),
            endpoint="http://127.0.0.1:30000",
            run_workload=workload,
        )

    assert len(tuple((tmp_path / "profile/raw").glob("*.trace.json.gz"))) == 2


def test_active_profiler_rejects_traces_without_complete_events(tmp_path: Path) -> None:
    profiler = _LocalProfiler(
        _config(expected_rank_traces=1, required_kernel_pattern=None)
    )

    def workload() -> ExecutionResult:
        assert profiler.raw_dir is not None
        with gzip.open(
            profiler.raw_dir / "rank-0.trace.json.gz", "wt", encoding="utf-8"
        ) as handle:
            json.dump({"traceEvents": [{"name": "metadata", "ph": "M"}]}, handle)
        return _execution(tmp_path)

    with pytest.raises(ProfileCaptureError, match="no complete-duration"):
        profiler.capture(
            ProfileRequest("champion", "abc", "workload", max_bytes=1_000_000),
            _context(tmp_path),
            endpoint="http://127.0.0.1:30000",
            run_workload=workload,
        )

    assert (tmp_path / "profile/raw/rank-0.trace.json.gz").is_file()


def test_active_profiler_requires_an_exact_rank_set(tmp_path: Path) -> None:
    profiler = _LocalProfiler(_config())

    def workload() -> ExecutionResult:
        assert profiler.raw_dir is not None
        _trace(profiler.raw_dir / "TP-0-a.trace.json.gz")
        _trace(profiler.raw_dir / "TP-0-b.trace.json.gz")
        return _execution(tmp_path)

    with pytest.raises(ProfileCaptureError, match="duplicate rank"):
        profiler.capture(
            ProfileRequest("champion", "abc", "workload", max_bytes=1_000_000),
            _context(tmp_path),
            endpoint="http://127.0.0.1:30000",
            run_workload=workload,
        )
