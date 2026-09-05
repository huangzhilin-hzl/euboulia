from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from euboulia.harnesses.lm_eval import run_lm_eval


def test_timestamped_result_is_published_unchanged_and_raw_evidence_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = tmp_path / "accuracy.json"
    result.write_text('{"results": {"gsm8k": {"exact_match": 1}}}')
    payload = '{\n "results": {"gsm8k": {"exact_match,flexible-extract": 0.81}}\n}\n'
    arguments = ["--tasks", "gsm8k", "--num_fewshot", "8", "--limit", "200"]

    def execute(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert command[:4] == [sys.executable, "-u", "-m", "lm_eval"]
        assert command[4:-2] == arguments
        assert command[-2] == "--output_path"
        assert not result.exists()
        Path(command[-1]).with_name("results_2026-09-05T06-00-00.json").write_text(payload)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    evidence = tmp_path / "evidence"
    assert run_lm_eval(result, arguments, evidence_dir=evidence) == 0
    assert result.read_text() == payload
    assert next(evidence.glob("lm-eval-*/results_*.json")).read_text() == payload


def test_child_failure_removes_stale_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = tmp_path / "accuracy.json"
    result.write_text('{"results": {"gsm8k": {"exact_match": 1}}}')
    monkeypatch.setattr(
        subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 7)
    )
    assert run_lm_eval(result, ["--tasks", "gsm8k"], evidence_dir=tmp_path / "evidence") == 7
    assert not result.exists()


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_results_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    def execute(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        for index in range(count):
            Path(command[-1]).with_name(f"results_{index}.json").write_text('{"results": {}}')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    result = tmp_path / "accuracy.json"
    with pytest.raises(ValueError, match="expected one fresh"):
        run_lm_eval(result, [], evidence_dir=tmp_path / "evidence")
    assert not result.exists()


def test_gsm8k_override_inherits_unmodified_installed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = tmp_path / "installed-gsm8k.yaml"
    task = 'task: gsm8k\ndataset_path: gsm8k\ndoc_to_text: "Question: {{question}}"\n'
    upstream.write_text(task)
    monkeypatch.setattr(
        "euboulia.harnesses.lm_eval.importlib.metadata.distribution",
        lambda name: SimpleNamespace(locate_file=lambda path: upstream),
    )

    def execute(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        override = Path(command[command.index("--tasks") + 1])
        assert json.loads(override.read_text()) == {
            "include": "upstream-gsm8k.yaml", "dataset_path": "openai/gsm8k"
        }
        assert (override.parent / "upstream-gsm8k.yaml").read_text() == task
        assert command[command.index("--limit") + 1] == "200"
        Path(command[-1]).with_name("results_timestamp.json").write_text('{"results": {}}')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_lm_eval(
        tmp_path / "accuracy.json",
        ["--tasks", "gsm8k", "--limit", "200"],
        evidence_dir=tmp_path / "evidence",
        gsm8k_dataset_path="openai/gsm8k",
    ) == 0
