from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from euboulia.execution import CommandExecutor, execute


def test_command_captures_stdout_stderr_and_metadata(tmp_path: Path) -> None:
    result = CommandExecutor(tmp_path / "artifacts").run(
        [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('warning', file=sys.stderr)",
        ],
        artifact_prefix="smoke",
    )

    assert result.succeeded
    assert result.returncode == 0
    assert result.argv[0] == sys.executable
    assert result.stdout_path.read_text(encoding="utf-8") == "hello\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "warning\n"
    assert result.duration_seconds >= 0
    assert result.to_dict()["succeeded"] is True


def test_argument_with_shell_syntax_is_never_executed_by_a_shell(tmp_path: Path) -> None:
    sentinel = tmp_path / "must-not-exist"
    shell_like_argument = f"; touch {sentinel}"

    result = CommandExecutor(tmp_path / "artifacts").run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", shell_like_argument]
    )

    assert result.succeeded
    assert result.stdout_path.read_text(encoding="utf-8").strip() == shell_like_argument
    assert not sentinel.exists()


def test_dry_run_creates_artifacts_without_spawning_process(tmp_path: Path) -> None:
    sentinel = tmp_path / "not-created"
    result = execute(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"],
        artifact_dir=tmp_path / "artifacts",
        dry_run=True,
    )

    assert result.dry_run
    assert result.succeeded
    assert result.returncode is None
    assert result.stdout_path.exists()
    assert result.stderr_path.exists()
    assert not sentinel.exists()


def test_timeout_is_a_structured_result(tmp_path: Path) -> None:
    result = CommandExecutor(tmp_path / "artifacts").run(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_seconds=0.05,
    )

    assert result.timed_out
    assert not result.succeeded
    assert result.returncode is None
    assert result.error is not None
    assert "timed out" in result.error


def test_environment_is_allowlisted_and_overrides_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EUBOULIA_ALLOWED", "from-parent")
    monkeypatch.setenv("EUBOULIA_SECRET", "do-not-inherit")
    script = (
        "import json, os; "
        "print(json.dumps({k: os.environ.get(k) for k in "
        "['EUBOULIA_ALLOWED', 'EUBOULIA_OVERRIDE', 'EUBOULIA_SECRET']}))"
    )

    result = CommandExecutor(
        tmp_path / "artifacts",
        env_allowlist={"PATH", "EUBOULIA_ALLOWED"},
        env_overrides={"EUBOULIA_OVERRIDE": "explicit"},
    ).run([sys.executable, "-c", script])
    payload = json.loads(result.stdout_path.read_text(encoding="utf-8"))

    assert result.succeeded
    assert payload == {
        "EUBOULIA_ALLOWED": "from-parent",
        "EUBOULIA_OVERRIDE": "explicit",
        "EUBOULIA_SECRET": None,
    }
    assert "EUBOULIA_SECRET" not in result.environment_keys
    assert "explicit" not in json.dumps(result.to_dict())


def test_run_overrides_can_remove_constructor_environment(tmp_path: Path) -> None:
    result = CommandExecutor(
        tmp_path / "artifacts",
        env_allowlist={"PATH"},
        env_overrides={"REMOVE_ME": "constructor-value"},
    ).run(
        [sys.executable, "-c", "import os; print(os.environ.get('REMOVE_ME', 'missing'))"],
        env_overrides={"REMOVE_ME": None},
    )

    assert result.succeeded
    assert result.stdout_path.read_text(encoding="utf-8").strip() == "missing"


def test_missing_executable_is_recorded_instead_of_raised(tmp_path: Path) -> None:
    result = CommandExecutor(tmp_path / "artifacts").run(
        [str(tmp_path / "definitely-not-an-executable")]
    )

    assert not result.succeeded
    assert result.returncode is None
    assert result.error is not None
    assert result.stderr_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", ["echo unsafe", b"echo unsafe", [], ["has\x00nul"]])
def test_invalid_argv_is_rejected_before_artifacts_are_created(
    tmp_path: Path, argv: object
) -> None:
    executor = CommandExecutor(tmp_path / "artifacts")

    with pytest.raises((TypeError, ValueError)):
        executor.run(argv)  # type: ignore[arg-type]

    assert not (tmp_path / "artifacts").exists()


def test_environment_values_must_not_contain_nul(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NUL"):
        CommandExecutor(tmp_path / "artifacts", env_overrides={"BAD": "a\x00b"})


def test_parent_environment_is_not_mutated(tmp_path: Path) -> None:
    before = dict(os.environ)
    result = CommandExecutor(
        tmp_path / "artifacts", env_overrides={"EUBOULIA_CHILD_ONLY": "1"}
    ).run([sys.executable, "-c", "pass"])

    assert result.succeeded
    assert dict(os.environ) == before
