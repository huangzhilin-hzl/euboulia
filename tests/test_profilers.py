from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from euboulia.profilers import (
    AnalysisThresholds,
    BottleneckKind,
    MeasurementLane,
    MetricValue,
    Observation,
    ProfileAnalysis,
    ProfileParseError,
    ProfileSource,
    SubjectKind,
    analyze_observations,
    parse_ncu_csv,
    parse_nsys_stats_csv,
    parse_torch_chrome_trace,
    require_gate_eligible_lane,
)


def test_profiler_models_are_diagnostic_only_and_round_trip() -> None:
    observation = Observation(
        observation_id="trace:1",
        source=ProfileSource.TORCH_CHROME_TRACE,
        subject_kind=SubjectKind.GPU_KERNEL,
        name="gemm",
        artifact="trace.json.gz",
        start_ns=1_000,
        duration_ns=2_000,
        count=1,
    )

    restored = Observation.from_json(observation.to_json())

    assert restored == observation
    assert not restored.gate_eligible
    with pytest.raises(ValueError, match="profile_diagnostic"):
        Observation(
            observation_id="bad",
            source=ProfileSource.NCU_CSV,
            subject_kind=SubjectKind.GPU_KERNEL,
            name="bad",
            artifact="bad.csv",
            measurement_lane=MeasurementLane.SERVING_UNPROFILED,
        )
    with pytest.raises(ValueError, match="not reward metrics"):
        require_gate_eligible_lane(MeasurementLane.PROFILE_DIAGNOSTIC)
    require_gate_eligible_lane(MeasurementLane.SERVING_UNPROFILED)


@pytest.mark.parametrize("compressed", [False, True])
def test_torch_chrome_trace_parses_complete_events_in_microseconds(
    tmp_path: Path, compressed: bool
) -> None:
    payload = {
        "schemaVersion": 1,
        "displayTimeUnit": "ms",
        "traceEvents": [
            {
                "ph": "X",
                "cat": "cpu_op",
                "name": "aten::linear",
                "ts": 1.5,
                "dur": 2.25,
                "pid": 7,
                "tid": 8,
            },
            {
                "ph": "X",
                "cat": "kernel",
                "name": "ampere_sgemm",
                "ts": 4,
                "dur": 5,
                "args": {"Device": 0, "stream": 3, "External id": 91},
            },
            {"ph": "X", "cat": "cuda_runtime", "name": "cudaLaunchKernel", "ts": 9, "dur": 1},
            {"ph": "B", "cat": "cpu_op", "name": "ignored", "ts": 1},
        ],
    }
    path = tmp_path / ("trace.json.gz" if compressed else "trace.json")
    if compressed:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    observations = parse_torch_chrome_trace(path)

    assert len(observations) == 3
    assert observations[0].start_ns == 1_500
    assert observations[0].duration_ns == 2_250
    assert observations[1].subject_kind is SubjectKind.GPU_KERNEL
    assert observations[1].device == "0"
    assert observations[1].correlation_ids == {"external id": "91"}
    assert observations[2].subject_kind is SubjectKind.RUNTIME_API


def test_nsys_summary_parser_uses_semantic_headers_and_units(tmp_path: Path) -> None:
    path = tmp_path / "kernels.csv"
    path.write_text(
        "Generating report...\n"
        '"Name","Avg (us)","Instances","Time (%)","Total Time (us)"\n'
        '"fused_attention",25,4,80,100\n'
        '"small_kernel",5,4,20,20\n',
        encoding="utf-8",
    )

    observations = parse_nsys_stats_csv(path, report="cuda_gpu_kern_sum")

    assert [item.name for item in observations] == ["fused_attention", "small_kernel"]
    assert observations[0].duration_ns == 100_000
    assert observations[0].count == 4
    assert observations[0].metrics["average_duration_ns"].value == 25_000
    assert observations[0].metrics["report_share_pct"].value == 80
    assert "not wall time" in observations[0].warnings[0]


def test_nsys_trace_parser_preserves_timeline_scope(tmp_path: Path) -> None:
    path = tmp_path / "trace.csv"
    path.write_text(
        '"Duration (ns)","Name","Start (ns)","Stream","Device","CorrId"\n'
        '5000,"kernel_a",1000,7,0,42\n',
        encoding="utf-8",
    )

    (observation,) = parse_nsys_stats_csv(path, report="cuda_gpu_trace")

    assert observation.start_ns == 1_000
    assert observation.duration_ns == 5_000
    assert observation.stream == "7"
    assert observation.correlation_ids == {"correlation_id": "42"}


def test_ncu_raw_csv_groups_metrics_per_launch_and_preserves_unknowns(tmp_path: Path) -> None:
    path = tmp_path / "profile.raw.csv"
    path.write_text(
        "==PROF== Connected\n"
        '"ID","Kernel Name","Stream","Metric Name","Metric Unit","Metric Value"\n'
        '1,"gemm_kernel",7,"gpu__time_duration.sum","nsecond",20000\n'
        ',,,"sm__throughput.avg.pct_of_peak_sustained_elapsed","percent",90\n'
        ',,,"gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed","percent",35\n'
        ',,,"custom__counter.sum","count",123\n'
        '2,"copy_kernel",8,"gpu__time_duration.sum","usecond",5\n'
        ',,,"sm__throughput.avg.pct_of_peak_sustained_elapsed","percent",20\n'
        ',,,"not_available.metric","",n/a\n',
        encoding="utf-8",
    )

    observations = parse_ncu_csv(path)

    assert len(observations) == 2
    assert observations[0].name == "gemm_kernel"
    assert observations[0].duration_ns == 20_000
    assert observations[0].metrics["sm_throughput_pct"].value == 90
    assert observations[0].metrics["custom__counter.sum"].native_name == "custom__counter.sum"
    assert observations[1].duration_ns == 5_000
    assert observations[1].warnings == ("skipped non-numeric metric 'not_available.metric'",)


def test_rule_analyzer_classifies_pressure_and_keeps_reward_isolated(tmp_path: Path) -> None:
    path = tmp_path / "profile.raw.csv"
    path.write_text(
        '"ID","Kernel Name","Metric Name","Metric Unit","Metric Value"\n'
        '1,"gemm","gpu__time_duration.sum","nsecond",20000\n'
        ',,"sm__throughput.avg.pct_of_peak_sustained_elapsed","percent",92\n'
        ',,"gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed","percent",40\n',
        encoding="utf-8",
    )

    analysis = analyze_observations(parse_ncu_csv(path), profile_id="candidate-a/profile-1")
    restored = ProfileAnalysis.from_json(analysis.to_json())

    assert restored == analysis
    assert not analysis.gate_eligible
    assert analysis.measurement_lane is MeasurementLane.PROFILE_DIAGNOSTIC
    assert analysis.hotspots[0].share_basis == "share_of_gpu_kernel_duration"
    assert BottleneckKind.COMPUTE in {finding.kind for finding in analysis.findings}
    assert any("unprofiled serving" in warning for warning in analysis.warnings)
    assert any("replay" in warning for warning in analysis.warnings)


def test_analyzer_flags_sync_share_and_many_short_launches() -> None:
    observations = [
        Observation(
            observation_id="api:sync",
            source=ProfileSource.NSYS_STATS_CSV,
            subject_kind=SubjectKind.RUNTIME_API,
            name="cudaDeviceSynchronize",
            artifact="api.csv",
            duration_ns=80,
            count=20,
        ),
        Observation(
            observation_id="api:other",
            source=ProfileSource.NSYS_STATS_CSV,
            subject_kind=SubjectKind.RUNTIME_API,
            name="cudaLaunchKernel",
            artifact="api.csv",
            duration_ns=20,
            count=100,
        ),
        Observation(
            observation_id="kernel:short",
            source=ProfileSource.NSYS_STATS_CSV,
            subject_kind=SubjectKind.GPU_KERNEL,
            name="tiny_kernel",
            artifact="kernels.csv",
            duration_ns=1_000_000,
            count=200,
        ),
    ]

    analysis = analyze_observations(
        observations,
        thresholds=AnalysisThresholds(many_kernel_launches=100, short_kernel_ns=10_000),
    )

    kinds = {finding.kind for finding in analysis.findings}
    assert BottleneckKind.SYNC in kinds
    assert BottleneckKind.LAUNCH in kinds


def test_analyzer_can_flag_a_binding_counter_when_its_counterpart_is_missing() -> None:
    observation = Observation(
        observation_id="ncu:1",
        source=ProfileSource.NCU_CSV,
        subject_kind=SubjectKind.GPU_KERNEL,
        name="gemm",
        artifact="ncu.csv",
        duration_ns=1_000,
        metrics={"sm_throughput_pct": MetricValue(90, "%")},
    )

    analysis = analyze_observations((observation,))

    assert BottleneckKind.COMPUTE in {finding.kind for finding in analysis.findings}


def test_parsers_fail_closed_on_missing_structural_headers(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")

    with pytest.raises(ProfileParseError, match="header"):
        parse_nsys_stats_csv(path, report="cuda_gpu_kern_sum")
    with pytest.raises(ProfileParseError, match="Metric Name"):
        parse_ncu_csv(path)
