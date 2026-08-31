from __future__ import annotations

import math
import unittest

from euboulia.models import (
    Candidate,
    Experiment,
    ExperimentStatus,
    Framework,
    MetricDirection,
    Metrics,
    Verdict,
    VerdictStatus,
    Workload,
)


class ModelRoundTripTests(unittest.TestCase):
    def make_experiment(self) -> Experiment:
        workload = Workload(
            name="decode-1k-128",
            model="acme/model",
            input_tokens=1024,
            output_tokens=128,
            concurrency=16,
            num_requests=64,
            request_rate=8.5,
            dataset="random",
            seed=7,
            parameters={"temperature": 0.0, "stream": True},
            metadata={"tags": ["sm90", "decode"]},
        )
        candidate = Candidate(
            candidate_id="cand-001",
            framework=Framework.SGLANG,
            name="fused-rmsnorm",
            source_revision="deadbeef",
            parameters={"serve_args": ["--tp-size", "8"], "num_stages": 4},
            environment={"CUDA_VISIBLE_DEVICES": "0,1,2,3,4,5,6,7"},
            patch="changes/cand-001.diff",
        )
        baseline = Metrics(
            values={"mean_tpot_ms": 10.0, "completed": 64},
            units={"mean_tpot_ms": "ms", "completed": "requests"},
            samples={"mean_tpot_ms": (10.1, 9.9)},
            correctness_passed=True,
        )
        candidate_metrics = Metrics(
            values={"mean_tpot_ms": 9.0, "completed": 64},
            units={"mean_tpot_ms": "ms", "completed": "requests"},
            samples={"mean_tpot_ms": (9.1, 8.9)},
            correctness_passed=True,
        )
        verdict = Verdict(
            status=VerdictStatus.ACCEPT,
            reasons=("performance threshold passed",),
            correctness_passed=True,
            performance_passed=True,
            metric="mean_tpot_ms",
            direction=MetricDirection.MINIMIZE,
            baseline_value=10.0,
            candidate_value=9.0,
            relative_improvement=0.1,
            details={"source": "test"},
        )
        return Experiment(
            experiment_id="exp-001",
            workload=workload,
            candidate=candidate,
            status=ExperimentStatus.SUCCEEDED,
            metrics=candidate_metrics,
            baseline_metrics=baseline,
            verdict=verdict,
            created_at="2026-08-31T00:00:00Z",
            finished_at="2026-08-31T00:01:00Z",
            artifacts=("trace.json", "summary.json"),
            metadata={"runner": {"host": "gpu-node"}},
        )

    def test_nested_experiment_json_round_trip(self) -> None:
        original = self.make_experiment()
        restored = Experiment.from_json(original.to_json())
        self.assertEqual(restored, original)
        self.assertIs(restored.candidate.framework, Framework.SGLANG)
        self.assertIs(restored.verdict.direction, MetricDirection.MINIMIZE)
        self.assertEqual(restored.metrics.samples["mean_tpot_ms"], (9.1, 8.9))

    def test_each_leaf_model_round_trips(self) -> None:
        experiment = self.make_experiment()
        for model in (
            experiment.workload,
            experiment.candidate,
            experiment.metrics,
            experiment.verdict,
        ):
            with self.subTest(model=type(model).__name__):
                self.assertEqual(type(model).from_json(model.to_json()), model)

    def test_string_enums_are_normalized(self) -> None:
        candidate = Candidate(candidate_id="c", framework="vllm")
        verdict = Verdict(status="accept", direction="max")
        self.assertIs(candidate.framework, Framework.VLLM)
        self.assertIs(verdict.status, VerdictStatus.ACCEPT)
        self.assertIs(verdict.direction, MetricDirection.MAXIMIZE)

    def test_dynamic_metrics_preserve_integer_counters(self) -> None:
        metrics = Metrics(
            values={
                "request_throughput": 12.5,
                "output_token_throughput": 900,
                "completed": 32,
                "failed": 0,
            }
        )
        restored = Metrics.from_json(metrics.to_json())
        self.assertEqual(restored.values, metrics.values)
        self.assertIsInstance(restored.values["completed"], int)

    def test_non_finite_metrics_can_be_gated_but_not_serialized(self) -> None:
        metrics = Metrics(values={"latency_ms": math.nan})
        self.assertFalse(metrics.is_finite("latency_ms"))
        with self.assertRaises(ValueError):
            metrics.to_json()

    def test_invalid_shapes_and_metadata_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Workload(name="bad", model="m", concurrency=0)
        with self.assertRaises(TypeError):
            Candidate(candidate_id="c", framework=Framework.VLLM, environment={"X": 1})
        with self.assertRaises(ValueError):
            Metrics(values={"latency": 1.0}, units={"unknown": "ms"})
        with self.assertRaises(ValueError):
            Workload(name="bad", model="m", metadata={"nan": math.inf})


if __name__ == "__main__":
    unittest.main()
