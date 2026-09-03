"""Profiler normalization and conservative bottleneck analysis."""

from .analysis import AnalysisThresholds, analyze_observations
from .models import (
    BottleneckFinding,
    BottleneckKind,
    Hotspot,
    MeasurementLane,
    MetricValue,
    Observation,
    ProfileAnalysis,
    ProfileSource,
    SubjectKind,
    require_gate_eligible_lane,
)
from .parsers import (
    ProfileParseError,
    iter_torch_chrome_trace,
    parse_ncu_csv,
    parse_nsys_stats_csv,
    parse_profile,
    parse_torch_chrome_trace,
)

__all__ = [
    "AnalysisThresholds",
    "BottleneckFinding",
    "BottleneckKind",
    "Hotspot",
    "MeasurementLane",
    "MetricValue",
    "Observation",
    "ProfileAnalysis",
    "ProfileParseError",
    "ProfileSource",
    "SubjectKind",
    "analyze_observations",
    "iter_torch_chrome_trace",
    "parse_ncu_csv",
    "parse_nsys_stats_csv",
    "parse_profile",
    "parse_torch_chrome_trace",
    "require_gate_eligible_lane",
]
