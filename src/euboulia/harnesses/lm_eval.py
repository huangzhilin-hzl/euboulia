"""Run external lm-eval and publish its unchanged result at a stable path."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path


def run_lm_eval(
    result_path: Path,
    arguments: Sequence[str],
    *,
    evidence_dir: Path,
    gsm8k_dataset_path: str | None = None,
) -> int:
    """Keep raw CLI output and reject missing, ambiguous, or stale results."""

    result_path = result_path.resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(tempfile.mkdtemp(prefix="lm-eval-", dir=evidence_dir)).resolve()
    child_arguments = list(arguments)
    if any(arg.split("=", 1)[0] in {"--output_path", "-o"} for arg in child_arguments):
        raise ValueError("use --result-path; lm-eval output is managed by the adapter")
    if gsm8k_dataset_path is not None:
        _override_gsm8k_dataset(child_arguments, raw_dir, gsm8k_dataset_path)
    command = [
        sys.executable,
        "-u",
        "-m",
        "lm_eval",
        *child_arguments,
        "--output_path",
        str(raw_dir / "results.json"),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        return completed.returncode
    candidates = [*raw_dir.glob("results_*.json"), *raw_dir.glob("results.json")]
    if len(candidates) != 1 or not candidates[0].is_file() or candidates[0].is_symlink():
        raise ValueError(f"expected one fresh lm-eval result, found {len(candidates)}")
    source = candidates[0]
    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("results"), dict):
        raise ValueError("lm-eval result must contain a results object")
    with tempfile.NamedTemporaryFile(dir=result_path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(source, staged)
        staged.replace(result_path)
    finally:
        staged.unlink(missing_ok=True)
    print(f"lm-eval result: {source} -> {result_path}", flush=True)
    return 0


def _override_gsm8k_dataset(arguments: list[str], raw_dir: Path, dataset_path: str) -> None:
    """Inherit the installed task, changing only the Hub repository identifier."""

    try:
        index = arguments.index("--tasks") + 1
        task = arguments[index]
    except (ValueError, IndexError) as exc:
        raise ValueError("--gsm8k-dataset-path requires --tasks gsm8k") from exc
    if task != "gsm8k":
        raise ValueError("--gsm8k-dataset-path requires --tasks gsm8k")
    upstream = Path(
        str(importlib.metadata.distribution("lm_eval").locate_file("lm_eval/tasks/gsm8k/gsm8k.yaml"))
    )
    shutil.copyfile(upstream, raw_dir / "upstream-gsm8k.yaml")
    override = raw_dir / "gsm8k.yaml"
    override.write_text(
        json.dumps({"include": "upstream-gsm8k.yaml", "dataset_path": dataset_path}) + "\n",
        encoding="utf-8",
    )
    arguments[index] = str(override)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--gsm8k-dataset-path")
    options, arguments = parser.parse_known_args(argv)
    evidence_dir = Path(
        os.environ.get("EUBOULIA_COMMAND_EVIDENCE_DIR", str(options.result_path.parent))
    )
    try:
        return run_lm_eval(
            options.result_path,
            arguments,
            evidence_dir=evidence_dir,
            gsm8k_dataset_path=options.gsm8k_dataset_path,
        )
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        print(f"lm-eval adapter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
