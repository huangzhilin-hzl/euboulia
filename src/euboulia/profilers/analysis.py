"""Conservative rule-based analysis over normalized profiler observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .models import (
    BottleneckFinding,
    BottleneckKind,
    Hotspot,
    MetricValue,
    Observation,
    ProfileAnalysis,
    ProfileSource,
    SubjectKind,
)


@dataclass(frozen=True, slots=True)
class AnalysisThresholds:
    """Versioned heuristics; values are suspicions, never correctness claims."""

    sol_pressure_pct: float = 60.0
    sol_binding_pct: float = 80.0
    sol_dominance_gap_pct: float = 10.0
    low_occupancy_pct: float = 60.0
    transfer_ratio: float = 0.20
    sync_api_share: float = 0.10
    communication_kernel_share: float = 0.20
    short_kernel_ns: int = 20_000
    many_kernel_launches: int = 100
    low_gpu_busy_ratio: float = 0.60
    hotspot_min_share: float = 0.01

    def __post_init__(self) -> None:
        for field_name in (
            "sol_pressure_pct",
            "sol_binding_pct",
            "sol_dominance_gap_pct",
            "low_occupancy_pct",
        ):
            value = getattr(self, field_name)
            if not 0 <= value <= 100:
                raise ValueError(f"{field_name} must be in [0, 100]")
        for field_name in (
            "transfer_ratio",
            "sync_api_share",
            "communication_kernel_share",
            "low_gpu_busy_ratio",
            "hotspot_min_share",
        ):
            value = getattr(self, field_name)
            if not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be in [0, 1]")
        if self.short_kernel_ns < 0 or self.many_kernel_launches < 1:
            raise ValueError("kernel duration/count thresholds must be non-negative")


@dataclass(slots=True)
class _Aggregate:
    kind: SubjectKind
    name: str
    device: str | None
    rank: str | None
    phase: str | None
    total_duration_ns: int = 0
    count: int = 0
    observation_ids: list[str] | None = None
    metric_values: dict[str, list[MetricValue]] | None = None

    def __post_init__(self) -> None:
        self.observation_ids = []
        self.metric_values = defaultdict(list)


def analyze_observations(
    observations: Iterable[Observation],
    *,
    profile_id: str = "profile-analysis",
    thresholds: AnalysisThresholds | None = None,
    top_n: int = 30,
) -> ProfileAnalysis:
    """Aggregate hotspots and emit explainable, multi-label bottleneck hypotheses."""

    limits = thresholds or AnalysisThresholds()
    items = tuple(observations)
    if top_n < 1:
        raise ValueError("top_n must be positive")
    if not all(isinstance(item, Observation) for item in items):
        raise TypeError("observations must contain Observation objects")

    warnings = [
        "profile-derived values are diagnostic only; performance gates require "
        "an unprofiled serving rerun",
        "hotspot shares use activity-class denominators and are not application wall-time shares",
    ]
    if not items:
        finding = BottleneckFinding(
            rule_id="euboulia.insufficient.no_observations.v1",
            kind=BottleneckKind.INSUFFICIENT_EVIDENCE,
            confidence=1.0,
            evidence=("no profiler observations were supplied",),
        )
        return ProfileAnalysis(
            profile_id=profile_id,
            sources=(),
            observation_count=0,
            findings=(finding,),
            warnings=tuple(warnings),
        )

    aggregates: dict[tuple[object, ...], _Aggregate] = {}
    for observation in items:
        key = (
            observation.subject_kind,
            observation.name,
            observation.device,
            observation.rank,
            observation.phase,
        )
        aggregate = aggregates.setdefault(
            key,
            _Aggregate(
                kind=observation.subject_kind,
                name=observation.name,
                device=observation.device,
                rank=observation.rank,
                phase=observation.phase,
            ),
        )
        duration = observation.duration_ns
        if duration is None:
            duration_metric = observation.metrics.get("total_duration_ns")
            duration = round(duration_metric.value) if duration_metric is not None else 0
        aggregate.total_duration_ns += duration
        aggregate.count += observation.count if observation.count is not None else 1
        assert aggregate.observation_ids is not None
        assert aggregate.metric_values is not None
        aggregate.observation_ids.append(observation.observation_id)
        for metric_name, metric in observation.metrics.items():
            aggregate.metric_values[metric_name].append(metric)

    denominators: dict[str, int] = defaultdict(int)
    for aggregate in aggregates.values():
        denominators[_share_domain(aggregate.kind)] += aggregate.total_duration_ns
    kernel_duration = denominators.get("gpu_kernel_duration", 0)
    transfer_duration = denominators.get("gpu_memory_activity_duration", 0)
    transfer_ratio = (
        transfer_duration / (kernel_duration + transfer_duration)
        if kernel_duration + transfer_duration
        else 0.0
    )
    total_kernel_launches = sum(
        aggregate.count
        for aggregate in aggregates.values()
        if aggregate.kind is SubjectKind.GPU_KERNEL
    )

    hotspots: list[Hotspot] = []
    for index, aggregate in enumerate(
        sorted(aggregates.values(), key=lambda item: item.total_duration_ns, reverse=True)
    ):
        domain = _share_domain(aggregate.kind)
        denominator = denominators[domain]
        share = aggregate.total_duration_ns / denominator if denominator else 0.0
        if share < limits.hotspot_min_share and aggregate.total_duration_ns:
            continue
        metrics = _aggregate_metrics(aggregate)
        findings = _classify_hotspot(
            aggregate,
            metrics,
            share,
            transfer_ratio=transfer_ratio,
            total_kernel_launches=total_kernel_launches,
            thresholds=limits,
        )
        assert aggregate.observation_ids is not None
        hotspots.append(
            Hotspot(
                hotspot_id=f"hotspot:{index}",
                subject_kind=aggregate.kind,
                name=aggregate.name,
                total_duration_ns=aggregate.total_duration_ns,
                count=aggregate.count,
                share=share,
                share_basis=f"share_of_{domain}",
                observation_ids=tuple(aggregate.observation_ids),
                metrics=metrics,
                findings=findings,
                device=aggregate.device,
                rank=aggregate.rank,
                phase=aggregate.phase,
            )
        )
        if len(hotspots) >= top_n:
            break

    global_findings = list(_timeline_findings(items, limits))
    all_findings = _deduplicate_findings(
        finding for hotspot in hotspots for finding in hotspot.findings
    )
    all_findings.extend(
        finding
        for finding in global_findings
        if finding.rule_id not in {existing.rule_id for existing in all_findings}
    )
    if not all_findings:
        all_findings.append(
            BottleneckFinding(
                rule_id="euboulia.insufficient.no_rule_match.v1",
                kind=BottleneckKind.INSUFFICIENT_EVIDENCE,
                confidence=0.8,
                evidence=("available counters did not satisfy a conservative rule",),
                caveats=("collect complementary NCU counters or a timeline before tuning",),
            )
        )
    if not any(item.start_ns is not None for item in items):
        warnings.append(
            "no event timeline was present; overlap, gaps, and GPU busy ratio were not inferred"
        )
    if any(item.source is ProfileSource.NCU_CSV for item in items):
        warnings.append(
            "NCU may replay kernels; NCU duration and throughput are not serving rewards"
        )

    sources = tuple(sorted({item.source for item in items}, key=lambda item: item.value))
    return ProfileAnalysis(
        profile_id=profile_id,
        sources=sources,
        observation_count=len(items),
        hotspots=tuple(hotspots),
        findings=tuple(all_findings),
        warnings=tuple(warnings),
    )


def _share_domain(kind: SubjectKind) -> str:
    if kind is SubjectKind.GPU_KERNEL:
        return "gpu_kernel_duration"
    if kind in {SubjectKind.MEMORY_TRANSFER, SubjectKind.MEMORY_SET}:
        return "gpu_memory_activity_duration"
    if kind is SubjectKind.RUNTIME_API:
        return "runtime_api_duration"
    if kind is SubjectKind.CPU_OP:
        return "cpu_op_duration"
    if kind is SubjectKind.NVTX_RANGE:
        return "nvtx_range_duration"
    return "unknown_activity_duration"


def _aggregate_metrics(aggregate: _Aggregate) -> dict[str, MetricValue]:
    assert aggregate.metric_values is not None
    result: dict[str, MetricValue] = {}
    for name, samples in aggregate.metric_values.items():
        if not samples:
            continue
        if name == "total_duration_ns":
            value = sum(item.value for item in samples)
        else:
            value = sum(item.value for item in samples) / len(samples)
        units = {item.unit for item in samples}
        unit = samples[0].unit if len(units) == 1 else "mixed"
        native_names = {item.native_name for item in samples}
        native_name = samples[0].native_name if len(native_names) == 1 else None
        result[name] = MetricValue(value, unit, native_name)
    result["aggregated_total_duration_ns"] = MetricValue(
        aggregate.total_duration_ns, "ns", "normalized observation duration"
    )
    return result


def _metric(metrics: dict[str, MetricValue], name: str) -> float | None:
    item = metrics.get(name)
    return item.value if item is not None else None


def _classify_hotspot(
    aggregate: _Aggregate,
    metrics: dict[str, MetricValue],
    share: float,
    *,
    transfer_ratio: float,
    total_kernel_launches: int,
    thresholds: AnalysisThresholds,
) -> tuple[BottleneckFinding, ...]:
    findings: list[BottleneckFinding] = []
    sm = _metric(metrics, "sm_throughput_pct")
    memory_candidates = [
        item
        for item in (
            _metric(metrics, "memory_throughput_pct"),
            _metric(metrics, "dram_throughput_pct"),
        )
        if item is not None
    ]
    memory = max(memory_candidates) if memory_candidates else None
    occupancy = _metric(metrics, "achieved_occupancy_pct")
    required_ncu = (ProfileSource.NCU_CSV,)

    if sm is not None and memory is not None:
        if sm >= thresholds.sol_binding_pct and memory >= thresholds.sol_binding_pct:
            findings.append(
                BottleneckFinding(
                    rule_id="euboulia.sol.mixed.v1",
                    kind=BottleneckKind.MIXED,
                    confidence=0.80,
                    evidence=(f"SM throughput={sm:.1f}%", f"memory throughput={memory:.1f}%"),
                    required_sources=required_ncu,
                    caveats=("SpeedOfLight percentages are achieved pressure, not causal proof",),
                )
            )
        elif sm >= thresholds.sol_binding_pct or (
            sm >= thresholds.sol_pressure_pct and sm - memory >= thresholds.sol_dominance_gap_pct
        ):
            findings.append(
                BottleneckFinding(
                    rule_id="euboulia.sol.compute_pressure.v1",
                    kind=BottleneckKind.COMPUTE,
                    confidence=0.78 if sm >= thresholds.sol_binding_pct else 0.68,
                    evidence=(f"SM throughput={sm:.1f}%", f"memory throughput={memory:.1f}%"),
                    required_sources=required_ncu,
                    caveats=("confirm with relevant instruction-pipeline counters",),
                )
            )
        elif memory >= thresholds.sol_binding_pct or (
            memory >= thresholds.sol_pressure_pct
            and memory - sm >= thresholds.sol_dominance_gap_pct
        ):
            findings.append(
                BottleneckFinding(
                    rule_id="euboulia.sol.memory_pressure.v1",
                    kind=BottleneckKind.MEMORY,
                    confidence=0.78 if memory >= thresholds.sol_binding_pct else 0.68,
                    evidence=(f"memory throughput={memory:.1f}%", f"SM throughput={sm:.1f}%"),
                    required_sources=required_ncu,
                    caveats=(
                        "separate DRAM, L2, and shared-memory pressure before choosing a fix",
                    ),
                )
            )
        elif sm < thresholds.sol_pressure_pct and memory < thresholds.sol_pressure_pct:
            findings.append(
                BottleneckFinding(
                    rule_id="euboulia.sol.underutilized.v1",
                    kind=BottleneckKind.UNDERUTILIZED,
                    confidence=0.55,
                    evidence=(f"SM throughput={sm:.1f}%", f"memory throughput={memory:.1f}%"),
                    required_sources=required_ncu,
                    caveats=(
                        "low pressure is non-specific; inspect latency, dependencies, "
                        "and launch gaps",
                    ),
                )
            )
    elif sm is not None and sm >= thresholds.sol_binding_pct:
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.sol.compute_pressure.v1",
                kind=BottleneckKind.COMPUTE,
                confidence=0.65,
                evidence=(f"SM throughput={sm:.1f}%", "memory throughput counter unavailable"),
                required_sources=required_ncu,
                caveats=("collect the memory SpeedOfLight counter before assigning causality",),
            )
        )
    elif memory is not None and memory >= thresholds.sol_binding_pct:
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.sol.memory_pressure.v1",
                kind=BottleneckKind.MEMORY,
                confidence=0.65,
                evidence=(f"memory throughput={memory:.1f}%", "SM throughput counter unavailable"),
                required_sources=required_ncu,
                caveats=("collect the SM SpeedOfLight counter before assigning causality",),
            )
        )

    if (
        occupancy is not None
        and occupancy < thresholds.low_occupancy_pct
        and (sm is None or sm < thresholds.sol_pressure_pct)
        and (memory is None or memory < thresholds.sol_pressure_pct)
    ):
        limits = [
            name
            for name in (
                "occupancy_limit_registers",
                "occupancy_limit_shared_memory",
                "occupancy_limit_warps",
            )
            if name in metrics
        ]
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.occupancy.low.v1",
                kind=BottleneckKind.OCCUPANCY,
                confidence=0.63,
                evidence=(
                    f"achieved occupancy={occupancy:.1f}%",
                    "reported launch limits=" + (", ".join(limits) if limits else "none"),
                ),
                required_sources=required_ncu,
                caveats=("higher occupancy does not necessarily improve performance",),
            )
        )

    lowered = aggregate.name.lower()
    if (
        aggregate.kind is SubjectKind.RUNTIME_API
        and any(token in lowered for token in ("synchronize", "wait", "eventquery"))
        and share >= thresholds.sync_api_share
    ):
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.runtime.sync_share.v1",
                kind=BottleneckKind.SYNC,
                confidence=0.62,
                evidence=(f"{aggregate.name} is {share:.1%} of observed runtime API duration",),
                caveats=("runtime API share is not application wall-time share",),
            )
        )
    if (
        aggregate.kind is SubjectKind.GPU_KERNEL
        and "nccl" in lowered
        and share >= thresholds.communication_kernel_share
    ):
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.kernel.communication_share.v1",
                kind=BottleneckKind.COMMUNICATION,
                confidence=0.65,
                evidence=(f"NCCL kernel is {share:.1%} of observed GPU kernel duration",),
                caveats=("a summary cannot determine compute/communication overlap",),
            )
        )
    if (
        aggregate.kind in {SubjectKind.MEMORY_TRANSFER, SubjectKind.MEMORY_SET}
        and transfer_ratio >= thresholds.transfer_ratio
    ):
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.gpu.transfer_ratio.v1",
                kind=BottleneckKind.TRANSFER,
                confidence=0.60,
                evidence=(
                    f"memory activities are {transfer_ratio:.1%} of observed "
                    "kernel+memory duration",
                ),
                caveats=("summed durations may overlap and therefore are not wall time",),
            )
        )
    average_duration = aggregate.total_duration_ns / aggregate.count if aggregate.count else 0.0
    if (
        aggregate.kind is SubjectKind.GPU_KERNEL
        and total_kernel_launches >= thresholds.many_kernel_launches
        and average_duration <= thresholds.short_kernel_ns
    ):
        findings.append(
            BottleneckFinding(
                rule_id="euboulia.kernel.many_short_launches.v1",
                kind=BottleneckKind.LAUNCH,
                confidence=0.52,
                evidence=(
                    f"{total_kernel_launches} observed kernel launches",
                    f"{aggregate.name} average duration={average_duration:.0f} ns",
                ),
                caveats=("confirm launch gaps on a timeline before attributing overhead",),
            )
        )
    return tuple(findings)


def _timeline_findings(
    observations: tuple[Observation, ...], thresholds: AnalysisThresholds
) -> tuple[BottleneckFinding, ...]:
    intervals = [
        (item.start_ns, item.start_ns + item.duration_ns)
        for item in observations
        if item.start_ns is not None and item.duration_ns is not None
    ]
    gpu_intervals = [
        (item.start_ns, item.start_ns + item.duration_ns)
        for item in observations
        if item.start_ns is not None
        and item.duration_ns is not None
        and item.subject_kind
        in {SubjectKind.GPU_KERNEL, SubjectKind.MEMORY_TRANSFER, SubjectKind.MEMORY_SET}
    ]
    if not intervals or not gpu_intervals:
        return ()
    capture_start = min(start for start, _ in intervals)
    capture_end = max(end for _, end in intervals)
    capture_duration = capture_end - capture_start
    if capture_duration <= 0:
        return ()
    busy_ratio = _interval_union_duration(gpu_intervals) / capture_duration
    runtime_count = sum(
        item.count or 1 for item in observations if item.subject_kind is SubjectKind.RUNTIME_API
    )
    if busy_ratio >= thresholds.low_gpu_busy_ratio or runtime_count == 0:
        return ()
    return (
        BottleneckFinding(
            rule_id="euboulia.timeline.cpu_submission_suspected.v1",
            kind=BottleneckKind.CPU_SUBMISSION,
            confidence=0.56,
            evidence=(
                f"GPU activity union is {busy_ratio:.1%} of captured event span",
                f"observed runtime API calls={runtime_count}",
            ),
            required_sources=(ProfileSource.TORCH_CHROME_TRACE, ProfileSource.NSYS_STATS_CSV),
            caveats=("capture boundaries and missing processes can create apparent GPU gaps",),
        ),
    )


def _interval_union_duration(intervals: list[tuple[int, int]]) -> int:
    total = 0
    current_start: int | None = None
    current_end: int | None = None
    for start, end in sorted(intervals):
        if current_start is None:
            current_start, current_end = start, end
            continue
        assert current_end is not None
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None and current_end is not None:
        total += current_end - current_start
    return total


def _deduplicate_findings(
    findings: Iterable[BottleneckFinding],
) -> list[BottleneckFinding]:
    result: list[BottleneckFinding] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.rule_id not in seen:
            result.append(finding)
            seen.add(finding.rule_id)
    return result


__all__ = ["AnalysisThresholds", "analyze_observations"]
