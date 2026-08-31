from __future__ import annotations

import math
import unittest

from euboulia.gates import CompositeGate, CorrectnessGate, PerformanceGate
from euboulia.models import MetricDirection, Metrics, VerdictStatus


class CorrectnessGateTests(unittest.TestCase):
    def test_boolean_correctness_is_fail_closed(self) -> None:
        gate = CorrectnessGate()
        self.assertEqual(
            gate.evaluate(Metrics(correctness_passed=True)).status,
            VerdictStatus.ACCEPT,
        )
        self.assertEqual(
            gate.evaluate(Metrics(correctness_passed=False)).status,
            VerdictStatus.REJECT,
        )
        self.assertEqual(gate.evaluate(Metrics()).status, VerdictStatus.REJECT)

    def test_scalar_correctness_supports_min_and_max_thresholds(self) -> None:
        self.assertTrue(
            CorrectnessGate(metric="correct", threshold=1)
            .evaluate(Metrics(values={"correct": 1}))
            .accepted
        )
        self.assertTrue(
            CorrectnessGate(metric="failed", direction=MetricDirection.MINIMIZE, threshold=0)
            .evaluate(Metrics(values={"failed": 0}))
            .accepted
        )
        self.assertFalse(
            CorrectnessGate(metric="failed", direction=MetricDirection.MINIMIZE, threshold=0)
            .evaluate(Metrics(values={"failed": 1}))
            .accepted
        )

    def test_non_finite_correctness_rejects(self) -> None:
        verdict = CorrectnessGate(metric="error").evaluate(Metrics(values={"error": math.inf}))
        self.assertFalse(verdict.accepted)
        self.assertIn("not finite", verdict.reasons[0])


class PerformanceGateTests(unittest.TestCase):
    def test_minimize_direction_computes_relative_improvement(self) -> None:
        verdict = PerformanceGate(
            metric="latency_ms",
            direction=MetricDirection.MINIMIZE,
            min_relative_improvement=0.10,
        ).evaluate(
            Metrics(values={"latency_ms": 100.0}),
            Metrics(values={"latency_ms": 90.0}),
        )
        self.assertTrue(verdict.accepted)
        self.assertAlmostEqual(verdict.relative_improvement, 0.10)

    def test_maximize_direction_and_required_improvement_boundary(self) -> None:
        gate = PerformanceGate(
            metric="output_token_throughput",
            direction=MetricDirection.MAXIMIZE,
            min_relative_improvement=0.05,
        )
        self.assertTrue(gate.evaluate(100.0, 105.0).accepted)
        self.assertFalse(gate.evaluate(100.0, 104.999).accepted)

    def test_allowed_regression_boundary_is_inclusive(self) -> None:
        gate = PerformanceGate(
            metric="tpot_ms",
            direction=MetricDirection.MINIMIZE,
            allowed_relative_regression=0.02,
        )
        self.assertTrue(gate.evaluate(10.0, 10.2).accepted)
        self.assertFalse(gate.evaluate(10.0, 10.201).accepted)

    def test_noise_tolerance_neutralizes_small_change(self) -> None:
        gate = PerformanceGate(
            metric="latency_ms",
            direction=MetricDirection.MINIMIZE,
            noise_tolerance=0.01,
        )
        verdict = gate.evaluate(100.0, 100.8)
        self.assertTrue(verdict.accepted)
        self.assertIn("noise", verdict.reasons[0])

    def test_missing_and_non_finite_values_reject(self) -> None:
        gate = PerformanceGate("latency_ms", MetricDirection.MINIMIZE)
        cases = [
            (Metrics(), Metrics(values={"latency_ms": 1.0})),
            (Metrics(values={"latency_ms": 1.0}), Metrics()),
            (Metrics(values={"latency_ms": math.nan}), Metrics(values={"latency_ms": 1.0})),
            (Metrics(values={"latency_ms": 1.0}), Metrics(values={"latency_ms": math.inf})),
        ]
        for baseline, candidate in cases:
            with self.subTest(baseline=baseline.values, candidate=candidate.values):
                verdict = gate.evaluate(baseline, candidate)
                self.assertFalse(verdict.accepted)
                self.assertTrue(verdict.details["fail_closed"])

    def test_zero_baseline_has_explicit_directional_semantics(self) -> None:
        maximize = PerformanceGate("throughput", MetricDirection.MAXIMIZE)
        self.assertTrue(maximize.evaluate(0.0, 1.0).accepted)
        self.assertFalse(maximize.evaluate(0.0, -1.0).accepted)
        self.assertTrue(maximize.evaluate(0.0, 0.0).accepted)

        requires_gain = PerformanceGate(
            "throughput", MetricDirection.MAXIMIZE, min_relative_improvement=0.01
        )
        self.assertFalse(requires_gain.evaluate(0.0, 0.0).accepted)

    def test_threshold_validation(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceGate("latency", MetricDirection.MINIMIZE, noise_tolerance=-0.1)
        with self.assertRaises(ValueError):
            PerformanceGate(
                "latency", MetricDirection.MINIMIZE, allowed_relative_regression=math.inf
            )


class CompositeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = CompositeGate(
            performance=PerformanceGate("latency_ms", MetricDirection.MINIMIZE)
        )
        self.baseline = Metrics(values={"latency_ms": 10.0}, correctness_passed=True)

    def test_accepts_only_when_both_gates_pass(self) -> None:
        candidate = Metrics(values={"latency_ms": 9.0}, correctness_passed=True)
        verdict = self.gate.evaluate(self.baseline, candidate)
        self.assertTrue(verdict.accepted)
        self.assertTrue(verdict.correctness_passed)
        self.assertTrue(verdict.performance_passed)

    def test_correctness_failure_short_circuits_performance(self) -> None:
        candidate = Metrics(values={"latency_ms": 1.0}, correctness_passed=False)
        verdict = self.gate.evaluate(self.baseline, candidate)
        self.assertFalse(verdict.accepted)
        self.assertFalse(verdict.correctness_passed)
        self.assertIsNone(verdict.performance_passed)
        self.assertNotIn("performance", verdict.details)

    def test_missing_correctness_or_performance_is_rejected(self) -> None:
        candidates = [
            Metrics(values={"latency_ms": 9.0}),
            Metrics(correctness_passed=True),
            Metrics(values={"latency_ms": math.nan}, correctness_passed=True),
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate.values):
                self.assertFalse(self.gate.evaluate(self.baseline, candidate).accepted)


if __name__ == "__main__":
    unittest.main()
