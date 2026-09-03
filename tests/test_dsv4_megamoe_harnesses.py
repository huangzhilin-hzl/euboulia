from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from euboulia.harnesses.sglang import (
    dsv4_megamoe_gate,
    dsv4_megamoe_profile_gate,
    dsv4_megamoe_report,
    gsm8k,
    prepare_sharegpt_exact,
)
from euboulia.harnesses.sglang.dsv4_megamoe_gate import (
    REQUIRED_ENVIRONMENT,
    StartupGateError,
    StartupGateSettings,
)


def _gate_settings(tmp_path: Path) -> StartupGateSettings:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    service_manifest = tmp_path / "service.json"
    stdout.write_text(
        "/src/launch_server.py:61: UserWarning: 'python -m sglang.launch_server' "
        "is still supported, but 'sglang serve' is the recommended entrypoint.\n"
        "Auto-detected DSV4 routed-expert layout: is_fp4_experts=True\n"
        "MegaMoE initialized; max_total_num_tokens=8835328; page_size=256\n"
        "Capturing batches (bs=64 avail_mem=10.22 GB): 0%\n"
        "Capturing batches (bs=1 avail_mem=7.87 GB): 100%\n",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    service_manifest.write_text('{"status":"ready"}\n', encoding="utf-8")
    return StartupGateSettings(
        endpoint="http://127.0.0.1:8188",
        pid=1234,
        stdout_path=stdout,
        stderr_path=stderr,
        service_manifest_path=service_manifest,
        evidence_dir=tmp_path / "gate-evidence",
        sglang_source=tmp_path / "SGLang",
        deepgemm_source=tmp_path / "DeepGEMM",
    )


def _launch_argv() -> tuple[str, ...]:
    argv = ["python3", "-u", "-m", "sglang.launch_server"]
    for name, value in dsv4_megamoe_gate.REQUIRED_VALUE_ARGUMENTS.items():
        argv.extend((name, value))
    argv.extend(sorted(dsv4_megamoe_gate.REQUIRED_FLAG_ARGUMENTS))
    return tuple(argv)


def test_dsv4_startup_gate_captures_exact_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _gate_settings(tmp_path)
    environment = dict(REQUIRED_ENVIRONMENT)
    environment["SGLANG_HOST_IP"] = "127.0.0.1"
    server_info = dict(dsv4_megamoe_gate.EXPECTED_SERVER_INFO)
    server_info.update(
        {
            "max_total_num_tokens": 8835328,
            "page_size": 256,
            "cuda_graph_config": {
                "decode": {"backend": "full", "max_bs": 64, "bs": [1, 64]},
                "prefill": {"backend": "disabled", "max_bs": 8192},
            },
            "internal_states": [
                {
                    "page_size": 256,
                    "memory_usage": {
                        "weight": 24.96,
                        "kvcache": 0,
                        "token_capacity": 8835328,
                        "graph": 2.54,
                    },
                }
                for _ in range(8)
            ],
        }
    )

    monkeypatch.setattr(
        dsv4_megamoe_gate,
        "_read_owned_process",
        lambda pid: (environment, _launch_argv()),
    )

    def fake_get(url: str) -> bytes:
        if url.endswith("/v1/models"):
            return b'{"data":[{"id":"dsv4"}]}'
        if url.endswith("/server_info"):
            return json.dumps(server_info).encode()
        if url.endswith("/metrics"):
            return b"sglang:spec_accept_length 4\n"
        return b"ok\n"

    monkeypatch.setattr(dsv4_megamoe_gate, "_http_get", fake_get)
    monkeypatch.setattr(
        dsv4_megamoe_gate,
        "_nvidia_smi",
        lambda: "".join(
            f"{index}, GPU-{index}, NVIDIA H20, 97871, 1000, 96871, 0\n"
            for index in range(8)
        ),
    )
    monkeypatch.setattr(
        dsv4_megamoe_gate,
        "_install_observation",
        lambda configured: {
            "sglang_commit": "a" * 40,
            "deep_gemm_commit": "5d32216686b982a39eabccca9419af430a60cfc2",
        },
    )

    result = dsv4_megamoe_gate.run_startup_gate(settings)

    assert result["status"] == "passed"
    assert result["gpu_count"] == 8
    assert (settings.evidence_dir / "server_info_initial.json").is_file()
    assert (settings.evidence_dir / "metrics_initial.prom").is_file()
    assert (settings.evidence_dir / "nvidia_smi_initial.csv").is_file()
    assert (settings.evidence_dir / "environment.txt").is_file()
    assert (settings.evidence_dir / "server_startup_summary.md").is_file()


def test_dsv4_startup_gate_rejects_humming_environment() -> None:
    environment = dict(REQUIRED_ENVIRONMENT)
    environment["SGLANG_HOST_IP"] = "127.0.0.1"
    environment["SGLANG_DSV4_FP4_EXPERTS"] = "1"

    with pytest.raises(StartupGateError, match="forbidden Humming"):
        dsv4_megamoe_gate._validate_environment(environment)


def test_dsv4_startup_gate_rejects_megamoe_fallback() -> None:
    text = (
        "Auto-detected DSV4 routed-expert layout: is_fp4_experts=True\n"
        "MegaMoE unavailable; falling back to ordinary MoE fallback\n"
    )

    with pytest.raises(StartupGateError, match="fatal or fallback"):
        dsv4_megamoe_gate._validate_startup_log(text, ())


def test_gsm8k_command_matches_formal_accuracy_protocol(tmp_path: Path) -> None:
    settings = gsm8k.GSM8KSettings(
        endpoint="http://127.0.0.1:8188",
        evidence_dir=tmp_path,
    )

    command = settings.command()

    assert command[command.index("--eval-name") + 1] == "gsm8k"
    assert command[command.index("--num-examples") + 1] == "200"
    assert command[command.index("--num-shots") + 1] == "8"
    assert command[command.index("--num-threads") + 1] == "64"
    assert command[command.index("--temperature") + 1] == "0"
    assert command[command.index("--max-tokens") + 1] == "512"


def test_kernel_trace_scanner_handles_chunk_boundaries(tmp_path: Path) -> None:
    trace = tmp_path / "rank-TP-0.trace.json.gz"
    with gzip.open(trace, "wb") as stream:
        stream.write(b"x" * (1024 * 1024 - 8))
        stream.write(b"fp8_mxfp4_mega_moe")

    assert dsv4_megamoe_profile_gate._gzip_contains(
        trace, b"fp8_mxfp4_mega_moe"
    )


def test_prepare_sharegpt_exact_is_exact_and_deterministic(tmp_path: Path) -> None:
    class CharacterTokenizer:
        def encode(self, text: str) -> list[int]:
            return [ord(character) for character in text]

        def __call__(
            self,
            text: str,
            *,
            return_offsets_mapping: bool,
            add_special_tokens: bool,
        ) -> dict[str, list[object]]:
            assert return_offsets_mapping is True
            assert add_special_tokens is True
            return {
                "input_ids": list(self.encode(text)),
                "offset_mapping": [(index, index + 1) for index in range(len(text))],
            }

    source = tmp_path / "sharegpt.json"
    source.write_text(
        json.dumps(
            [
                {
                    "conversations": [
                        {"from": "human", "value": "a" * 200},
                        {"from": "gpt", "value": "answer-a"},
                    ]
                },
                {
                    "conversations": [
                        {"from": "human", "value": "b" * 200},
                        {"from": "gpt", "value": "answer-b"},
                    ]
                },
            ]
        ),
        encoding="utf-8",
    )

    manifests = []
    for output_name in ("first", "second"):
        settings = prepare_sharegpt_exact.PreparationSettings(
            source=source,
            tokenizer_path=tmp_path / "model",
            output_dir=tmp_path / output_name,
            lengths=(4, 8),
            samples=2,
            seed=1,
        )
        manifest = prepare_sharegpt_exact.prepare_datasets(
            settings,
            tokenizer_loader=lambda _: CharacterTokenizer(),
        )
        manifests.append(manifest)
        for length in (4, 8):
            dataset_path = settings.output_dir / f"sharegpt_isl{length}_n2.json"
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            assert len(dataset) == 2
            assert {
                len(CharacterTokenizer().encode(row["conversations"][0]["value"]))
                for row in dataset
            } == {length}

    first_datasets = manifests[0]["datasets"]
    second_datasets = manifests[1]["datasets"]
    assert isinstance(first_datasets, dict)
    assert isinstance(second_datasets, dict)
    for length_key in ("4", "8"):
        assert first_datasets[length_key]["dataset_sha256"] == second_datasets[length_key][
            "dataset_sha256"
        ]
        assert first_datasets[length_key]["samples"] == second_datasets[length_key][
            "samples"
        ]

    with pytest.raises(FileExistsError, match="manifest already exists"):
        prepare_sharegpt_exact.prepare_datasets(
            prepare_sharegpt_exact.PreparationSettings(
                source=source,
                tokenizer_path=tmp_path / "model",
                output_dir=tmp_path / "first",
                lengths=(4,),
                samples=1,
            ),
            tokenizer_loader=lambda _: CharacterTokenizer(),
        )


def test_dsv4_report_requires_and_aggregates_complete_matrix(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    startup = target / "evaluations" / "first" / "startup-evidence"
    startup.mkdir(parents=True)
    install = {
        "sglang_commit": "a" * 40,
        "deep_gemm_commit": "5d32216686b982a39eabccca9419af430a60cfc2",
    }
    (startup / "startup_gate.json").write_text(json.dumps({"install": install}))
    startup_files = {
        "environment.txt": "env\n",
        "server_startup_summary.md": "# startup\n",
        "server_info_initial.json": "{}\n",
        "metrics_initial.prom": "metric 1\n",
        "nvidia_smi_initial.csv": "gpu\n",
        "models_initial.json": '{"data":[]}\n',
        "install_observation.json": json.dumps(install),
        "server_command.txt": "python3 -m sglang.launch_server\n",
        "server_startup_key_info.log": "memory\n",
        "server_startup_warnings_errors.log": "",
    }
    for name, content in startup_files.items():
        (startup / name).write_text(content)
    (target / "runtime-provenance.json").write_text(
        json.dumps(
            {
                "expected": {
                    "container": {"image": "registry.example/sglang:scenario"}
                }
            }
        )
    )
    kernel = target / "raw" / "megamoe_kernel_path_evidence.txt"
    kernel.parent.mkdir()
    kernel.write_text(
        "".join(
            f"PASS rank={rank} kernel=fp8_mxfp4_mega_moe trace=rank-{rank}\n"
            for rank in range(8)
        )
    )
    manifest = json.dumps({"source_sha256": "b" * 64}).encode()

    shapes = [
        (input_tokens, 256, 1)
        for input_tokens in sorted({16384, 32768, 65536, 131072, 262144})
    ]
    shapes.extend(
        (input_tokens, 1024, concurrency)
        for input_tokens in sorted({1024, 32768, 65536, 131072, 262144})
        for concurrency in (1, 2, 4, 8, 16)
    )
    for index, (input_tokens, output_tokens, concurrency) in enumerate(shapes):
        point = workspace / "workload-points" / f"point-{index}"
        evidence = tmp_path / "raw-evidence" / f"point-{index}"
        point.mkdir(parents=True)
        evidence.mkdir(parents=True)
        (evidence / "sharegpt_manifest.json").write_bytes(manifest)
        (evidence / "flush-after-warmup-1.txt").write_text("Cache flushed\n")
        samples = []
        for round_id in (1, 2, 3):
            (evidence / f"flush-before-round-{round_id}.txt").write_text("Cache flushed\n")
            (evidence / f"server_info-round-{round_id}.json").write_text("{}\n")
            (evidence / f"metrics-round-{round_id}.prom").write_text(
                "sglang:spec_accept_length{rank=\"0\"} 2.5\n"
                "sglang:spec_accept_rate{rank=\"0\"} 0.5\n"
            )
            samples.append(
                {
                    "dataset_name": "sharegpt",
                    "completed": concurrency,
                    "errors": [],
                    "input_lens": [input_tokens] * concurrency,
                    "output_lens": [output_tokens] * concurrency,
                    "mean_ttft_ms": 10.0 + round_id,
                    "mean_tpot_ms": 2.0 + round_id,
                    "mean_e2e_latency_ms": 20.0 + round_id,
                    "accept_length": 2.5,
                    "avg_spec_accept_length": 2.5,
                    "cache_report": {"cache_hit_rate_pct": 0.0},
                }
            )
        (point / "euboulia-result.json").write_text(
            json.dumps(
                {
                    "dataset": "sharegpt",
                    "warmups": 1,
                    "repetitions": 3,
                    "evidence_dir": str(evidence),
                    "samples": samples,
                }
            )
        )

    result = dsv4_megamoe_report.write_report(
        workspace, target, {"score": 0.91}
    )

    assert result["performance_rounds"] == 90
    assert result["zero_cache_hit_rounds"] == 90
    assert result["kernel_path_rank_count"] == 8
    assert (target / "summary_rounds.csv").is_file()
    assert (target / "summary_best.csv").is_file()
    assert (target / "summary.md").is_file()
    assert (target / "result_validation.json").is_file()
    assert (target / "sharegpt_manifest.json").read_bytes() == manifest
