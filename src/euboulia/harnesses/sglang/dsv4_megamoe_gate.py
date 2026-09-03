"""Fail-closed startup gate for the DS-V4-Flash CP8/TP8/EP8 MegaMoE scenario."""

from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class StartupGateError(RuntimeError):
    """Raised when observed runtime state violates the scenario contract."""


REQUIRED_ENVIRONMENT: Mapping[str, str] = {
    "SGLANG_OPT_USE_DEEPGEMM_MEGA_MOE": "1",
    "SGLANG_OPT_DEEPGEMM_MEGA_MOE_NUM_MAX_TOKENS_PER_RANK": "1024",
    "SGLANG_JIT_DEEPGEMM_PRECOMPILE": "1",
    "SGLANG_OPT_USE_JIT_INDEXER_METADATA": "1",
    "SGLANG_OPT_SWA_EVICT_KEEP_WINDOW": "1",
    "SGLANG_JIT_DEEPGEMM_TILE_BUCKET_WARMUP": "1",
    "SGLANG_CP_DISABLE_DECODE_UNPADDING": "0",
    "SGLANG_CP_DISABLE_DECODE_SLICE": "0",
    "SGLANG_RAGGED_VERIFY_MODE": "static",
    "FLASHINFER_DISABLE_VERSION_CHECK": "1",
    "SGLANG_OPT_FP8_WO_A_GEMM": "1",
    "SGLANG_ENABLE_JIT_DEEPGEMM": "1",
    "SGLANG_DSPARK_VERIFY_WO_A_EINSUM_LIMIT": "8",
    "SGLANG_DSV4_DEEPGEMM_SMALL_M_TRITON_LIMIT": "8",
    "SGLANG_DSPARK_VERIFY_COMPRESS_BF16_FP32_LIMIT": "8",
    "SGLANG_DSPARK_VERIFY_WO_B_BF16_LIMIT": "8",
    "NCCL_IB_TC": "136",
    "NCCL_IB_RETRY_CNT": "7",
    "NCCL_SOCKET_IFNAME": "bond0",
    "NCCL_DEBUG": "WARN",
    "NCCL_IB_GID_INDEX": "3",
    "NCCL_IB_SL": "5",
    "NCCL_IB_TIMEOUT": "22",
    "NCCL_SET_THREAD_NAME": "1",
    "NCCL_IB_HCA": "mlx5_bond",
    "NCCL_DEBUG_SUBSYS": "INIT,TUNING,GRAPH",
    "NCCL_IB_QPS_PER_CONNECTION": "8",
}

FORBIDDEN_ENVIRONMENT = frozenset(
    {"SGLANG_DSV4_FP4_EXPERTS", "SGLANG_HUMMING_INPUT_QUANT_CONFIG"}
)
FORBIDDEN_ARGUMENTS = frozenset(
    {"--moe-runner-backend=humming", "--quantization=humming"}
)
REQUIRED_DEEPGEMM_APIS = (
    "get_symm_buffer_for_sm90_mega_moe",
    "transform_weights_for_fp8_mxfp4_fused_mega_moe_sm90",
    "fp8_mxfp4_mega_moe",
)
EXPECTED_SERVER_INFO: Mapping[str, object] = {
    "status": "ready",
    "model_path": "/home/admin/model/DeepSeek-V4-Flash-0731",
    "pp_size": 1,
    "tp_size": 8,
    "attn_cp_size": 8,
    "ep_size": 8,
    "moe_a2a_backend": "megamoe",
    "speculative_algorithm": "DSPARK",
    "speculative_dspark_block_size": 5,
    "chunked_prefill_size": 8192,
    "max_running_requests": 64,
    "cuda_graph_max_bs_decode": 64,
    "mem_fraction_static": 0.88,
    "disable_custom_all_reduce": False,
    "enable_nccl_nvls": False,
}
REQUIRED_VALUE_ARGUMENTS: Mapping[str, str] = {
    "--model-path": "/home/admin/model/DeepSeek-V4-Flash-0731",
    "--host": "0.0.0.0",
    "--port": "8188",
    "--nnodes": "1",
    "--pp-size": "1",
    "--tp-size": "8",
    "--mem-fraction-static": "0.88",
    "--max-running-requests": "64",
    "--cuda-graph-max-bs-decode": "64",
    "--chunked-prefill-size": "8192",
    "--tool-call-parser": "deepseekv4",
    "--reasoning-parser": "deepseek-v4",
    "--cp-strategy": "interleave",
    "--moe-a2a-backend": "megamoe",
    "--speculative-algorithm": "DSPARK",
    "--speculative-dspark-block-size": "5",
    "--swa-full-tokens-ratio": "0.05",
}
REQUIRED_FLAG_ARGUMENTS = frozenset(
    {
        "--trust-remote-code",
        "--enable-prefill-cp",
        "--enable-cache-report",
        "--enable-metrics",
        "--allow-auto-output-truncate",
        "--enable-repetition-detection",
        "--enable-cp-decode-attn-tp",
    }
)


@dataclass(frozen=True, slots=True)
class StartupGateSettings:
    endpoint: str
    pid: int
    stdout_path: Path
    stderr_path: Path
    service_manifest_path: Path
    evidence_dir: Path
    sglang_source: Path = Path("/home/admin/src/dsv4-megamoe/SGLang")
    deepgemm_source: Path = Path("/home/admin/src/dsv4-megamoe/DeepGEMM")
    expected_gpu_count: int = 8
    allowed_warning_patterns: tuple[str, ...] = (
        r"python -m sglang\.launch_server.*(?:deprecated|recommended)",
        r"FastAPIDeprecationWarning.*ORJSONResponse.*deprecated",
        r"ProcessGroupNCCL.*Guessing device ID based on global rank",
    )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> StartupGateSettings:
        values = os.environ if environ is None else environ
        try:
            pid = int(_required(values, "EUBOULIA_TARGET_PID"))
        except ValueError as exc:
            raise StartupGateError("EUBOULIA_TARGET_PID must be a positive integer") from exc
        if pid <= 0:
            raise StartupGateError("EUBOULIA_TARGET_PID must be a positive integer")
        extra_patterns = tuple(
            item
            for item in values.get("EUBOULIA_ALLOWED_STARTUP_WARNING_REGEX", "").split(";;")
            if item
        )
        return cls(
            endpoint=_required(values, "EUBOULIA_TARGET_ENDPOINT").rstrip("/"),
            pid=pid,
            stdout_path=Path(_required(values, "EUBOULIA_TARGET_STDOUT_PATH")),
            stderr_path=Path(_required(values, "EUBOULIA_TARGET_STDERR_PATH")),
            service_manifest_path=Path(_required(values, "EUBOULIA_TARGET_MANIFEST_PATH")),
            evidence_dir=Path(_required(values, "EUBOULIA_COMMAND_EVIDENCE_DIR")),
            sglang_source=Path(
                values.get("EUBOULIA_SGLANG_SOURCE")
                or values.get("EUBOULIA_TARGET_WORKSPACE")
                or "/home/admin/src/dsv4-megamoe/SGLang"
            ),
            deepgemm_source=Path(
                values.get("EUBOULIA_DEEPGEMM_SOURCE", "/home/admin/src/dsv4-megamoe/DeepGEMM")
            ),
            allowed_warning_patterns=(
                r"python -m sglang\.launch_server.*(?:deprecated|recommended)",
                r"FastAPIDeprecationWarning.*ORJSONResponse.*deprecated",
                r"ProcessGroupNCCL.*Guessing device ID based on global rank",
                *extra_patterns,
            ),
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        return None


def run_startup_gate(settings: StartupGateSettings) -> dict[str, object]:
    """Capture startup evidence and reject any mismatch before warmup."""

    evidence = settings.evidence_dir
    evidence.mkdir(parents=True, exist_ok=False)
    environment, argv = _read_owned_process(settings.pid)
    _validate_environment(environment)
    _validate_argv(argv)

    health = _http_get(settings.endpoint + "/health")
    models_bytes = _http_get(settings.endpoint + "/v1/models")
    server_info_bytes = _http_get(settings.endpoint + "/server_info")
    metrics = _http_get(settings.endpoint + "/metrics")
    try:
        models: object = json.loads(models_bytes)
        server_info: object = json.loads(server_info_bytes)
    except json.JSONDecodeError as exc:
        raise StartupGateError(f"server returned invalid JSON: {exc}") from exc
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise StartupGateError("/v1/models did not return an OpenAI-compatible model list")
    if not isinstance(server_info, dict):
        raise StartupGateError("/server_info must return a JSON object")
    _validate_server_info(cast(dict[str, object], server_info))

    gpu_csv = _nvidia_smi()
    gpu_lines = [line for line in gpu_csv.splitlines() if line.strip()]
    if len(gpu_lines) != settings.expected_gpu_count:
        raise StartupGateError(
            f"expected {settings.expected_gpu_count} GPUs, observed {len(gpu_lines)}"
        )
    if any("H20" not in line for line in gpu_lines):
        raise StartupGateError("all visible GPUs must be NVIDIA H20")

    install = _install_observation(settings)
    startup_log = _read_startup_log(settings.stdout_path, settings.stderr_path)
    if "Auto-detected DSV4 routed-expert layout: is_fp4_experts=True" not in startup_log:
        raise StartupGateError("startup log does not confirm is_fp4_experts=True")
    _validate_cuda_graph_log(startup_log)
    warnings = _validate_startup_log(startup_log, settings.allowed_warning_patterns)
    service_manifest_bytes = settings.service_manifest_path.read_bytes()
    container_image, container_digest = _container_identity(service_manifest_bytes)
    service_manifest = _json_object(service_manifest_bytes, "owned service manifest")
    required_api_value = install.get("deep_gemm_required_apis")
    required_apis = required_api_value if isinstance(required_api_value, list) else []

    (evidence / "health_initial.txt").write_bytes(health)
    (evidence / "models_initial.json").write_bytes(models_bytes)
    (evidence / "server_info_initial.json").write_bytes(server_info_bytes)
    (evidence / "metrics_initial.prom").write_bytes(metrics)
    (evidence / "nvidia_smi_initial.csv").write_text(gpu_csv, encoding="utf-8")
    (evidence / "environment.txt").write_text(
        "\n".join(
            (
                f"EUBOULIA_DECLARED_CONTAINER_IMAGE={container_image}",
                f"EUBOULIA_DECLARED_CONTAINER_IMAGE_DIGEST={container_digest}",
                f"EUBOULIA_GPU_COUNT={len(gpu_lines)}",
                "EUBOULIA_GPU_MODELS="
                + ",".join(line.split(",", maxsplit=3)[2].strip() for line in gpu_lines),
                f"EUBOULIA_SGLANG_COMMIT={install['sglang_commit']}",
                f"EUBOULIA_SGLANG_VERSION={install.get('sglang_version', 'N/A')}",
                f"EUBOULIA_SGLANG_FILE={install.get('sglang_file', 'N/A')}",
                f"EUBOULIA_DEEPGEMM_COMMIT={install['deep_gemm_commit']}",
                f"EUBOULIA_DEEPGEMM_VERSION={install.get('deep_gemm_version', 'N/A')}",
                f"EUBOULIA_DEEPGEMM_FILE={install.get('deep_gemm_file', 'N/A')}",
                "EUBOULIA_DEEPGEMM_REQUIRED_APIS="
                + ",".join(str(item) for item in required_apis),
                f"EUBOULIA_SERVER_ARGV_SHLEX={shlex.join(argv)}",
                *(f"{name}={environment[name]}" for name in sorted(environment)),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (evidence / "server_command.txt").write_text(shlex.join(argv) + "\n", encoding="utf-8")
    (evidence / "server_startup.log").write_text(startup_log, encoding="utf-8")
    (evidence / "server_startup_key_info.log").write_text(
        _matching_lines(
            startup_log,
            r"memory|mem[_ -]?pool|kv[_ -]?cache|token[_ -]?capacity|"
            r"max_total_num_tokens|page[_ -]?size|cuda graph|avail_mem|weight|MegaMoE|fp8_mxfp4",
        ),
        encoding="utf-8",
    )
    (evidence / "server_startup_warnings_errors.log").write_text(
        "\n".join(warnings) + ("\n" if warnings else ""), encoding="utf-8"
    )
    (evidence / "install_observation.json").write_text(
        json.dumps(install, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (evidence / "service_manifest_initial.json").write_bytes(
        service_manifest_bytes
    )
    summary = _startup_summary(
        cast(dict[str, object], server_info),
        install,
        warnings,
        startup_log,
        service_manifest,
    )
    (evidence / "server_startup_summary.md").write_text(summary, encoding="utf-8")
    result = {
        "status": "passed",
        "pid": settings.pid,
        "gpu_count": len(gpu_lines),
        "server": {key: server_info.get(key) for key in EXPECTED_SERVER_INFO},
        "install": install,
        "reviewed_warnings": warnings,
        "evidence_dir": str(evidence),
    }
    (evidence / "startup_gate.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def _read_owned_process(pid: int) -> tuple[dict[str, str], tuple[str, ...]]:
    try:
        environ_bytes = Path(f"/proc/{pid}/environ").read_bytes()
        cmdline_bytes = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise StartupGateError(f"cannot inspect owned target PID {pid}: {exc}") from exc
    environment: dict[str, str] = {}
    for item in environ_bytes.split(b"\0"):
        if not item or b"=" not in item:
            continue
        raw_name, raw_value = item.split(b"=", 1)
        environment[raw_name.decode(errors="replace")] = raw_value.decode(errors="replace")
    argv = tuple(item.decode(errors="replace") for item in cmdline_bytes.split(b"\0") if item)
    if not argv:
        raise StartupGateError(f"owned target PID {pid} has an empty command line")
    return environment, argv


def _validate_environment(environment: Mapping[str, str]) -> None:
    for name, expected in REQUIRED_ENVIRONMENT.items():
        if environment.get(name) != expected:
            raise StartupGateError(
                f"required environment mismatch: {name}={environment.get(name)!r}, "
                f"expected {expected!r}"
            )
    if not environment.get("SGLANG_HOST_IP"):
        raise StartupGateError("SGLANG_HOST_IP must be present and non-empty")
    present = sorted(name for name in FORBIDDEN_ENVIRONMENT if name in environment)
    if present:
        raise StartupGateError("forbidden Humming environment is present: " + ", ".join(present))


def _validate_argv(argv: Sequence[str]) -> None:
    normalized = tuple(
        f"{token}={argv[index + 1]}"
        if token in {"--moe-runner-backend", "--quantization"} and index + 1 < len(argv)
        else token
        for index, token in enumerate(argv)
    )
    forbidden = sorted(FORBIDDEN_ARGUMENTS.intersection(normalized))
    if forbidden:
        raise StartupGateError(
            "forbidden Humming launch argument is present: " + ", ".join(forbidden)
        )
    for name, expected in REQUIRED_VALUE_ARGUMENTS.items():
        try:
            actual = argv[argv.index(name) + 1]
        except (ValueError, IndexError) as exc:
            raise StartupGateError(f"required launch argument is missing: {name}") from exc
        if actual != expected:
            raise StartupGateError(f"launch argument {name}={actual!r}, expected {expected!r}")
    missing_flags = sorted(REQUIRED_FLAG_ARGUMENTS.difference(argv))
    if missing_flags:
        raise StartupGateError("required launch flags are missing: " + ", ".join(missing_flags))


def _validate_server_info(server_info: Mapping[str, object]) -> None:
    for key, expected in EXPECTED_SERVER_INFO.items():
        if server_info.get(key) != expected:
            raise StartupGateError(
                f"server_info mismatch: {key}={server_info.get(key)!r}, expected {expected!r}"
            )
    internal_states = server_info.get("internal_states")
    if not isinstance(internal_states, list) or not internal_states:
        raise StartupGateError("server_info must contain at least one internal state")
    for rank, state in enumerate(internal_states):
        if not isinstance(state, Mapping):
            raise StartupGateError(f"server_info internal state {rank} must be an object")
        memory = state.get("memory_usage")
        if not isinstance(memory, Mapping):
            raise StartupGateError(f"server_info rank {rank} has no memory_usage")
        if memory.get("token_capacity") != server_info.get("max_total_num_tokens"):
            raise StartupGateError(f"server_info rank {rank} token capacity is inconsistent")
        if state.get("page_size") != 256:
            raise StartupGateError(f"server_info rank {rank} page size is not 256")
        for memory_key in ("weight", "token_capacity", "graph"):
            value = memory.get(memory_key)
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise StartupGateError(
                    f"server_info rank {rank} has invalid memory_usage.{memory_key}"
                )
    cuda_graph = server_info.get("cuda_graph_config")
    decode = cuda_graph.get("decode") if isinstance(cuda_graph, Mapping) else None
    prefill = cuda_graph.get("prefill") if isinstance(cuda_graph, Mapping) else None
    decode_batches = decode.get("bs") if isinstance(decode, Mapping) else None
    if (
        not isinstance(decode, Mapping)
        or decode.get("backend") != "full"
        or decode.get("max_bs") != 64
        or not isinstance(decode_batches, list)
        or 64 not in decode_batches
    ):
        raise StartupGateError("server_info does not confirm full decode CUDA Graph through BS64")
    if not isinstance(prefill, Mapping) or prefill.get("backend") != "disabled":
        raise StartupGateError("server_info does not confirm the expected disabled prefill graph")


def _http_get(url: str) -> bytes:
    request = Request(url, method="GET", headers={"User-Agent": "euboulia-dsv4-gate/1"})
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=60.0) as response:
            if not 200 <= response.getcode() < 300:
                raise StartupGateError(f"GET {url} returned HTTP {response.getcode()}")
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise StartupGateError(f"GET {url} failed: {exc}") from exc


def _nvidia_smi() -> str:
    completed = subprocess.run(
        (
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ),
        check=False,
        shell=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise StartupGateError(f"nvidia-smi failed: {completed.stderr.strip()}")
    return completed.stdout


def _install_observation(settings: StartupGateSettings) -> dict[str, object]:
    try:
        sglang = importlib.import_module("sglang")
        deep_gemm = importlib.import_module("deep_gemm")
    except ImportError as exc:
        raise StartupGateError(f"required runtime package is not importable: {exc}") from exc
    missing = [name for name in REQUIRED_DEEPGEMM_APIS if not hasattr(deep_gemm, name)]
    if missing:
        raise StartupGateError("DeepGEMM SM90 MegaMoE APIs are missing: " + ", ".join(missing))
    sglang_file = str(getattr(sglang, "__file__", ""))
    expected_prefix = str((settings.sglang_source / "python").resolve()) + "/"
    if not str(Path(sglang_file).resolve()).startswith(expected_prefix):
        raise StartupGateError(
            f"SGLang import is not from the declared source checkout: {sglang_file}"
        )
    return {
        "sglang_file": sglang_file,
        "sglang_version": str(getattr(sglang, "__version__", "unknown")),
        "sglang_commit": _git_head(settings.sglang_source),
        "deep_gemm_file": str(getattr(deep_gemm, "__file__", "")),
        "deep_gemm_version": str(getattr(deep_gemm, "__version__", "unknown")),
        "deep_gemm_commit": _git_head(settings.deepgemm_source),
        "deep_gemm_required_apis": list(REQUIRED_DEEPGEMM_APIS),
        "deep_gemm_missing_apis": missing,
    }


def _git_head(repository: Path) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=False,
        shell=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise StartupGateError(
            f"cannot capture git HEAD for {repository}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _read_startup_log(stdout_path: Path, stderr_path: Path) -> str:
    try:
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise StartupGateError(f"cannot read owned target log: {exc}") from exc
    return stdout + ("\n--- stderr ---\n" if stderr else "") + stderr


def _validate_startup_log(text: str, allowed_warning_patterns: Sequence[str]) -> list[str]:
    fatal_pattern = re.compile(
        r"traceback|out of memory|\boom\b|fallback|missing .*(?:api|symbol)|"
        r"symm.*buffer.*(?:fail|error)|jit.*(?:fail|error)|exception",
        re.IGNORECASE,
    )
    fatal = [line for line in text.splitlines() if fatal_pattern.search(line)]
    if fatal:
        raise StartupGateError("fatal or fallback startup log entry: " + fatal[0])
    warning_pattern = re.compile(
        r"warning|warn|error|failed|\[(?:W|E)\d{3,}", re.IGNORECASE
    )
    warnings = [
        line
        for line in text.splitlines()
        if warning_pattern.search(line) and not line.strip().startswith("warnings.warn(")
    ]
    compiled_allowed = [re.compile(pattern, re.IGNORECASE) for pattern in allowed_warning_patterns]
    unreviewed = [
        line for line in warnings if not any(pattern.search(line) for pattern in compiled_allowed)
    ]
    if unreviewed:
        raise StartupGateError("unreviewed startup warning/error: " + unreviewed[0])
    return warnings


def _validate_cuda_graph_log(text: str) -> None:
    captures = re.findall(
        r"Capturing batches \(bs=(\d+) avail_mem=([0-9.]+) GB\)", text
    )
    if not captures or captures[0][0] != "64":
        raise StartupGateError("startup log does not show decode CUDA Graph capture from BS64")
    if "100%" not in text:
        raise StartupGateError("startup log does not show completed CUDA Graph capture")


def _matching_lines(text: str, pattern: str) -> str:
    compiled = re.compile(pattern, re.IGNORECASE)
    lines = [line for line in text.splitlines() if compiled.search(line)]
    return "\n".join(lines) + ("\n" if lines else "")


def _startup_summary(
    server_info: Mapping[str, object],
    install: Mapping[str, object],
    warnings: Sequence[str],
    startup_log: str,
    service_manifest: Mapping[str, object],
) -> str:
    keys = (
        "model_path",
        "pp_size",
        "tp_size",
        "attn_cp_size",
        "ep_size",
        "moe_a2a_backend",
        "is_fp4_experts",
        "dtype",
        "kv_cache_dtype",
        "max_total_num_tokens",
        "page_size",
        "mem_fraction_static",
        "cuda_graph_max_bs_decode",
    )
    cuda_graph = server_info.get("cuda_graph_config")
    decode = cuda_graph.get("decode") if isinstance(cuda_graph, Mapping) else None
    prefill = cuda_graph.get("prefill") if isinstance(cuda_graph, Mapping) else None
    decode_backend = decode.get("backend", "N/A") if isinstance(decode, Mapping) else "N/A"
    decode_batches = decode.get("bs", "N/A") if isinstance(decode, Mapping) else "N/A"
    prefill_backend = (
        prefill.get("backend", "N/A") if isinstance(prefill, Mapping) else "N/A"
    )
    capture_rows = re.findall(
        r"Capturing batches \(bs=(\d+) avail_mem=([0-9.]+) GB\)", startup_log
    )
    capture_first = (
        f"bs={capture_rows[0][0]}, avail_mem={capture_rows[0][1]} GB"
        if capture_rows
        else "N/A"
    )
    capture_last = (
        f"bs={capture_rows[-1][0]}, avail_mem={capture_rows[-1][1]} GB"
        if capture_rows
        else "N/A"
    )
    started_at = service_manifest.get("started_at")
    ready_at = service_manifest.get("ready_at")
    startup_elapsed = _elapsed_seconds(started_at, ready_at)
    lines = [
        "# DS-V4-Flash MegaMoE startup summary",
        "",
        "- Gate: `passed`",
        "- Scope: `global` (server_info 未按 rank 区分的字段)",
        f"- SGLang commit: `{install['sglang_commit']}`",
        f"- DeepGEMM commit: `{install['deep_gemm_commit']}`",
        f"- Reviewed startup warnings: `{len(warnings)}`",
        "",
        "| Field | Observed |",
        "| --- | --- |",
        *[f"| `{key}` | `{server_info.get(key)}` |" for key in keys],
        "",
        "## CUDA Graph",
        "",
        f"- Decode backend: `{decode_backend}`",
        f"- Decode capture batch sizes: `{decode_batches}`",
        f"- Prefill backend: `{prefill_backend}`",
        f"- First capture observation: `{capture_first}`",
        f"- Last capture observation: `{capture_last}`",
        "- Capture completion: `passed` (100% progress observed and server reached ready state)",
        "",
        "## Startup timing",
        "",
        f"- Process started at: `{started_at if isinstance(started_at, str) else 'N/A'}`",
        f"- Server ready at: `{ready_at if isinstance(ready_at, str) else 'N/A'}`",
        f"- Total start-to-ready seconds: `{startup_elapsed}`",
        "- Weight loading seconds: `N/A` (no structured duration exposed)",
        "- KV cache initialization seconds: `N/A` (no structured duration exposed)",
        "- CUDA Graph capture seconds: `N/A` (no structured duration exposed)",
        "",
        "## Per-rank resources",
        "",
        "| rank | weight GiB | KV cache GiB | token capacity | graph GiB | page size |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        *_rank_resource_rows(server_info),
        "",
        "## Reviewed warnings",
        "",
        "| classification | original log line |",
        "| --- | --- |",
        *(
            [
                "| 已知可接受 (allowlist) | `"
                + line.replace("|", "\\|").replace("`", "'")
                + "` |"
                for line in warnings
            ]
            if warnings
            else ["| N/A | No startup warnings observed |"]
        ),
        "",
        "完整资源、KV cache、CUDA graph 和 MegaMoE 原始信息见 "
        "`server_startup_key_info.log`、`server_info_initial.json` 与 `metrics_initial.prom`。",
        "",
    ]
    return "\n".join(lines)


def _elapsed_seconds(started_at: object, ready_at: object) -> str:
    if not isinstance(started_at, str) or not isinstance(ready_at, str):
        return "N/A"
    try:
        elapsed = (
            datetime.fromisoformat(ready_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
    except ValueError:
        return "N/A"
    return f"{elapsed:.3f}" if elapsed >= 0 else "N/A"


def _rank_resource_rows(server_info: Mapping[str, object]) -> list[str]:
    internal_states = server_info.get("internal_states")
    if not isinstance(internal_states, list):  # guarded by _validate_server_info
        return []
    rows: list[str] = []
    for rank, state in enumerate(internal_states):
        if not isinstance(state, Mapping):
            continue
        memory = state.get("memory_usage")
        if not isinstance(memory, Mapping):
            continue
        rank_label: str | int = "global" if len(internal_states) == 1 else rank
        rows.append(
            f"| {rank_label} | {memory.get('weight', 'N/A')} | "
            f"{memory.get('kvcache', 'N/A')} | "
            f"{memory.get('token_capacity', 'N/A')} | {memory.get('graph', 'N/A')} | "
            f"{state.get('page_size', 'N/A')} |"
        )
    return rows


def _container_identity(manifest_bytes: bytes) -> tuple[str, str]:
    try:
        manifest: object = json.loads(manifest_bytes)
    except json.JSONDecodeError:
        return ("N/A", "N/A")
    provenance = manifest.get("provenance") if isinstance(manifest, Mapping) else None
    runtime = provenance.get("runtime") if isinstance(provenance, Mapping) else None
    expected = runtime.get("expected") if isinstance(runtime, Mapping) else None
    container = expected.get("container") if isinstance(expected, Mapping) else None
    if not isinstance(container, Mapping):
        return ("N/A", "N/A")
    image = container.get("image")
    digest = container.get("digest")
    return (
        image if isinstance(image, str) and image else "N/A",
        digest if isinstance(digest, str) and digest else "N/A",
    )


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise StartupGateError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StartupGateError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise StartupGateError(f"{name} is required")
    return value


def main() -> int:
    try:
        result = run_startup_gate(StartupGateSettings.from_environment())
    except StartupGateError as exc:
        print(f"DS-V4 MegaMoE startup gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
