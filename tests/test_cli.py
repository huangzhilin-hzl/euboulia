import json
from pathlib import Path

from test_config import VALID_CONFIG, write_config

from euboulia.cli import main


def test_plan_cli_renders_without_execution(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG)

    exit_code = main(["plan", "--config", str(path)])

    assert exit_code == 0
    assert not (tmp_path / "artifacts").exists()


def test_evaluate_cli_returns_rejection_status(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, VALID_CONFIG)
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(
        json.dumps({"completed": 16, "failed": 0, "output_throughput": 100.0}),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps({"completed": 16, "failed": 0, "output_throughput": 99.0}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "evaluate",
            "--config",
            str(config_path),
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
        ]
    )

    assert exit_code == 1
