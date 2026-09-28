"""Validated profiler options; target command and output paths belong to the worker."""

from __future__ import annotations

DEFAULT_ARGUMENTS = {
    "run": "",
    "ncu": (
        "--kernel-name-base function\n"
        "--kernel-name 'regex:.*'\n"
        "--set detailed\n"
        "--launch-count 1"
    ),
    "nsys": "--trace=cuda,nvtx\n--sample=none\n--cpuctxsw=none",
}
REPORT_NAMES = {"details.txt", "sass.txt", "stats.txt"}

# Deliberately exclude output, executable, config-file, device and attach options.
# A separate value or --name=value is accepted; no shell evaluation takes place.
OPTIONS = {
    "ncu": {
        "--set",
        "--section",
        "--metrics",
        "--kernel-name",
        "--kernel-name-base",
        "--launch-count",
        "--launch-skip",
        "--launch-skip-before-match",
        "--nvtx-include",
        "--nvtx-exclude",
        "--replay-mode",
        "--profile-from-start",
        "--target-processes",
        "--cache-control",
    },
    "nsys": {
        "--trace",
        "--sample",
        "--cpuctxsw",
        "--capture-range",
        "--capture-range-end",
        "--nvtx-capture",
        "--duration",
        "--delay",
        "--cuda-memory-usage",
        "--cuda-graph-trace",
    },
}


def validate_options(mode: str, args: list[str]) -> None:
    if mode not in DEFAULT_ARGUMENTS:
        raise ValueError("mode must be run, ncu or nsys")
    if mode == "run":
        if args:
            raise ValueError("profiler arguments require ncu or nsys mode")
        return
    index = 0
    while index < len(args):
        token = args[index]
        key, sep, value = token.partition("=")
        if key == "--nvtx" and mode == "ncu" and not sep:
            index += 1
            continue
        if key not in OPTIONS[mode]:
            raise ValueError(f"unsupported {mode} option: {key}; use long option names")
        if not sep:
            index += 1
            value = args[index] if index < len(args) else ""
        if not value or value.startswith("-"):
            raise ValueError(f"{key} needs a value")
        if (
            mode == "nsys"
            and key in {"--sample", "--cpuctxsw"}
            and value not in {"none", "process-tree"}
        ):
            raise ValueError(f"{key} must be none or process-tree")
        index += 1
