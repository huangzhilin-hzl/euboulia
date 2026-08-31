import json
from pathlib import Path

from euboulia.optimization.config import ProfileArtifactConfig
from euboulia.optimization.contracts import ProfileRequest, StageContext
from euboulia.optimization.profiling import ImportedProfiler, RuleAnalyzer


def _trace(path: Path) -> None:
    events = [
        {
            "name": "decode_kernel",
            "cat": "kernel",
            "ph": "X",
            "ts": index * 10,
            "dur": 5,
            "pid": 1,
            "tid": 2,
        }
        for index in range(110)
    ]
    path.write_text(json.dumps({"traceEvents": events}), encoding="utf-8")


def test_imported_profiler_and_rule_analyzer_bridge_contracts(tmp_path: Path) -> None:
    path = tmp_path / "trace.json"
    _trace(path)
    profiler = ImportedProfiler((ProfileArtifactConfig(path=path, source="torch_chrome_trace"),))
    context = StageContext(run_id="run", iteration_id="iteration", artifact_dir=tmp_path)
    profile = profiler.capture(
        ProfileRequest(
            candidate_id="champion",
            source_revision="abc",
            workload_digest="workload",
            max_bytes=1_000_000,
        ),
        context,
    )

    report = RuleAnalyzer(profiler).analyze(profile, (), context)

    assert profile.metadata["gate_eligible"] is False
    assert profile.metrics["observation_count"] == 110
    assert any(finding.category == "launch" for finding in report.findings)
    assert report.metadata["measurement_lane"] == "profile_diagnostic"


def test_imported_profiler_enforces_total_byte_budget(tmp_path: Path) -> None:
    path = tmp_path / "trace.json"
    _trace(path)
    profiler = ImportedProfiler((ProfileArtifactConfig(path=path, source="torch_chrome_trace"),))

    try:
        profiler.capture(
            ProfileRequest("candidate", "abc", "workload", max_bytes=1),
            StageContext("run", "iteration", tmp_path),
        )
    except ValueError as exc:
        assert "byte budget" in str(exc)
    else:  # pragma: no cover - makes a missing exception explicit
        raise AssertionError("expected imported profile byte budget failure")
