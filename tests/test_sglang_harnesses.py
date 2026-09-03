from __future__ import annotations

import hashlib
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


def _sharegpt_contract(
    tmp_path: Path,
    *,
    input_tokens: int = 1024,
    samples: int = 16,
    seed: int = 42,
) -> tuple[Path, Path]:
    dataset = [
        {
            "id": str(index),
            "conversations": [
                {"from": "human", "value": f"unique prompt {index}"},
                {"from": "gpt", "value": f"answer {index}"},
            ],
        }
        for index in range(samples)
    ]
    dataset_path = tmp_path / f"sharegpt_isl{input_tokens}_n{samples}.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    metadata = []
    for index, row in enumerate(dataset):
        prompt = row["conversations"][0]["value"]
        metadata.append(
            {
                "sample_index": index,
                "input_tokens": input_tokens,
                "prompt_utf8_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "prompt_token_ids_sha256": hashlib.sha256(
                    f"tokens-{index}".encode()
                ).hexdigest(),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_sha256": "a" * 64,
                "seed": seed,
                "samples_per_length": samples,
                "datasets": {
                    str(input_tokens): {
                        "dataset_sha256": hashlib.sha256(
                            dataset_path.read_bytes()
                        ).hexdigest(),
                        "samples": metadata,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return dataset_path, manifest_path


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

    assert command[:3] == (
        command[0],
        "-m",
        "sglang.benchmark.serving",
    )
    assert command[command.index("--random-input-len") + 1] == "1024"
    assert command[command.index("--random-output-len") + 1] == "256"
    assert command[command.index("--random-range-ratio") + 1] == "1.0"
    assert command[command.index("--request-rate") + 1] == "inf"
    assert command[command.index("--max-concurrency") + 1] == "8"
    assert command[command.index("--base-url") + 1] == "http://127.0.0.1:30000"
    assert "--flush-cache" in command
    assert "--disable-tqdm" in command


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


def test_benchmark_discards_warmups_and_writes_median_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _benchmark_settings()
    samples = iter(
        [
            {"completed": 32, "output_throughput": 10.0, "mean_ttft_ms": 90.0},
            {"completed": 32, "output_throughput": 100.0, "mean_ttft_ms": 30.0},
            {"completed": 32, "output_throughput": 90.0, "mean_ttft_ms": 40.0},
            {"completed": 32, "output_throughput": 120.0, "mean_ttft_ms": 20.0},
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
    with pytest.raises(BenchmarkHarnessError, match="dataset=random or dataset=sharegpt"):
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


def test_sharegpt_command_uses_exact_dataset_contract(tmp_path: Path) -> None:
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
            "EUBOULIA_SHAREGPT_DATASET_ROOT": str(tmp_path),
        }
    )

    command = settings.command(tmp_path / "round.jsonl")

    assert command[:3] == (command[0], "-m", "sglang.bench_serving")
    assert command[command.index("--dataset-path") + 1].endswith(
        "sharegpt_isl16384_n16.json"
    )
    assert command[command.index("--sharegpt-output-len") + 1] == "256"
    assert command[command.index("--sharegpt-context-len") + 1] == "16640"
    assert command[command.index("--warmup-requests") + 1] == "0"
    assert "--cache-report" in command
    assert "--output-details" in command
    assert "--random-input-len" not in command


def test_sharegpt_benchmark_preserves_flush_snapshots_and_best_average(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dataset_path, manifest_path = _sharegpt_contract(tmp_path)
    settings = _benchmark_settings(
        input_tokens=1024,
        output_tokens=256,
        concurrency=1,
        num_prompts=1,
        dataset="sharegpt",
        dataset_path=dataset_path,
        dataset_manifest_path=manifest_path,
        warmups=1,
        repetitions=3,
    )
    samples = iter(
        [
            {
                "dataset_name": "sharegpt",
                "completed": 1,
                "errors": [],
                "input_lens": [1024],
                "output_lens": [256],
                "output_throughput": 10.0,
                "mean_ttft_ms": 99.0,
            },
            *[
                {
                    "dataset_name": "sharegpt",
                    "completed": 1,
                    "errors": [],
                    "input_lens": [1024],
                    "output_lens": [256],
                    "output_throughput": throughput,
                    "mean_ttft_ms": ttft,
                    "cache_report": {"cache_hit_rate_pct": 0.0},
                }
                for throughput, ttft in ((20.0, 30.0), (30.0, 20.0), (10.0, 40.0))
            ],
        ]
    )

    def fake_flush(configured: BenchmarkSettings, path: Path) -> None:
        path.write_text("Cache flushed\n")

    def fake_snapshot(configured: BenchmarkSettings, evidence: Path, tag: str) -> None:
        (evidence / f"server_info-{tag}.json").write_text("{}")
        (evidence / f"metrics-{tag}.prom").write_text("metric 1\n")

    monkeypatch.setattr(benchmark, "_flush_cache", fake_flush)
    monkeypatch.setattr(benchmark, "_capture_snapshot", fake_snapshot)

    result = benchmark.run_benchmark(
        settings,
        sample_runner=lambda configured, output: next(samples),
    )

    evidence = tmp_path / "euboulia-result-evidence"
    assert result["metrics"]["mean_ttft_ms"] == 30.0  # type: ignore[index]
    assert result["average_metrics"]["mean_ttft_ms"] == 30.0  # type: ignore[index]
    assert result["best_metrics"]["mean_ttft_ms"] == 20.0  # type: ignore[index]
    assert len(list(evidence.glob("flush-before-round-*.txt"))) == 3
    assert len(list(evidence.glob("metrics-round-*.prom"))) == 3
    assert (evidence / "flush-after-warmup-1.txt").is_file()
    assert (evidence / "sharegpt_manifest.json").is_file()
    assert (evidence / "evidence-manifest.json").is_file()


def test_sharegpt_benchmark_fails_on_nonzero_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dataset_path, manifest_path = _sharegpt_contract(tmp_path)
    settings = _benchmark_settings(
        input_tokens=1024,
        output_tokens=256,
        concurrency=1,
        num_prompts=1,
        dataset="sharegpt",
        dataset_path=dataset_path,
        dataset_manifest_path=manifest_path,
        warmups=0,
        repetitions=1,
    )
    monkeypatch.setattr(
        benchmark,
        "_flush_cache",
        lambda configured, path: path.write_text("Cache flushed\n"),
    )

    with pytest.raises(BenchmarkHarnessError, match=r"expected 0\.0%"):
        benchmark.run_benchmark(
            settings,
            sample_runner=lambda configured, output: {
                "dataset_name": "sharegpt",
                "completed": 1,
                "errors": [],
                "input_lens": [1024],
                "output_lens": [256],
                "output_throughput": 10.0,
                "cache_report": {"cache_hit_rate_pct": 1.25},
            },
        )
