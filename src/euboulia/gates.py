"""Fail-closed correctness and performance gates."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from numbers import Real
from typing import TypeAlias

from .models import JSONValue, MetricDirection, Metrics, Verdict, VerdictStatus

MetricSource: TypeAlias = Metrics | Mapping[str, int | float]
MetricInput: TypeAlias = MetricSource | int | float


def _direction(value: MetricDirection | str) -> MetricDirection:
    if isinstance(value, MetricDirection):
        return value
    try:
        return MetricDirection(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("direction must be MetricDirection.MINIMIZE or MAXIMIZE") from exc


def _extract_metric(source: MetricInput, metric: str) -> tuple[float | None, str | None]:
    if isinstance(source, Metrics):
        raw = source.values.get(metric)
    elif isinstance(source, Mapping):
        raw = source.get(metric)
    else:
        raw = source
    if raw is None:
        return None, f"metric {metric!r} is missing"
    if isinstance(raw, bool) or not isinstance(raw, Real):
        return None, f"metric {metric!r} is not numeric"
    value = float(raw)
    if not math.isfinite(value):
        return None, f"metric {metric!r} is not finite"
    return value, None


@dataclass(frozen=True, slots=True)
class CorrectnessGate:
    """Require a boolean correctness flag or a thresholded scalar metric.

    With the default ``metric=None``, the gate reads
    ``Metrics.correctness_passed`` and then falls back to common scalar keys
    (``correctness_passed`` and ``correct``). An explicit metric can express
    checks such as ``correct >= 1`` or ``failed <= 0``.
    """

    metric: str | None = None
    direction: MetricDirection = MetricDirection.MAXIMIZE
    threshold: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", _direction(self.direction))
        if self.metric is not None and (not isinstance(self.metric, str) or not self.metric):
            raise ValueError("metric must be a non-empty string or None")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, Real):
            raise TypeError("threshold must be numeric")
        if not math.isfinite(float(self.threshold)):
            raise ValueError("threshold must be finite")
        object.__setattr__(self, "threshold", float(self.threshold))

    def evaluate(self, metrics: Metrics | Mapping[str, object] | bool) -> Verdict:
        metric_name = self.metric
        raw: object | None
        if isinstance(metrics, bool):
            raw = metrics
        elif metric_name is None and isinstance(metrics, Metrics):
            raw = metrics.correctness_passed
            if raw is None:
                raw = metrics.values.get("correctness_passed")
            if raw is None:
                raw = metrics.values.get("correct")
        elif isinstance(metrics, Metrics):
            raw = metrics.values.get(metric_name)  # type: ignore[arg-type]
        elif metric_name is None:
            raw = metrics.get("correctness_passed")
            if raw is None:
                raw = metrics.get("correct")
        else:
            raw = metrics.get(metric_name)

        details: dict[str, JSONValue] = {
            "gate": "correctness",
            "metric": metric_name,
            "threshold": self.threshold,
            "direction": self.direction.value,
        }
        if raw is None:
            return Verdict(
                status=VerdictStatus.REJECT,
                reasons=("correctness result is missing",),
                correctness_passed=False,
                details=details,
            )

        if isinstance(raw, bool):
            passed = raw
            details["observed"] = raw
        elif isinstance(raw, Real):
            observed = float(raw)
            if not math.isfinite(observed):
                return Verdict(
                    status=VerdictStatus.REJECT,
                    reasons=("correctness result is not finite",),
                    correctness_passed=False,
                    details=details,
                )
            details["observed"] = observed
            passed = (
                observed <= self.threshold
                if self.direction is MetricDirection.MINIMIZE
                else observed >= self.threshold
            )
        else:
            return Verdict(
                status=VerdictStatus.REJECT,
                reasons=("correctness result is neither boolean nor numeric",),
                correctness_passed=False,
                details=details,
            )

        return Verdict(
            status=VerdictStatus.ACCEPT if passed else VerdictStatus.REJECT,
            reasons=("correctness passed" if passed else "correctness failed",),
            correctness_passed=passed,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class PerformanceGate:
    """Compare one scalar metric between a baseline and a candidate.

    ``relative_improvement`` is positive when the candidate is better:

    * minimize: ``(baseline - candidate) / abs(baseline)``
    * maximize: ``(candidate - baseline) / abs(baseline)``

    When ``min_relative_improvement`` is positive it is a strict promotion
    target. Otherwise, a regression no larger than
    ``allowed_relative_regression`` is accepted. Changes within
    ``noise_tolerance`` are treated as zero. All thresholds are fractions, so
    ``0.01`` means one percent.
    """

    metric: str
    direction: MetricDirection
    min_relative_improvement: float = 0.0
    allowed_relative_regression: float = 0.0
    noise_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric.strip():
            raise ValueError("metric must be a non-empty string")
        object.__setattr__(self, "metric", self.metric.strip())
        object.__setattr__(self, "direction", _direction(self.direction))
        for field_name in (
            "min_relative_improvement",
            "allowed_relative_regression",
            "noise_tolerance",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f"{field_name} must be numeric")
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
            object.__setattr__(self, field_name, value)

    def evaluate(self, baseline: MetricInput, candidate: MetricInput) -> Verdict:
        baseline_value, baseline_error = _extract_metric(baseline, self.metric)
        candidate_value, candidate_error = _extract_metric(candidate, self.metric)
        errors = tuple(error for error in (baseline_error, candidate_error) if error)
        if errors:
            return Verdict(
                status=VerdictStatus.REJECT,
                reasons=errors,
                performance_passed=False,
                metric=self.metric,
                direction=self.direction,
                baseline_value=baseline_value,
                candidate_value=candidate_value,
                allowed_relative_regression=self.allowed_relative_regression,
                min_relative_improvement=self.min_relative_improvement,
                noise_tolerance=self.noise_tolerance,
                details={"gate": "performance", "fail_closed": True},
            )

        assert baseline_value is not None and candidate_value is not None
        signed_delta = (
            baseline_value - candidate_value
            if self.direction is MetricDirection.MINIMIZE
            else candidate_value - baseline_value
        )

        relative_improvement: float | None
        if baseline_value == 0.0:
            relative_improvement = 0.0 if candidate_value == 0.0 else None
            if signed_delta > 0:
                passed = True
                reason = "candidate is directionally better than a zero baseline"
            elif signed_delta < 0:
                passed = False
                reason = "candidate is directionally worse than a zero baseline"
            else:
                passed = self.min_relative_improvement == 0.0
                reason = (
                    "candidate equals zero baseline"
                    if passed
                    else "candidate does not meet required relative improvement"
                )
        else:
            relative_improvement = signed_delta / abs(baseline_value)
            effective_improvement = (
                0.0 if abs(relative_improvement) <= self.noise_tolerance else relative_improvement
            )
            acceptance_floor = (
                self.min_relative_improvement
                if self.min_relative_improvement > 0.0
                else -self.allowed_relative_regression
            )
            passed = effective_improvement >= acceptance_floor or math.isclose(
                effective_improvement,
                acceptance_floor,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            if passed and effective_improvement == 0.0 and relative_improvement != 0.0:
                reason = "performance change is within the noise tolerance"
            elif passed:
                reason = "performance threshold passed"
            elif relative_improvement < 0:
                reason = "performance regression exceeds the allowed threshold"
            else:
                reason = "candidate does not meet required relative improvement"

        return Verdict(
            status=VerdictStatus.ACCEPT if passed else VerdictStatus.REJECT,
            reasons=(reason,),
            performance_passed=passed,
            metric=self.metric,
            direction=self.direction,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            relative_improvement=relative_improvement,
            allowed_relative_regression=self.allowed_relative_regression,
            min_relative_improvement=self.min_relative_improvement,
            noise_tolerance=self.noise_tolerance,
            details={
                "gate": "performance",
                "signed_delta": signed_delta,
                "zero_baseline": baseline_value == 0.0,
            },
        )


@dataclass(frozen=True, slots=True)
class CompositeGate:
    """Run correctness first, then performance, rejecting on any uncertainty."""

    performance: PerformanceGate
    correctness: CorrectnessGate = field(default_factory=CorrectnessGate)

    def evaluate(self, baseline: Metrics, candidate: Metrics) -> Verdict:
        if not isinstance(baseline, Metrics) or not isinstance(candidate, Metrics):
            raise TypeError("CompositeGate requires Metrics baseline and candidate")

        correctness_verdict = self.correctness.evaluate(candidate)
        if not correctness_verdict.accepted:
            return Verdict(
                status=VerdictStatus.REJECT,
                reasons=correctness_verdict.reasons,
                correctness_passed=False,
                performance_passed=None,
                metric=self.performance.metric,
                direction=self.performance.direction,
                details={
                    "gate": "composite",
                    "fail_closed": True,
                    "correctness": correctness_verdict.to_dict(),
                },
            )

        performance_verdict = self.performance.evaluate(baseline, candidate)
        accepted = performance_verdict.accepted
        return Verdict(
            status=VerdictStatus.ACCEPT if accepted else VerdictStatus.REJECT,
            reasons=performance_verdict.reasons,
            correctness_passed=True,
            performance_passed=accepted,
            metric=performance_verdict.metric,
            direction=performance_verdict.direction,
            baseline_value=performance_verdict.baseline_value,
            candidate_value=performance_verdict.candidate_value,
            relative_improvement=performance_verdict.relative_improvement,
            allowed_relative_regression=performance_verdict.allowed_relative_regression,
            min_relative_improvement=performance_verdict.min_relative_improvement,
            noise_tolerance=performance_verdict.noise_tolerance,
            details={
                "gate": "composite",
                "fail_closed": True,
                "correctness": correctness_verdict.to_dict(),
                "performance": performance_verdict.to_dict(),
            },
        )


def evaluate_candidate(
    *,
    baseline: Metrics,
    candidate: Metrics,
    performance_gate: PerformanceGate,
    correctness_gate: CorrectnessGate | None = None,
) -> Verdict:
    """Convenience wrapper for the common composite-gate operation."""

    return CompositeGate(
        performance=performance_gate,
        correctness=correctness_gate or CorrectnessGate(),
    ).evaluate(baseline, candidate)


__all__ = [
    "CompositeGate",
    "CorrectnessGate",
    "MetricInput",
    "MetricSource",
    "PerformanceGate",
    "evaluate_candidate",
]
