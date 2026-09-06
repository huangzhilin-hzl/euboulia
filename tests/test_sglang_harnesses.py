from __future__ import annotations

import json
from pathlib import Path

import pytest

from euboulia.harnesses.sglang import benchmark, correctness
from euboulia.harnesses.sglang.benchmark import (
    BenchmarkHarnessError,
    BenchmarkSettings,
)
from euboulia.harnesses.sglang.correctness import (
    CorrectnessHarnessError,
    SmokeSettings,
)
from euboulia.optimization.evaluator import parse_metrics


def _benchmark_settings(**overrides: object) -> BenchmarkSettings:
    values: dict[str, object] = {
        "endpoint": "http://127.0.0.1:30000",
        "model": "zai-org/GLM-5.3-Flash",
        "input_tokens": 1024,
        "output_tokens": 256,
        "concurrency": 8,
        "num_prompts": 32,
        "dataset": "random",
        "warmups": 1,
        "repetitions": 3,
        "metrics_path": Path("euboulia-result.json"),
    }
    values.update(overrides)
    return BenchmarkSettings(**values)  # type: ignore[arg-type]


def test_smoke_settings_and_native_requests_use_runner_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SmokeSettings.from_environment(
        {
            "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:30000/",
            "EUBOULIA_MODEL": "zai-org/GLM-5.3-Flash",
            "EUBOULIA_SMOKE_REQUESTS": "2",
        }
    )
    observed: list[tuple[str, object, float]] = []

    def fake_post(url: str, payload: object, timeout: float) -> object:
        observed.append((url, payload, timeout))
        return {"text": "OK"}

    monkeypatch.setattr(correctness, "_post_json", fake_post)

    result = correctness.run_smoke(settings)

    assert result["completed"] == 2
    assert [item[0] for item in observed] == [
        "http://127.0.0.1:30000/generate",
        "http://127.0.0.1:30000/generate",
    ]
    assert observed[0][1] == {
        "text": "Reply with the single word OK.",
        "sampling_params": {"temperature": 0, "max_new_tokens": 8},
    }


def test_smoke_fails_closed_on_empty_openai_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SmokeSettings(
        endpoint="http://127.0.0.1:30000",
        model="zai-org/GLM-5.3-Flash",
        api="openai-chat",
        prompt="Reply OK",
        max_tokens=8,
        requests=1,
        timeout_seconds=30,
    )
    monkeypatch.setattr(
        correctness,
        "_post_json",
        lambda url, payload, timeout: {"choices": [{"message": {"content": ""}}]},
    )

    with pytest.raises(CorrectnessHarnessError, match="smoke request 1 failed"):
        correctness.run_smoke(settings)


def test_benchmark_command_is_fixed_length_and_saturation_oriented(tmp_path: Path) -> None:
    command = _benchmark_settings().command(tmp_path / "sample.jsonl")

    assert command[:3] == (command[0], "-m", "sglang.bench_serving")
    assert command[command.index("--random-input-len") + 1] == "1024"
    assert command[command.index("--random-output-len") + 1] == "256"
    assert command[command.index("--random-range-ratio") + 1] == "1.0"
    assert command[command.index("--request-rate") + 1] == "inf"
    assert command[command.index("--max-concurrency") + 1] == "8"
    assert command[command.index("--base-url") + 1] == "http://127.0.0.1:30000"
    assert "--flush-cache" in command
    assert "--disable-tqdm" in command
    assert "--output-details" in command


def test_benchmark_environment_uses_served_name_and_finite_request_rate() -> None:
    settings = BenchmarkSettings.from_environment(
        {
            "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:30000",
            "EUBOULIA_MODEL": "/models/target",
            "EUBOULIA_MODEL_SERVED_NAME": "target-served",
            "EUBOULIA_INPUT_TOKENS": "1024",
            "EUBOULIA_OUTPUT_TOKENS": "256",
            "EUBOULIA_CONCURRENCY": "8",
            "EUBOULIA_NUM_PROMPTS": "32",
            "EUBOULIA_DATASET": "random",
            "EUBOULIA_REPETITIONS": "3",
            "EUBOULIA_REQUEST_RATE": "12.5",
        }
    )

    command = settings.command(Path("sample.jsonl"))

    assert settings.model == "target-served"
    assert command[command.index("--model") + 1] == "target-served"
    assert command[command.index("--request-rate") + 1] == "12.5"


def test_benchmark_parameters_drive_seed_range_and_cache_policy() -> None:
    settings = BenchmarkSettings.from_environment(
        {
            "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:30000",
            "EUBOULIA_MODEL": "/models/target",
            "EUBOULIA_INPUT_TOKENS": "1024",
            "EUBOULIA_OUTPUT_TOKENS": "256",
            "EUBOULIA_CONCURRENCY": "8",
            "EUBOULIA_NUM_PROMPTS": "32",
            "EUBOULIA_DATASET": "random",
            "EUBOULIA_REPETITIONS": "3",
            "EUBOULIA_BENCHMARK_PARAMETERS": json.dumps(
                {"seed": 7, "random_range_ratio": 0.25, "flush_cache": False}
            ),
        }
    )

    command = settings.command(Path("sample.jsonl"))

    assert command[command.index("--seed") + 1] == "7"
    assert command[command.index("--random-range-ratio") + 1] == "0.25"
    assert "--flush-cache" not in command


def test_benchmark_parameters_reject_non_executable_labels() -> None:
    environment = {
        "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:30000",
        "EUBOULIA_MODEL": "/models/target",
        "EUBOULIA_INPUT_TOKENS": "1024",
        "EUBOULIA_OUTPUT_TOKENS": "256",
        "EUBOULIA_CONCURRENCY": "8",
        "EUBOULIA_NUM_PROMPTS": "32",
        "EUBOULIA_DATASET": "random",
        "EUBOULIA_REPETITIONS": "3",
        "EUBOULIA_BENCHMARK_PARAMETERS": json.dumps(
            {"protocol": "fixed-random-cache-clean"}
        ),
    }

    with pytest.raises(BenchmarkHarnessError, match="unsupported benchmark parameter"):
        BenchmarkSettings.from_environment(environment)


def test_benchmark_discards_warmups_and_writes_median_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _benchmark_settings()
    samples = iter(
        [
            {
                **sample,
                "input_lens": [1024] * 32,
                "output_lens": [256] * 32,
            }
            for sample in [
                {"completed": 32, "output_throughput": 10.0, "mean_ttft_ms": 90.0},
                {"completed": 32, "output_throughput": 100.0, "mean_ttft_ms": 30.0},
                {"completed": 32, "output_throughput": 90.0, "mean_ttft_ms": 40.0},
                {"completed": 32, "output_throughput": 120.0, "mean_ttft_ms": 20.0},
            ]
        ]
    )

    result = benchmark.run_benchmark(
        settings,
        sample_runner=lambda configured, output: next(samples),
    )

    assert result["aggregation"] == "median"
    assert result["metrics"] == {
        "completed": 32.0,
        "mean_ttft_ms": 30.0,
        "output_throughput": 100.0,
    }
    written = json.loads(settings.metrics_path.read_text(encoding="utf-8"))
    assert written == result
    assert parse_metrics(settings.metrics_path)["output_throughput"] == 100.0
    assert len(written["samples"]) == 3
    assert all(sample["output_throughput"] != 10.0 for sample in written["samples"])


@pytest.mark.parametrize("key", ["input_lens", "output_lens"])
@pytest.mark.parametrize("bad_values", [None, [], [38], [True]])
def test_fixed_length_benchmark_rejects_missing_or_wrong_request_lengths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, bad_values: object
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _benchmark_settings(num_prompts=1, warmups=0, repetitions=1)
    sample = {
        "completed": 1,
        "output_throughput": 100.0,
        "input_lens": [1024],
        "output_lens": [256],
        key: bad_values,
    }
    with pytest.raises(BenchmarkHarnessError, match=key):
        benchmark.run_benchmark(settings, sample_runner=lambda configured, output: sample)
    assert not settings.metrics_path.exists()


def test_benchmark_fails_when_any_request_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _benchmark_settings(warmups=0, repetitions=1)

    with pytest.raises(BenchmarkHarnessError, match="completed 31/32 requests"):
        benchmark.run_benchmark(
            settings,
            sample_runner=lambda configured, output: {
                "completed": 31,
                "output_throughput": 100.0,
            },
        )


def test_benchmark_rejects_metrics_symlink_outside_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (worktree / "results").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(worktree)
    settings = _benchmark_settings(
        warmups=0,
        repetitions=1,
        metrics_path=Path("results/euboulia-result.json"),
    )

    with pytest.raises(BenchmarkHarnessError, match="must remain inside the worktree"):
        benchmark.run_benchmark(
            settings,
            sample_runner=lambda configured, output: {
                "completed": 32,
                "output_throughput": 100.0,
            },
        )


def test_benchmark_environment_contract_rejects_unknown_dataset() -> None:
    with pytest.raises(BenchmarkHarnessError, match="dataset=random, random-ids, or sharegpt"):
        BenchmarkSettings.from_environment(
            {
                "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:30000",
                "EUBOULIA_MODEL": "zai-org/GLM-5.3-Flash",
                "EUBOULIA_INPUT_TOKENS": "1024",
                "EUBOULIA_OUTPUT_TOKENS": "256",
                "EUBOULIA_CONCURRENCY": "8",
                "EUBOULIA_NUM_PROMPTS": "32",
                "EUBOULIA_DATASET": "synthetic",
                "EUBOULIA_REPETITIONS": "3",
            }
        )


def test_sharegpt_command_uses_native_dataset_path(tmp_path: Path) -> None:
    dataset_path = tmp_path / "sharegpt.json"
    settings = BenchmarkSettings.from_environment(
        {
            "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:8188",
            "EUBOULIA_MODEL": "/models/dsv4",
            "EUBOULIA_INPUT_TOKENS": "16384",
            "EUBOULIA_OUTPUT_TOKENS": "256",
            "EUBOULIA_CONCURRENCY": "1",
            "EUBOULIA_NUM_PROMPTS": "1",
            "EUBOULIA_DATASET": "sharegpt",
            "EUBOULIA_REPETITIONS": "3",
            "EUBOULIA_DATASET_PATH": str(dataset_path),
        }
    )

    command = settings.command(tmp_path / "round.jsonl")

    assert command[:3] == (command[0], "-m", "sglang.bench_serving")
    assert command[command.index("--dataset-path") + 1] == str(dataset_path)
    assert command[command.index("--sharegpt-output-len") + 1] == "256"
    assert command[command.index("--sharegpt-context-len") + 1] == "16640"
    assert "--flush-cache" in command
    assert "--output-details" in command
    assert "--random-input-len" not in command


def test_random_ids_uses_standard_random_length_options() -> None:
    settings = BenchmarkSettings.from_environment(
        {
            "EUBOULIA_TARGET_ENDPOINT": "http://127.0.0.1:8188",
            "EUBOULIA_MODEL": "/models/target",
            "EUBOULIA_INPUT_TOKENS": "4096",
            "EUBOULIA_OUTPUT_TOKENS": "512",
            "EUBOULIA_CONCURRENCY": "4",
            "EUBOULIA_NUM_PROMPTS": "16",
            "EUBOULIA_DATASET": "random-ids",
            "EUBOULIA_REPETITIONS": "1",
            "EUBOULIA_BENCHMARK_PARAMETERS": json.dumps(
                {"random_range_ratio": 0}
            ),
        }
    )

    command = settings.command(Path("round.jsonl"))

    assert command[command.index("--dataset-name") + 1] == "random-ids"
    assert command[command.index("--random-input-len") + 1] == "4096"
    assert command[command.index("--random-output-len") + 1] == "512"
    assert command[command.index("--random-range-ratio") + 1] == "0.0"
