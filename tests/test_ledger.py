from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from euboulia.ledger import ExperimentLedger, LedgerCorruptionError
from euboulia.models import (
    Candidate,
    Experiment,
    ExperimentStatus,
    Framework,
    Metrics,
    Workload,
)


def make_experiment(
    experiment_id: str,
    status: ExperimentStatus = ExperimentStatus.PENDING,
    latency: float | None = None,
) -> Experiment:
    return Experiment(
        experiment_id=experiment_id,
        workload=Workload(name="smoke", model="acme/model"),
        candidate=Candidate(candidate_id="candidate", framework=Framework.VLLM),
        status=status,
        metrics=Metrics(values={"latency_ms": latency}) if latency is not None else None,
        created_at="2026-08-31T00:00:00Z",
    )


class ExperimentLedgerTests(unittest.TestCase):
    def test_append_preserves_order_and_existing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "experiments.jsonl"
            ledger = ExperimentLedger(path, fsync=True)
            first = make_experiment("one")
            second = make_experiment("two", ExperimentStatus.SUCCEEDED, 3.5)

            ledger.append(first)
            first_bytes = path.read_bytes()
            ledger.append(second)

            self.assertTrue(path.read_bytes().startswith(first_bytes))
            self.assertEqual(ledger.read_all(), [first, second])
            self.assertEqual(len(ledger), 2)
            self.assertEqual(ledger.latest(), second)

            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["experiment_id"], "one")

    def test_repeated_id_records_history_and_get_returns_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ExperimentLedger(Path(directory) / "experiments.jsonl")
            pending = make_experiment("same")
            finished = make_experiment("same", ExperimentStatus.SUCCEEDED, 4.0)
            ledger.append(pending)
            ledger.append(finished)

            self.assertEqual(ledger.history("same"), [pending, finished])
            self.assertEqual(ledger.get("same"), finished)
            self.assertIsNone(ledger.get("missing"))

    def test_missing_ledger_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ExperimentLedger(Path(directory) / "missing.jsonl")
            self.assertEqual(ledger.read_all(), [])
            self.assertIsNone(ledger.latest())

    def test_corrupt_line_reports_path_and_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiments.jsonl"
            first = make_experiment("valid")
            path.write_text(first.to_json() + "\n" + "{not-json}\n")
            ledger = ExperimentLedger(path)

            with self.assertRaises(LedgerCorruptionError) as caught:
                ledger.read_all()
            self.assertEqual(caught.exception.line_number, 2)
            self.assertIn(str(path), str(caught.exception))

    def test_append_rejects_non_experiment_and_non_finite_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ExperimentLedger(Path(directory) / "experiments.jsonl")
            with self.assertRaises(TypeError):
                ledger.append("not an experiment")  # type: ignore[arg-type]
            bad = make_experiment("bad", ExperimentStatus.SUCCEEDED, float("nan"))
            with self.assertRaises(ValueError):
                ledger.append(bad)
            self.assertFalse(ledger.path.exists())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform lacks O_NOFOLLOW")
    def test_append_refuses_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "valuable.txt"
            target.write_text("unchanged\n")
            link = Path(directory) / "experiments.jsonl"
            link.symlink_to(target)

            with self.assertRaises(OSError):
                ExperimentLedger(link).append(make_experiment("symlink"))

            self.assertEqual(target.read_text(), "unchanged\n")


if __name__ == "__main__":
    unittest.main()
