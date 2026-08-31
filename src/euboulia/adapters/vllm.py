"""Adapter for the stable ``vllm bench`` command family."""

from __future__ import annotations

import math
import shlex
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


class VLLMAdapter(BaseAdapter):
    """Build public vLLM benchmark commands without importing or running vLLM."""

    framework = "vllm"

    def __init__(self, executable: str = "vllm") -> None:
        self.executable = _nonempty(executable, "executable")

    def build_command(
        self,
        mode: str | BenchmarkType,
        workload: Mapping[str, object],
        parameters: Mapping[str, object],
        base_args: Sequence[str],
        result_path: str | Path,
    ) -> AdapterCommand:
        benchmark_type = coerce_benchmark_type(mode)
        if not isinstance(workload, Mapping):
            raise AdapterError("workload must be a mapping")

        model = require_string(workload, "model")
        if benchmark_type is BenchmarkType.SERVE:
            return self.build_serve_command(
                model=model,
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
        if benchmark_type is BenchmarkType.LATENCY:
            return self.build_latency_command(
                model=model,
                result_path=result_path,
                input_len=positive_int(workload, "input_tokens", 32),
                output_len=positive_int(workload, "output_tokens", 128),
                batch_size=positive_int(workload, "concurrency", 8),
                parameters=parameters,
                base_args=base_args,
            )
        if benchmark_type is BenchmarkType.THROUGHPUT:
            return self.build_throughput_command(
                model=model,
                result_path=result_path,
                dataset_name=str(workload.get("dataset", "random")),
                input_len=positive_int(workload, "input_tokens", 1024),
                output_len=positive_int(workload, "output_tokens", 128),
                num_prompts=positive_int(workload, "num_prompts", 100),
                parameters=parameters,
                base_args=base_args,
            )

        sweep_parameters = dict(parameters)
        try:
            serve_cmd = sweep_parameters.pop("serve_cmd")
            bench_cmd = sweep_parameters.pop("bench_cmd")
        except KeyError as exc:
            raise AdapterError(
                "sweep parameters must provide serve_cmd and bench_cmd argv values"
            ) from exc
        if sweep_parameters:
            names = ", ".join(sorted(str(name) for name in sweep_parameters))
            raise AdapterError(f"unsupported generic sweep parameters: {names}")
        return self.build_sweep_command(
            serve_cmd=_nested_command(serve_cmd, "serve_cmd"),
            bench_cmd=_nested_command(bench_cmd, "bench_cmd"),
            result_path=result_path,
            base_args=base_args,
        )

    def build_serve_command(
        self,
        *,
        model: str,
        result_path: str | Path,
        backend: str = "vllm",
        base_url: str | None = None,
        endpoint: str = "/v1/completions",
        dataset_name: str = "random",
        dataset_path: str | Path | None = None,
        num_prompts: int = 100,
        num_warmups: int | None = None,
        random_input_len: int | None = None,
        random_output_len: int | None = None,
        request_rate: int | float | str | None = None,
        max_concurrency: int | None = None,
        percentile_metrics: Sequence[str] = ("ttft", "tpot", "itl", "e2el"),
        metric_percentiles: Sequence[int | float] = (50, 90, 99),
        save_detailed: bool = False,
        parameters: Mapping[str, object] | None = None,
        base_args: Sequence[str] = (),
    ) -> AdapterCommand:
        """Describe a load benchmark against an already-running HTTP endpoint."""

        result = _result_path(result_path)
        argv = [
            self.executable,
            "bench",
            "serve",
            "--backend",
            _nonempty(backend, "backend"),
            "--model",
            _nonempty(model, "model"),
        ]
        if base_url is not None:
            argv.extend(("--base-url", _nonempty(base_url, "base_url")))
        argv.extend(
            (
                "--endpoint",
                _nonempty(endpoint, "endpoint"),
                "--dataset-name",
                _nonempty(dataset_name, "dataset_name"),
                "--num-prompts",
                str(_positive_integer(num_prompts, "num_prompts")),
            )
        )
        if dataset_path is not None:
            argv.extend(("--dataset-path", str(dataset_path)))
        if num_warmups is not None:
            argv.extend(("--num-warmups", str(_nonnegative_integer(num_warmups, "num_warmups"))))
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
        if request_rate is not None:
            argv.extend(("--request-rate", cli_number(request_rate, "request_rate")))
        if max_concurrency is not None:
            argv.extend(
                (
                    "--max-concurrency",
                    str(_positive_integer(max_concurrency, "max_concurrency")),
                )
            )

        metrics = [_nonempty(item, "percentile_metrics item") for item in percentile_metrics]
        if not metrics:
            raise AdapterError("percentile_metrics must not be empty")
        percentiles = [_percentile(item) for item in metric_percentiles]
        if not percentiles:
            raise AdapterError("metric_percentiles must not be empty")
        argv.extend(("--percentile-metrics", ",".join(metrics)))
        argv.extend(("--metric-percentiles", ",".join(percentiles)))
        argv.append("--save-result")
        argv.extend(("--result-dir", str(result.parent), "--result-filename", result.name))
        if save_detailed:
            argv.append("--save-detailed")

        append_base_args(argv, base_args)
        append_parameter_args(
            argv,
            parameters or {},
            reserved=frozenset({"save_result", "result_dir", "result_filename"}),
        )
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.SERVE,
            argv=tuple(argv),
            result_path=result,
        )

    def build_latency_command(
        self,
        *,
        model: str,
        result_path: str | Path,
        input_len: int = 32,
        output_len: int = 128,
        batch_size: int = 8,
        num_iters_warmup: int = 10,
        num_iters: int = 30,
        parameters: Mapping[str, object] | None = None,
        base_args: Sequence[str] = (),
    ) -> AdapterCommand:
        """Build the stable offline latency benchmark command."""

        result = _result_path(result_path)
        argv = [
            self.executable,
            "bench",
            "latency",
            "--model",
            _nonempty(model, "model"),
            "--input-len",
            str(_positive_integer(input_len, "input_len")),
            "--output-len",
            str(_positive_integer(output_len, "output_len")),
            "--batch-size",
            str(_positive_integer(batch_size, "batch_size")),
            "--num-iters-warmup",
            str(_nonnegative_integer(num_iters_warmup, "num_iters_warmup")),
            "--num-iters",
            str(_positive_integer(num_iters, "num_iters")),
            "--output-json",
            str(result),
        ]
        append_base_args(argv, base_args)
        append_parameter_args(
            argv,
            parameters or {},
            reserved=frozenset({"output_json"}),
        )
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.LATENCY,
            argv=tuple(argv),
            result_path=result,
        )

    def build_throughput_command(
        self,
        *,
        model: str,
        result_path: str | Path,
        dataset_name: str = "random",
        dataset_path: str | Path | None = None,
        input_len: int | None = None,
        output_len: int | None = None,
        num_prompts: int = 100,
        parameters: Mapping[str, object] | None = None,
        base_args: Sequence[str] = (),
    ) -> AdapterCommand:
        """Build the stable offline throughput benchmark command."""

        result = _result_path(result_path)
        argv = [
            self.executable,
            "bench",
            "throughput",
            "--model",
            _nonempty(model, "model"),
            "--dataset-name",
            _nonempty(dataset_name, "dataset_name"),
            "--num-prompts",
            str(_positive_integer(num_prompts, "num_prompts")),
        ]
        if dataset_path is not None:
            argv.extend(("--dataset-path", str(dataset_path)))
        if input_len is not None:
            argv.extend(("--input-len", str(_positive_integer(input_len, "input_len"))))
        if output_len is not None:
            argv.extend(("--output-len", str(_positive_integer(output_len, "output_len"))))
        argv.extend(("--output-json", str(result)))
        append_base_args(argv, base_args)
        append_parameter_args(
            argv,
            parameters or {},
            reserved=frozenset({"output_json"}),
        )
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.THROUGHPUT,
            argv=tuple(argv),
            result_path=result,
        )

    def build_sweep_command(
        self,
        *,
        serve_cmd: str | Sequence[str],
        bench_cmd: str | Sequence[str],
        result_path: str | Path,
        serve_params: str | Path | None = None,
        bench_params: str | Path | None = None,
        after_bench_cmd: str | Sequence[str] | None = None,
        link_vars: str | None = None,
        num_runs: int = 3,
        server_ready_timeout: int = 300,
        show_stdout: bool = False,
        dry_run: bool = False,
        resume: bool = False,
        base_args: Sequence[str] = (),
    ) -> AdapterCommand:
        """Describe ``vllm bench sweep serve`` without executing it.

        ``result_path`` is the experiment directory, not a JSON file.  The official
        sweep command owns its nested server lifecycle, so callers must explicitly
        authorize commands whose ``manages_service_lifecycle`` field is true.
        """

        experiment_dir = _result_path(result_path)
        if experiment_dir.name in {"", ".", ".."}:
            raise AdapterError("sweep result_path must name an experiment directory")
        argv = [
            self.executable,
            "bench",
            "sweep",
            "serve",
            "--serve-cmd",
            _nested_command_text(serve_cmd, "serve_cmd"),
            "--bench-cmd",
            _nested_command_text(bench_cmd, "bench_cmd"),
            "--output-dir",
            str(experiment_dir.parent),
            "--experiment-name",
            experiment_dir.name,
            "--num-runs",
            str(_positive_integer(num_runs, "num_runs")),
            "--server-ready-timeout",
            str(_positive_integer(server_ready_timeout, "server_ready_timeout")),
        ]
        if serve_params is not None:
            argv.extend(("--serve-params", str(serve_params)))
        if bench_params is not None:
            argv.extend(("--bench-params", str(bench_params)))
        if after_bench_cmd is not None:
            argv.extend(
                ("--after-bench-cmd", _nested_command_text(after_bench_cmd, "after_bench_cmd"))
            )
        if link_vars is not None:
            argv.extend(("--link-vars", _nonempty(link_vars, "link_vars")))
        if show_stdout:
            argv.append("--show-stdout")
        if dry_run:
            argv.append("--dry-run")
        if resume:
            argv.append("--resume")
        append_base_args(argv, base_args)
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.SWEEP,
            argv=tuple(argv),
            result_path=experiment_dir,
            result_is_directory=True,
            manages_service_lifecycle=True,
        )

    # The official CLI names this subcommand "sweep serve"; expose both spellings.
    build_sweep_serve_command = build_sweep_command


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{name} must be a non-empty string without NUL bytes")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(f"{name} must be a non-negative integer")
    return value


def _percentile(value: object) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise AdapterError("metric percentiles must be finite numbers from 0 to 100")
    return str(value)


def _result_path(path: str | Path) -> Path:
    result = Path(path)
    if not str(result):
        raise AdapterError("result_path must not be empty")
    return result


def _nested_command(value: object, name: str) -> str | Sequence[str]:
    if isinstance(value, str):
        return _nonempty(value, name)
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return value
    raise AdapterError(f"{name} must be a command string or argv sequence")


def _nested_command_text(value: str | Sequence[str], name: str) -> str:
    if isinstance(value, str):
        return _nonempty(value, name)
    if isinstance(value, bytes) or not isinstance(value, Sequence) or not value:
        raise AdapterError(f"{name} must be a non-empty argv sequence")
    tokens: list[str] = []
    for index, token in enumerate(value):
        if not isinstance(token, str) or not token or "\x00" in token:
            raise AdapterError(f"{name}[{index}] must be a non-empty string without NUL bytes")
        tokens.append(token)
    return shlex.join(tokens)


__all__ = ["VLLMAdapter"]
