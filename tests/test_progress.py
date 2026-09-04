import json
from pathlib import Path

import pytest

from euboulia.progress import read_run_progress, write_run_progress

RUN_UID = "run-01HF7YAT000000000000000000"


def test_progress_record_is_private_atomic_and_round_trips(tmp_path: Path) -> None:
    path = write_run_progress(
        tmp_path,
        RUN_UID,
        status="running",
        phase="evaluating",
        detail="qualification point p1",
        completed_units=1,
        total_units=3,
    )

    assert path == tmp_path / RUN_UID / "progress.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_run_progress(path) == json.loads(path.read_text(encoding="utf-8"))
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_progress_rejects_invalid_completed_units(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="completed_units"):
        write_run_progress(
            tmp_path,
            RUN_UID,
            status="running",
            phase="evaluating",
            completed_units=value,  # type: ignore[arg-type]
            total_units=3,
        )
