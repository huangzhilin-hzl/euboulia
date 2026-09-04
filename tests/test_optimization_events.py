import math
from pathlib import Path

import pytest

from euboulia.optimization import (
    ArtifactRef,
    EventLedger,
    EventLedgerCorruptionError,
    EventType,
    OptimizationEvent,
)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="trace-1",
        path="iterations/001/profile/trace.json",
        sha256="a" * 64,
        size_bytes=42,
        media_type="application/json",
    )


def test_event_json_round_trip_preserves_typed_artifacts() -> None:
    event = OptimizationEvent(
        event_id="event-1",
        event_type=EventType.PROFILE_COMPLETED,
        run_uid="run-1",
        iteration_id="iteration-1",
        input_digest="input-digest",
        payload={"provider": "sglang_torch", "count": 1},
        artifacts=(artifact(),),
    )

    restored = OptimizationEvent.from_json(event.to_json())

    assert restored == event
    assert restored.to_dict()["schema_version"] == 2


def test_event_ledger_appends_without_rewriting_and_filters(tmp_path: Path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    ledger = EventLedger(ledger_path, fsync=True)
    first = OptimizationEvent.create(EventType.RUN_STARTED, "run-1")
    second = OptimizationEvent.create(
        EventType.ITERATION_STARTED, "run-1", iteration_id="iteration-1"
    )
    third = OptimizationEvent.create(EventType.RUN_STARTED, "run-2")

    ledger.append(first)
    first_bytes = ledger_path.read_bytes()
    ledger.append(second)
    ledger.append(third)

    assert ledger_path.read_bytes().startswith(first_bytes)
    assert ledger.read_all() == [first, second, third]
    assert ledger.by_run("run-1") == [first, second]
    assert ledger.by_iteration("run-1", "iteration-1") == [second]
    assert ledger.latest("run-1") == second
    assert len(ledger) == 3


def test_missing_event_ledger_is_empty_without_creation(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "events.jsonl"
    ledger = EventLedger(path, create_parents=False)

    assert ledger.read_all() == []
    assert not path.parent.exists()


def test_corrupt_event_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    valid = OptimizationEvent.create(EventType.RUN_STARTED, "run-1")
    path.write_text(valid.to_json() + "\nnot json\n", encoding="utf-8")

    with pytest.raises(EventLedgerCorruptionError) as error:
        EventLedger(path).read_all()

    assert error.value.line_number == 2
    assert error.value.path == path


def test_events_reject_non_finite_payload_and_wrong_ledger_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        OptimizationEvent.create(
            EventType.RUN_STARTED,
            "run-1",
            payload={"bad": math.nan},
        )

    with pytest.raises(TypeError, match="OptimizationEvent"):
        EventLedger(tmp_path / "events.jsonl").append("event")  # type: ignore[arg-type]
