"""Adapter for SGLang's public serving benchmark CLI."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from .base import (
    AdapterCommand,
    AdapterError,
    BaseAdapter,
    BenchmarkType,
    append_base_args,
    append_parameter_args,
    cli_number,
    coerce_benchmark_type,
    optional_string,
    positive_int,
    require_string,
)


class SGLangAdapter(BaseAdapter):
    """Build ``python -m sglang.bench_serving`` commands.

    ``sglang.bench_serving`` is the compatibility entrypoint required by this
    project.  The adapter never imports SGLang and never starts or stops a server.
    """

    framework = "sglang"

    def __init__(self, python_executable: str = "python") -> None:
        if not python_executable or "\x00" in python_executable:
            raise AdapterError("python_executable must be a non-empty argv token")
        self.python_executable = python_executable

    def build_command(
        self,
        mode: str | BenchmarkType,
        workload: Mapping[str, object],
        parameters: Mapping[str, object],
        base_args: Sequence[str],
        result_path: str | Path,
    ) -> AdapterCommand:
        benchmark_type = coerce_benchmark_type(mode)
        if benchmark_type is not BenchmarkType.SERVE:
            raise AdapterError("SGLangAdapter supports only the stable serving benchmark CLI")
        if not isinstance(workload, Mapping):
            raise AdapterError("workload must be a mapping")

        return self.build_serve_command(
            model=require_string(workload, "model"),
            result_path=result_path,
            base_url=optional_string(workload, "endpoint"),
            dataset_name=str(workload.get("dataset", "random")),
            num_prompts=positive_int(workload, "num_prompts", 100),
            random_input_len=positive_int(workload, "input_tokens", 1024),
            random_output_len=positive_int(workload, "output_tokens", 128),
            max_concurrency=positive_int(workload, "concurrency", 1),
            parameters=parameters,
            base_args=base_args,
        )

    def build_serve_command(
        self,
        *,
        model: str,
        result_path: str | Path,
        backend: str = "sglang",
        base_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        dataset_name: str = "random",
        dataset_path: str | Path | None = None,
        num_prompts: int = 100,
        random_input_len: int | None = None,
        random_output_len: int | None = None,
        random_range_ratio: float | None = None,
        request_rate: int | float | str | None = None,
        max_concurrency: int | None = None,
        output_details: bool = False,
        parameters: Mapping[str, object] | None = None,
        base_args: Sequence[str] = (),
    ) -> AdapterCommand:
        """Describe one benchmark against an already-running SGLang endpoint."""

        model = _nonempty(model, "model")
        backend = _nonempty(backend, "backend")
        dataset_name = _nonempty(dataset_name, "dataset_name")
        result = _result_path(result_path)
        if base_url is not None and (host is not None or port is not None):
            raise AdapterError("base_url cannot be combined with host or port")

        argv = [
            self.python_executable,
            "-m",
            "sglang.bench_serving",
            "--backend",
            backend,
        ]
        if base_url is not None:
            argv.extend(("--base-url", _nonempty(base_url, "base_url")))
        else:
            if host is not None:
                argv.extend(("--host", _nonempty(host, "host")))
            if port is not None:
                argv.extend(("--port", str(_positive_integer(port, "port"))))
        argv.extend(
            (
                "--model",
                model,
                "--dataset-name",
                dataset_name,
                "--num-prompts",
                str(_positive_integer(num_prompts, "num_prompts")),
            )
        )
        if dataset_path is not None:
            argv.extend(("--dataset-path", str(dataset_path)))
        if random_input_len is not None:
            argv.extend(
                ("--random-input-len", str(_positive_integer(random_input_len, "random_input_len")))
            )
        if random_output_len is not None:
            argv.extend(
                (
                    "--random-output-len",
                    str(_positive_integer(random_output_len, "random_output_len")),
                )
            )
        if random_range_ratio is not None:
            if (
                isinstance(random_range_ratio, bool)
                or not isinstance(random_range_ratio, int | float)
                or not math.isfinite(random_range_ratio)
                or not 0 < random_range_ratio <= 1
            ):
                raise AdapterError("random_range_ratio must be in the interval (0, 1]")
            argv.extend(("--random-range-ratio", str(random_range_ratio)))
        if request_rate is not None:
            argv.extend(("--request-rate", cli_number(request_rate, "request_rate")))
        if max_concurrency is not None:
            argv.extend(
                (
                    "--max-concurrency",
                    str(_positive_integer(max_concurrency, "max_concurrency")),
                )
            )
        argv.extend(("--output-file", str(result)))
        if output_details:
            argv.append("--output-details")

        append_base_args(argv, base_args)
        append_parameter_args(
            argv,
            parameters or {},
            reserved=frozenset({"output_file"}),
        )
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.SERVE,
            argv=tuple(argv),
            result_path=result,
        )


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{name} must be a non-empty string without NUL bytes")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError(f"{name} must be a positive integer")
    return value


def _result_path(path: str | Path) -> Path:
    result = Path(path)
    if not str(result):
        raise AdapterError("result_path must not be empty")
    return result


__all__ = ["SGLangAdapter"]
