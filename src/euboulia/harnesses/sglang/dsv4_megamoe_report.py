"""Strict result aggregation for the DS-V4-Flash MegaMoE scenario."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast


class ScenarioReportError(RuntimeError):
    """Raised when the formal scenario evidence is incomplete or inconsistent."""


TTFT_INPUTS = frozenset({16384, 32768, 65536, 131072, 262144})
TPOT_INPUTS = frozenset({1024, 32768, 65536, 131072, 262144})
TPOT_CONCURRENCIES = frozenset({1, 2, 4, 8, 16})


def write_report(
    workspace: Path,
    target_artifact_dir: Path,
    accuracy: Mapping[str, object],
) -> dict[str, object]:
    """Validate 90 formal rounds and materialize the plan's canonical summaries."""

    point_files = sorted((workspace / "workload-points").glob("*/euboulia-result.json"))
    if len(point_files) != 30:
        raise ScenarioReportError(f"expected 30 workload points, observed {len(point_files)}")
    startup_path = _one(target_artifact_dir, "startup_gate.json")
    startup = _read_object(startup_path)
    install = startup.get("install")
    if not isinstance(install, dict):
        raise ScenarioReportError("startup gate has no install observation")
    sglang_commit = _required_string(install, "sglang_commit")
    deepgemm_commit = _required_string(install, "deep_gemm_commit")
    runtime = _read_object(target_artifact_dir / "runtime-provenance.json")
    image = _declared_image(runtime)

    performance_rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    manifest_digest: str | None = None
    manifest_bytes: bytes | None = None
    observed_shapes: set[tuple[str, int, int, int]] = set()
    for metrics_path in point_files:
        point_id = metrics_path.parent.name
        payload = _read_object(metrics_path)
        if payload.get("dataset") != "sharegpt":
            raise ScenarioReportError(f"{point_id} is not an exact ShareGPT result")
        if payload.get("warmups") != 1 or payload.get("repetitions") != 3:
            raise ScenarioReportError(f"{point_id} does not use 1 warmup and 3 rounds")
        samples = payload.get("samples")
        if not isinstance(samples, list) or len(samples) != 3:
            raise ScenarioReportError(f"{point_id} does not contain three measured samples")
        evidence_value = payload.get("evidence_dir")
        if not isinstance(evidence_value, str):
            raise ScenarioReportError(f"{point_id} has no evidence directory")
        evidence_dir = Path(evidence_value)
        _validate_round_evidence(evidence_dir)
        current_manifest = (evidence_dir / "sharegpt_manifest.json").read_bytes()
        current_digest = hashlib.sha256(current_manifest).hexdigest()
        if manifest_digest is None:
            manifest_digest = current_digest
            manifest_bytes = current_manifest
        elif current_digest != manifest_digest:
            raise ScenarioReportError("workload points used different ShareGPT manifests")

        first = _sample_object(samples[0], point_id)
        input_tokens = _exact_integer(first, "input_lens", point_id)
        output_tokens = _exact_integer(first, "output_lens", point_id)
        concurrency = _positive_integer(first.get("completed"), f"{point_id}.completed")
        case_type = _case_type(input_tokens, output_tokens, concurrency)
        shape = (case_type, input_tokens, output_tokens, concurrency)
        if shape in observed_shapes:
            raise ScenarioReportError(f"duplicate workload shape: {shape}")
        observed_shapes.add(shape)
        point_rows: list[dict[str, object]] = []
        for round_index, raw_sample in enumerate(samples, start=1):
            sample = _sample_object(raw_sample, point_id)
            _validate_sample_shape(sample, input_tokens, output_tokens, concurrency, point_id)
            cache_hit_rate = _cache_hit_rate(sample, point_id)
            metrics_snapshot = evidence_dir / f"metrics-round-{round_index}.prom"
            row = {
                "image": image,
                "sglang_commit": sglang_commit,
                "deepgemm_commit": deepgemm_commit,
                "case_type": case_type,
                "input_len": input_tokens,
                "output_len": output_tokens,
                "concurrency": concurrency,
                "round": round_index,
                "metric": _finite(
                    sample,
                    "mean_ttft_ms" if case_type == "ttft" else "mean_tpot_ms",
                    point_id,
                ),
                "mean_ttft_ms": _finite(sample, "mean_ttft_ms", point_id),
                "mean_tpot_ms": _finite(sample, "mean_tpot_ms", point_id),
                "mean_e2e_latency_ms": _finite(sample, "mean_e2e_latency_ms", point_id),
                "accept_length": _finite(sample, "accept_length", point_id),
                "avg_spec_accept_length": _avg_spec_accept_length(sample, point_id),
                "spec_accept_length": _prometheus_mean(
                    metrics_snapshot, "sglang:spec_accept_length"
                ),
                "spec_accept_rate": _prometheus_mean(
                    metrics_snapshot, "sglang:spec_accept_rate"
                ),
                "cache_hit_rate_pct": cache_hit_rate,
                "gsm8k_score": "",
                "status": "ok",
                "error": "",
            }
            performance_rows.append(row)
            point_rows.append(row)
        summaries.append(_summarize_shape(point_rows))

    _validate_shape_matrix(observed_shapes)
    kernel_evidence = target_artifact_dir / "raw" / "megamoe_kernel_path_evidence.txt"
    kernel_lines = kernel_evidence.read_text(encoding="utf-8").splitlines()
    kernel_ranks = {
        int(match.group(1))
        for line in kernel_lines
        if (
            match := re.fullmatch(
                r"PASS rank=(\d+) kernel=fp8_mxfp4_mega_moe trace=.+", line
            )
        )
    }
    if len(kernel_lines) != 8 or kernel_ranks != set(range(8)):
        raise ScenarioReportError("SM90 MegaMoE kernel evidence does not cover all eight ranks")
    if manifest_bytes is None or manifest_digest is None:  # pragma: no cover - points are nonempty
        raise AssertionError("manifest aggregation produced no result")

    score = _finite(accuracy, "score", "accuracy")
    accuracy_row: dict[str, object] = {key: "" for key in performance_rows[0]}
    accuracy_row.update(
        {
            "image": image,
            "sglang_commit": sglang_commit,
            "deepgemm_commit": deepgemm_commit,
            "case_type": "accuracy",
            "round": 1,
            "metric": score,
            "gsm8k_score": score,
            "status": "ok",
            "error": "",
        }
    )
    summary_accuracy: dict[str, object] = {key: "" for key in summaries[0]}
    summary_accuracy.update(
        {
            "case_type": "accuracy",
            "rounds": 1,
            "gsm8k_score": score,
        }
    )
    rows: list[Mapping[str, object]] = [*performance_rows, accuracy_row]
    summary_rows: list[Mapping[str, object]] = [*summaries, summary_accuracy]
    _write_csv(target_artifact_dir / "summary_rounds.csv", rows)
    _write_csv(target_artifact_dir / "summary_best.csv", summary_rows)
    (target_artifact_dir / "sharegpt_manifest.json").write_bytes(manifest_bytes)
    _copy_startup_artifacts(startup_path.parent, target_artifact_dir)

    validation = {
        "status": "passed",
        "dataset": "sharegpt",
        "performance_rounds": len(performance_rows),
        "ttft_rounds": sum(row["case_type"] == "ttft" for row in performance_rows),
        "tpot_rounds": sum(row["case_type"] == "tpot" for row in performance_rows),
        "accuracy_rounds": 1,
        "workload_shapes": len(summaries),
        "formal_flush_success": len(performance_rows),
        "warmup_flush_success": len(summaries),
        "zero_cache_hit_rounds": sum(
            row["cache_hit_rate_pct"] == 0.0 for row in performance_rows
        ),
        "kernel_path_rank_count": len(kernel_lines),
        "gsm8k_score": score,
        "sharegpt_manifest_sha256": manifest_digest,
    }
    (target_artifact_dir / "result_validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (target_artifact_dir / "summary.md").write_text(
        _markdown_summary(validation, summaries, image, sglang_commit, deepgemm_commit),
        encoding="utf-8",
    )
    return validation


def _validate_round_evidence(evidence_dir: Path) -> None:
    if not evidence_dir.is_dir():
        raise ScenarioReportError(f"benchmark evidence directory is missing: {evidence_dir}")
    warmup_flushes = list(evidence_dir.glob("flush-after-warmup-*.txt"))
    formal_flushes = list(evidence_dir.glob("flush-before-round-*.txt"))
    snapshots = list(evidence_dir.glob("metrics-round-*.prom"))
    server_infos = list(evidence_dir.glob("server_info-round-*.json"))
    if len(warmup_flushes) != 1 or len(formal_flushes) != 3:
        raise ScenarioReportError(f"invalid flush evidence in {evidence_dir}")
    if len(snapshots) != 3 or len(server_infos) != 3:
        raise ScenarioReportError(f"invalid server snapshot evidence in {evidence_dir}")
    for path in (*warmup_flushes, *formal_flushes):
        if "Cache flushed" not in path.read_text(encoding="utf-8", errors="replace"):
            raise ScenarioReportError(f"cache flush did not succeed: {path}")


def _sample_object(value: object, point_id: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ScenarioReportError(f"{point_id} contains a non-object sample")
    return cast(dict[str, object], value)


def _exact_integer(sample: Mapping[str, object], key: str, point_id: str) -> int:
    values = sample.get(key)
    if not isinstance(values, list) or not values:
        raise ScenarioReportError(f"{point_id}.{key} must be a non-empty list")
    first = _positive_integer(values[0], f"{point_id}.{key}[0]")
    if any(value != first for value in values):
        raise ScenarioReportError(f"{point_id}.{key} is not exact")
    return first


def _validate_sample_shape(
    sample: Mapping[str, object],
    input_tokens: int,
    output_tokens: int,
    concurrency: int,
    point_id: str,
) -> None:
    if sample.get("dataset_name") != "sharegpt":
        raise ScenarioReportError(f"{point_id} sample is not ShareGPT")
    errors = sample.get("errors")
    if sample.get("completed") != concurrency or (
        isinstance(errors, list) and any(errors)
    ):
        raise ScenarioReportError(f"{point_id} sample is incomplete or has errors")
    if sample.get("input_lens") != [input_tokens] * concurrency:
        raise ScenarioReportError(f"{point_id} sample input lengths are not exact")
    if sample.get("output_lens") != [output_tokens] * concurrency:
        raise ScenarioReportError(f"{point_id} sample output lengths are not exact")


def _case_type(input_tokens: int, output_tokens: int, concurrency: int) -> str:
    if output_tokens == 256 and concurrency == 1 and input_tokens in TTFT_INPUTS:
        return "ttft"
    if output_tokens == 1024 and input_tokens in TPOT_INPUTS and concurrency in TPOT_CONCURRENCIES:
        return "tpot"
    raise ScenarioReportError(
        f"unexpected workload shape: input={input_tokens}, output={output_tokens}, c={concurrency}"
    )


def _validate_shape_matrix(shapes: set[tuple[str, int, int, int]]) -> None:
    expected = {("ttft", input_tokens, 256, 1) for input_tokens in TTFT_INPUTS}
    expected.update(
        ("tpot", input_tokens, 1024, concurrency)
        for input_tokens in TPOT_INPUTS
        for concurrency in TPOT_CONCURRENCIES
    )
    if shapes != expected:
        raise ScenarioReportError(
            f"workload matrix mismatch: missing={sorted(expected - shapes)}, "
            f"unknown={sorted(shapes - expected)}"
        )


def _cache_hit_rate(sample: Mapping[str, object], point_id: str) -> float:
    cache = sample.get("cache_report")
    value = cache.get("cache_hit_rate_pct") if isinstance(cache, Mapping) else None
    if value is None:
        value = sample.get("_euboulia_cache_hit_rate_pct")
    result = _optional_finite(value)
    if result != 0.0:
        raise ScenarioReportError(f"{point_id} cache hit rate is {result!r}, expected 0.0")
    return result


def _prometheus_mean(path: Path, metric: str) -> float:
    values: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(metric + "{") or line.startswith(metric + " "):
            value = float(line.rsplit(" ", 1)[-1])
            if not math.isfinite(value):
                raise ScenarioReportError(f"{path} contains non-finite {metric}")
            values.append(value)
    if not values:
        raise ScenarioReportError(f"{path} does not contain {metric}")
    return statistics.mean(values)


def _avg_spec_accept_length(sample: Mapping[str, object], point_id: str) -> float:
    direct = _optional_finite(sample.get("avg_spec_accept_length"))
    if isinstance(direct, float):
        return direct
    server_info = sample.get("server_info")
    internal_states = (
        server_info.get("internal_states") if isinstance(server_info, Mapping) else None
    )
    values: list[float] = []
    if isinstance(internal_states, list):
        for state in internal_states:
            value = (
                _optional_finite(state.get("avg_spec_accept_length"))
                if isinstance(state, Mapping)
                else ""
            )
            if isinstance(value, float):
                values.append(value)
    if not values:
        raise ScenarioReportError(f"{point_id} has no avg_spec_accept_length")
    return statistics.mean(values)


def _summarize_shape(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    def numbers(key: str) -> list[float]:
        return [float(cast(float, row[key])) for row in rows]

    first = rows[0]
    accept = [
        float(value)
        for row in rows
        if isinstance((value := row["accept_length"]), int | float)
    ]
    average_spec_accept = [
        float(value)
        for row in rows
        if isinstance((value := row["avg_spec_accept_length"]), int | float)
    ]
    spec_lengths = [
        float(value)
        for row in rows
        if isinstance((value := row["spec_accept_length"]), int | float)
    ]
    spec_rates = [
        float(value)
        for row in rows
        if isinstance((value := row["spec_accept_rate"]), int | float)
    ]
    ttft = numbers("mean_ttft_ms")
    tpot = numbers("mean_tpot_ms")
    e2e = numbers("mean_e2e_latency_ms")
    return {
        "case_type": first["case_type"],
        "input_len": first["input_len"],
        "output_len": first["output_len"],
        "concurrency": first["concurrency"],
        "rounds": 3,
        "best_mean_ttft_ms": min(ttft),
        "avg_mean_ttft_ms": statistics.mean(ttft),
        "best_mean_tpot_ms": min(tpot),
        "avg_mean_tpot_ms": statistics.mean(tpot),
        "best_mean_e2e_latency_ms": min(e2e),
        "avg_mean_e2e_latency_ms": statistics.mean(e2e),
        "accept_length_avg": statistics.mean(accept) if accept else "",
        "avg_spec_accept_length_avg": (
            statistics.mean(average_spec_accept) if average_spec_accept else ""
        ),
        "spec_accept_length_avg": statistics.mean(spec_lengths) if spec_lengths else "",
        "spec_accept_rate_avg": statistics.mean(spec_rates) if spec_rates else "",
        "gsm8k_score": "",
    }


def _copy_startup_artifacts(source: Path, target: Path) -> None:
    raw = target / "raw"
    logs = target / "logs"
    raw.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    mapping = {
        "environment.txt": target / "environment.txt",
        "server_startup_summary.md": target / "server_startup_summary.md",
        "server_info_initial.json": raw / "server_info_initial.json",
        "metrics_initial.prom": raw / "metrics_initial.prom",
        "nvidia_smi_initial.csv": raw / "nvidia_smi_initial.csv",
        "models_initial.json": raw / "models_initial.json",
        "install_observation.json": raw / "install_observation.json",
        "server_command.txt": raw / "server_command.txt",
        "server_startup_key_info.log": logs / "server_startup_key_info.log",
        "server_startup_warnings_errors.log": logs / "server_startup_warnings_errors.log",
    }
    for name, destination in mapping.items():
        source_path = source / name
        if not source_path.is_file():
            raise ScenarioReportError(f"startup evidence is missing: {source_path}")
        shutil.copy2(source_path, destination)


def _markdown_summary(
    validation: Mapping[str, object],
    summaries: Sequence[Mapping[str, object]],
    image: str,
    sglang_commit: str,
    deepgemm_commit: str,
) -> str:
    lines = [
        "# DS-V4-Flash DSPARK CP8 TP8 EP8 MegaMoE validation",
        "",
        "## 结论",
        "",
        "- 启动门禁、90 轮 cache-clean ShareGPT、8-rank SM90 MegaMoE trace 与 GSM8K 均通过。",
        "- Kernel path: `fp8_mxfp4_mega_moe`; TP ranks: `0,1,2,3,4,5,6,7`。",
        f"- GSM8K 200 题 score: `{validation['gsm8k_score']}`。",
        "- 30/30 shape 完成同形状 warmup; 90/90 正式轮 flush 成功且 cache hit 为 0.0%。",
        "- 异常: `无`。",
        "",
        "## 版本",
        "",
        f"- Image: `{image}`",
        f"- SGLang commit: `{sglang_commit}`",
        f"- DeepGEMM commit: `{deepgemm_commit}`",
        "",
        "## Shape 汇总",
        "",
        "| type | ISL | OSL | C | best TTFT ms | avg TTFT ms | best TPOT ms | "
        "avg TPOT ms | accept avg | spec length avg | spec rate avg |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summaries:
        lines.append(
            f"| {row['case_type']} | {row['input_len']} | {row['output_len']} | "
            f"{row['concurrency']} | {row['best_mean_ttft_ms']} | "
            f"{row['avg_mean_ttft_ms']} | {row['best_mean_tpot_ms']} | "
            f"{row['avg_mean_tpot_ms']} | {row['accept_length_avg']} | "
            f"{row['spec_accept_length_avg']} | {row['spec_accept_rate_avg']} |"
        )
    lines.extend(
        [
            "",
            "逐轮值见 `summary_rounds.csv`; best/avg 与 acceptance 见 `summary_best.csv`。",
            "启动、安装、8-rank kernel trace、flush、cache report "
            "和逐轮 server 快照均保留在本目录。",
            "",
        ]
    )
    return "\n".join(lines)


def _one(root: Path, name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise ScenarioReportError(f"expected one {name}, observed {len(matches)}")
    return matches[0]


def _read_object(path: Path) -> dict[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioReportError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScenarioReportError(f"JSON artifact must be an object: {path}")
    return cast(dict[str, object], value)


def _declared_image(runtime: Mapping[str, object]) -> str:
    expected = runtime.get("expected")
    container = expected.get("container") if isinstance(expected, Mapping) else None
    image = container.get("image") if isinstance(container, Mapping) else None
    if not isinstance(image, str) or not image:
        raise ScenarioReportError("runtime provenance has no declared container image")
    return image


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ScenarioReportError(f"required string is missing: {key}")
    return item


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScenarioReportError(f"{name} must be a positive integer")
    integer = int(value)
    if integer <= 0 or integer != value:
        raise ScenarioReportError(f"{name} must be a positive integer")
    return integer


def _finite(value: Mapping[str, object], key: str, label: str) -> float:
    result = _optional_finite(value.get(key))
    if not isinstance(result, float):
        raise ScenarioReportError(f"{label}.{key} must be finite")
    return result


def _optional_finite(value: object) -> float | str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    result = float(value)
    return result if math.isfinite(result) else ""


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ScenarioReportError(f"cannot write empty CSV: {path}")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


__all__ = ["ScenarioReportError", "write_report"]
