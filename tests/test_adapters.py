from __future__ import annotations

import json
from pathlib import Path

import pytest

from euboulia.adapters import (
    AdapterError,
    BenchmarkType,
    SGLangAdapter,
    VLLMAdapter,
)


def _option(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_sglang_builds_public_serving_benchmark_argv(tmp_path: Path) -> None:
    result_path = tmp_path / "sglang.jsonl"
    command = SGLangAdapter(python_executable="python3").build_serve_command(
        model="org/model",
        result_path=result_path,
        base_url="http://127.0.0.1:30000",
        dataset_name="random",
        num_prompts=64,
        random_input_len=128,
        random_output_len=32,
        random_range_ratio=0,
        request_rate="inf",
        max_concurrency=16,
        output_details=True,
        parameters={"seed": 42},
        base_args=("--disable-tqdm",),
    )

    assert command.argv[:3] == ("python3", "-m", "sglang.bench_serving")
    assert _option(command.argv, "--output-file") == str(result_path)
    assert _option(command.argv, "--request-rate") == "inf"
    assert _option(command.argv, "--random-range-ratio") == "0"
    assert _option(command.argv, "--seed") == "42"
    assert "--output-details" in command.argv
    assert "--disable-tqdm" in command.argv
    assert command.result_path == result_path
    assert not command.manages_service_lifecycle
    assert command.as_argv() == list(command.argv)


def test_sglang_generic_command_maps_framework_neutral_workload(tmp_path: Path) -> None:
    workload = {
        "model": "org/model",
        "endpoint": "http://localhost:30000",
        "dataset": "random",
        "input_tokens": 256,
        "output_tokens": 64,
        "concurrency": 8,
        "num_prompts": 20,
    }
    command = SGLangAdapter().build_command(
        "serve",
        workload,
        {"max_concurrency": 12, "seed": 7},
        ["--ignore-eos"],
        tmp_path / "result.jsonl",
    )

    assert command.benchmark_type is BenchmarkType.SERVE
    assert _option(command.argv, "--base-url") == workload["endpoint"]
    assert command.argv.count("--max-concurrency") == 2
    assert command.argv[-4:] == ("--max-concurrency", "12", "--seed", "7")


def test_sglang_rejects_non_serving_mode_and_conflicting_address() -> None:
    with pytest.raises(AdapterError, match="only"):
        SGLangAdapter().build_command("latency", {"model": "m"}, {}, (), Path("result.json"))
    with pytest.raises(AdapterError, match="cannot be combined"):
        SGLangAdapter().build_serve_command(
            model="m",
            result_path="result.jsonl",
            base_url="http://localhost:30000",
            host="localhost",
        )


def test_sglang_parser_reads_last_appended_jsonl_record(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    records = [
        {"completed": 8, "num_prompts": 10, "output_throughput": 100.0},
        {
            "completed": 10,
            "failed": 0,
            "duration": 2.0,
            "output_throughput": 125.5,
            "latency": {"p99_ms": 40.0},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    parsed = SGLangAdapter().parse_result(path)

    assert parsed.raw == records
    assert parsed.selected_record == records[-1]
    assert len(parsed.records) == 2
    assert parsed.metrics["output_throughput"] == 125.5
    assert parsed.metrics["latency.p99_ms"] == 40.0
    assert parsed.metrics["duration_seconds"] == 2.0
    assert parsed.metrics["success_rate"] == 1.0


def test_vllm_serve_command_uses_result_directory_and_filename(tmp_path: Path) -> None:
    result_path = tmp_path / "nested" / "serve.json"
    command = VLLMAdapter().build_serve_command(
        model="org/model",
        result_path=result_path,
        base_url="http://127.0.0.1:8000",
        random_input_len=128,
        random_output_len=32,
        max_concurrency=4,
        save_detailed=True,
    )

    assert command.argv[:3] == ("vllm", "bench", "serve")
    assert "--save-result" in command.argv
    assert "--save-detailed" in command.argv
    assert _option(command.argv, "--result-dir") == str(result_path.parent)
    assert _option(command.argv, "--result-filename") == result_path.name
    assert _option(command.argv, "--percentile-metrics") == "ttft,tpot,itl,e2el"
    assert _option(command.argv, "--metric-percentiles") == "50,90,99"


def test_vllm_latency_and_throughput_commands_are_stable_public_clis(
    tmp_path: Path,
) -> None:
    adapter = VLLMAdapter(executable="/opt/bin/vllm")
    latency = adapter.build_latency_command(
        model="m",
        result_path=tmp_path / "latency.json",
        input_len=16,
        output_len=8,
        batch_size=2,
        num_iters_warmup=1,
        num_iters=3,
    )
    throughput = adapter.build_throughput_command(
        model="m",
        result_path=tmp_path / "throughput.json",
        input_len=16,
        output_len=8,
        num_prompts=10,
    )

    assert latency.argv[:3] == ("/opt/bin/vllm", "bench", "latency")
    assert _option(latency.argv, "--output-json") == str(tmp_path / "latency.json")
    assert throughput.argv[:3] == ("/opt/bin/vllm", "bench", "throughput")
    assert _option(throughput.argv, "--output-json") == str(tmp_path / "throughput.json")


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        ("serve", ("vllm", "bench", "serve")),
        ("latency", ("vllm", "bench", "latency")),
        ("throughput", ("vllm", "bench", "throughput")),
    ],
)
def test_vllm_generic_dispatch(tmp_path: Path, mode: str, prefix: tuple[str, ...]) -> None:
    workload = {
        "model": "m",
        "endpoint": "http://localhost:8000",
        "dataset": "random",
        "input_tokens": 32,
        "output_tokens": 8,
        "concurrency": 2,
        "num_prompts": 10,
    }

    command = VLLMAdapter().build_command(
        mode, workload, {"seed": 42}, ("--ignore-eos",), tmp_path / f"{mode}.json"
    )

    assert command.argv[:3] == prefix
    assert "--seed" in command.argv
    assert "--ignore-eos" in command.argv


def test_vllm_sweep_command_is_flagged_as_lifecycle_managing(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "sweep-01"
    command = VLLMAdapter().build_sweep_command(
        serve_cmd=["vllm", "serve", "org/model", "--port", "8000"],
        bench_cmd=["vllm", "bench", "serve", "--model", "org/model"],
        result_path=experiment_dir,
        serve_params=tmp_path / "serve-params.json",
        bench_params=tmp_path / "bench-params.json",
        num_runs=2,
        dry_run=True,
    )

    assert command.argv[:4] == ("vllm", "bench", "sweep", "serve")
    assert _option(command.argv, "--serve-cmd") == "vllm serve org/model --port 8000"
    assert _option(command.argv, "--output-dir") == str(tmp_path)
    assert _option(command.argv, "--experiment-name") == "sweep-01"
    assert "--dry-run" in command.argv
    assert command.result_path == experiment_dir
    assert command.result_is_directory
    assert command.manages_service_lifecycle


def test_vllm_generic_sweep_requires_nested_commands(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="serve_cmd and bench_cmd"):
        VLLMAdapter().build_command("sweep", {"model": "m"}, {}, (), tmp_path / "experiment")


def test_vllm_parser_keeps_raw_json_and_flattens_metrics(tmp_path: Path) -> None:
    path = tmp_path / "serve.json"
    payload = {
        "completed": 5,
        "num_prompts": 5,
        "metrics": {
            "output_throughput": 44.0,
            "request_throughput": 3.0,
            "ignored_boolean": True,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    parsed = VLLMAdapter().parse_result(path, BenchmarkType.SERVE)

    assert parsed.raw == payload
    assert parsed.metrics["metrics.output_throughput"] == 44.0
    assert parsed.metrics["output_throughput"] == 44.0
    assert parsed.metrics["request_throughput"] == 3.0
    assert parsed.metrics["success_rate"] == 1.0
    assert "metrics.ignored_boolean" not in parsed.metrics


@pytest.mark.parametrize("contents", ["", "not-json", "[]", "[1, 2]"])
def test_parser_fails_closed_on_malformed_payload(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(AdapterError):
        VLLMAdapter().parse_result(path)


def test_result_output_options_cannot_be_overridden() -> None:
    with pytest.raises(AdapterError, match="managed by the adapter"):
        VLLMAdapter().build_serve_command(
            model="m",
            result_path="result.json",
            parameters={"result_filename": "elsewhere.json"},
        )


def test_parameter_names_and_nested_values_fail_closed() -> None:
    adapter = VLLMAdapter()
    with pytest.raises(AdapterError, match="parameter name"):
        adapter.build_serve_command(
            model="m",
            result_path="result.json",
            parameters={1: "value"},  # type: ignore[dict-item]
        )
    with pytest.raises(AdapterError, match="JSON-compatible"):
        adapter.build_serve_command(
            model="m",
            result_path="result.json",
            parameters={"extra_request_body": {"bad": object()}},
        )


@pytest.mark.parametrize("adapter", [SGLangAdapter(), VLLMAdapter()])
@pytest.mark.parametrize(
    "base_args",
    [
        ("--profile",),
        ("--profile-output-dir=/tmp/profile",),
    ],
)
def test_serving_profile_control_base_args_fail_closed(
    adapter: SGLangAdapter | VLLMAdapter, base_args: tuple[str, ...]
) -> None:
    with pytest.raises(AdapterError, match="benchmark adapter"):
        adapter.build_serve_command(
            model="m",
            result_path="result.json",
            base_args=base_args,
        )


@pytest.mark.parametrize("adapter", [SGLangAdapter(), VLLMAdapter()])
@pytest.mark.parametrize("parameter", ["profile", "profile_output_dir", "profile-steps"])
def test_serving_profile_control_parameters_fail_closed(
    adapter: SGLangAdapter | VLLMAdapter, parameter: str
) -> None:
    with pytest.raises(AdapterError, match="benchmark adapter"):
        adapter.build_serve_command(
            model="m",
            result_path="result.json",
            parameters={parameter: True},
        )


@pytest.mark.parametrize("mode", [BenchmarkType.LATENCY, BenchmarkType.THROUGHPUT])
def test_vllm_offline_benchmark_profile_controls_fail_closed(
    mode: BenchmarkType, tmp_path: Path
) -> None:
    workload = {
        "model": "m",
        "input_tokens": 8,
        "output_tokens": 8,
        "concurrency": 1,
        "num_prompts": 2,
    }

    with pytest.raises(AdapterError, match="benchmark adapter"):
        VLLMAdapter().build_command(
            mode,
            workload,
            {"profile": True},
            (),
            tmp_path / "result.json",
        )
