"""Cached, read-only observations of every physical GPU on selectable nodes."""

from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from euboulia.playground.config import Cluster, GpuMetrics
from euboulia.playground.kubernetes import Kubernetes, PlaygroundError

CACHE_SECONDS = 15
STALE_SECONDS = 45
_METRIC_FIELDS = {
    "DCGM_FI_DEV_GPU_UTIL": "utilization",
    "DCGM_FI_DEV_FB_USED": "memory_used_mb",
    "DCGM_FI_DEV_FB_FREE": "memory_free_mb",
}
_SAMPLE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\{(.*)\}\s+(\S+)(?:\s+(\S+))?$")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("(?:[^"\\]|\\.)*")')


class _ReadOnlyKubernetes(Kubernetes):
    def call(self, args: list[str], *, data: str | None = None, timeout: int = 5) -> str:
        # Put the override before the subcommand, including before exec's `--`.
        return super().call(["--request-timeout=4s", *args], data=data, timeout=min(timeout, 5))


def _number(value: object, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and 0 <= result <= maximum else None


def parse_gpu_metrics(output: str, node_name: str) -> list[dict[str, Any]]:
    """Deduplicate physical UUID series while retaining invalid coverage evidence."""
    records: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.fullmatch(line.strip())
        if not match or match[1] not in _METRIC_FIELDS:
            continue
        pairs = _LABEL.findall(match[2])
        if _LABEL.sub("", match[2]).replace(",", "").strip() or len({p[0] for p in pairs}) != len(
            pairs
        ):
            raise ValueError("GPU metric labels are malformed")
        labels = {key: json.loads(value) for key, value in pairs}
        if any(labels.get(key) and labels[key] != node_name for key in ("Hostname", "NodeName")):
            raise ValueError("GPU metrics belong to another node")
        uuid = labels.get("UUID", "")
        index = labels.get("gpu", "")
        if any(key in labels for key in ("GPU_I_ID", "GPU_I_PROFILE")) or uuid.startswith("MIG-"):
            raise ValueError("MIG metrics do not cover physical GPUs")
        if not uuid.startswith("GPU-") or len(uuid) <= 4 or not index.isdigit():
            raise ValueError("GPU metrics lack physical UUID/index mappings")
        record = records.setdefault(uuid, {"uuid": uuid, "index": int(index)})
        if record["index"] != int(index):
            raise ValueError("GPU UUID changed physical index")
        if match[4] is not None:
            try:
                sampled_at = int(match[4]) / 1000
            except (ValueError, OverflowError) as exc:
                raise ValueError("GPU metric timestamp is invalid") from exc
            if not 0 <= sampled_at <= time.time():
                raise ValueError("GPU metric timestamp is invalid or in the future")
            record["sampled_at"] = min(sampled_at, record.get("sampled_at", sampled_at))
        key = _METRIC_FIELDS[match[1]]
        maximum = 100 if key == "utilization" else 16_777_216
        value = _number(match[3], maximum)
        if key in record and record[key] != value:
            record["invalid"] = True
            # Keep the conservative busy evidence when duplicated labels disagree.
            if value is not None:
                previous = record[key]
                record[key] = max(value, previous) if previous is not None else value
        else:
            record[key] = value
    if len({record["index"] for record in records.values()}) != len(records):
        raise ValueError("GPU indices are not unique")
    return list(records.values())


def gpu_status(
    node: dict[str, Any],
    readings: list[dict[str, Any]],
    metrics: GpuMetrics,
    *,
    checked_at: float | None = None,
    source: str | None = None,
    reason: str = "GPU telemetry unavailable",
) -> dict[str, Any]:
    """Only complete, valid physical coverage may establish that every GPU is idle."""
    sample_times = [reading["sampled_at"] for reading in readings if "sampled_at" in reading]
    if sample_times:
        checked_at = min(sample_times + ([checked_at] if checked_at is not None else []))
    total = node.get("gpu_count")
    status: dict[str, Any] = {
        "state": "unknown",
        "total": total,
        "idle": 0,
        "measured": 0,
        "checked_at": checked_at,
        "reason": reason,
        "source": source,
        "max_utilization": None,
        "max_memory_used_mb": None,
    }
    if node.get("ready") is False:
        return status | {"state": "not_ready", "reason": "Node is not Ready"}
    if node.get("gpu_mig"):
        return status | {"reason": "MIG inventory cannot establish physical GPU idleness"}
    if total == 0:
        return status | {"state": "no_gpu", "reason": "No physical GPUs advertised"}
    if checked_at is not None and time.time() - checked_at > STALE_SECONDS:
        return status | {"reason": "GPU telemetry is stale"}
    seen_uuids: set[str] = set()
    indices: dict[int, str] = {}
    invalid_mapping = False
    busy = False
    utilization_values: list[float] = []
    memory_values: list[float] = []
    for reading in readings:
        uuid, index = reading.get("uuid"), reading.get("index")
        if (
            not isinstance(uuid, str)
            or not uuid.startswith("GPU-")
            or len(uuid) <= 4
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or (index in indices and indices[index] != uuid)
        ):
            invalid_mapping = True
            continue
        if uuid in seen_uuids:
            invalid_mapping = True
            continue
        seen_uuids.add(uuid)
        indices[index] = uuid
        util = _number(reading.get("utilization"), 100)
        used = _number(reading.get("memory_used_mb"), 16_777_216)
        free = _number(reading.get("memory_free_mb"), 16_777_216)
        if free is None:
            memory_total = _number(reading.get("memory_total_mb"), 16_777_216)
            if used is not None and memory_total is not None and used <= memory_total:
                free = memory_total - used
        if util is not None:
            utilization_values.append(util)
        if used is not None:
            memory_values.append(used)
        observed_busy = (util is not None and util > metrics.idle_utilization_percent) or (
            used is not None and used > metrics.idle_memory_mb
        )
        busy = busy or observed_busy
        if (
            util is not None
            and used is not None
            and free is not None
            and used + free > 0
            and not reading.get("invalid")
        ):
            status["measured"] += 1
            if not observed_busy:
                status["idle"] += 1
    status["max_utilization"] = max(utilization_values, default=None)
    status["max_memory_used_mb"] = max(memory_values, default=None)
    if busy:
        return status | {"state": "busy", "reason": "GPU compute or framebuffer memory is in use"}
    if (
        not invalid_mapping
        and isinstance(total, int)
        and total > 0
        and len(readings) == total
        and status["measured"] == total
        and checked_at is not None
    ):
        return status | {"state": "idle", "reason": "Every physical GPU is idle"}
    if readings:
        status["reason"] = "Incomplete or invalid physical GPU coverage"
    return status


@dataclass
class _Cache:
    nodes: list[dict[str, Any]] | None = None
    listed_at: float = 0
    attempted_at: float = 0
    updated_at: float | None = None
    refreshing: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class NodeMonitor:
    def __init__(
        self,
        clusters: dict[str, Cluster],
        sessions: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.clusters = clusters
        self.sessions = sessions
        self._caches = {name: _Cache() for name in clusters}
        self._closed = threading.Event()

    @staticmethod
    def _baseline(
        node: dict[str, Any], previous: dict[str, Any] | None, metrics: GpuMetrics
    ) -> dict[str, Any]:
        status = gpu_status(node, [], metrics, reason="Reading GPU telemetry")
        if status["state"] in {"not_ready", "no_gpu"} or node.get("gpu_mig"):
            return status
        if (
            previous
            and previous.get("total") == node.get("gpu_count")
            and previous.get("checked_at") is not None
            and time.time() - previous["checked_at"] <= STALE_SECONDS
        ):
            return previous
        return status

    def snapshot(self, cluster_name: str, *, refresh: bool = False) -> dict[str, Any]:
        cluster, cache = self.clusters[cluster_name], self._caches[cluster_name]
        now = time.time()
        with cache.lock:
            if not self._closed.is_set() and (
                cache.nodes is None
                or (not cache.refreshing and (refresh or now - cache.listed_at >= CACHE_SECONDS))
            ):
                try:
                    nodes = _ReadOnlyKubernetes(cluster).nodes()
                except (PlaygroundError, ValueError, KeyError, TypeError, AttributeError):
                    if cache.nodes is None:
                        raise
                    for node in cache.nodes:
                        node["gpu"] = gpu_status(
                            node, [], cluster.gpu_metrics, reason="Node discovery failed"
                        )
                    cache.listed_at = cache.attempted_at = now
                    return self._view(cache)
                previous = {node["name"]: node.get("gpu") for node in cache.nodes or []}
                for node in nodes:
                    node["gpu"] = self._baseline(
                        node, previous.get(node["name"]), cluster.gpu_metrics
                    )
                cache.nodes, cache.listed_at = nodes, time.time()
            if (
                not self._closed.is_set()
                and not cache.refreshing
                and cache.nodes is not None
                and (refresh or now - cache.attempted_at >= CACHE_SECONDS)
            ):
                cache.refreshing, cache.attempted_at = True, now
                targets = copy.deepcopy(cache.nodes)
                for node in cache.nodes:
                    node["gpu"] = self._baseline(node, node.get("gpu"), cluster.gpu_metrics)
                threading.Thread(
                    target=self._refresh, args=(cluster_name, targets), daemon=True
                ).start()
            return self._view(cache)

    @staticmethod
    def _view(cache: _Cache) -> dict[str, Any]:
        nodes = copy.deepcopy(cache.nodes or [])
        for node in nodes:
            gpu = node["gpu"]
            checked_at = gpu.get("checked_at")
            if (
                checked_at is not None
                and time.time() - checked_at > STALE_SECONDS
                and gpu["state"] not in {"not_ready", "no_gpu"}
            ):
                gpu.update(state="unknown", idle=0, reason="GPU telemetry is stale")
        return {"nodes": nodes, "refreshing": cache.refreshing, "updated_at": cache.updated_at}

    def _session_targets(self, cluster: Cluster) -> dict[str, dict[str, Any]]:
        if self.sessions is None:
            return {}
        try:
            sessions = self.sessions()
        except Exception:
            return {}
        return {
            session["node"]: session
            for session in sessions
            if session.get("cluster") == cluster.name
            and session.get("cluster_fingerprint") == cluster.fingerprint
            and session.get("status") in {"ready", "disconnected"}
            and isinstance(session.get("uid"), str)
            and session["uid"]
            and all(
                isinstance(session.get(key), str) and session[key] for key in ("node", "pod", "id")
            )
        }

    def _collect(
        self,
        cluster: Cluster,
        node: dict[str, Any],
        exporter: str | None,
        session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        metrics = cluster.gpu_metrics
        result = gpu_status(node, [], metrics, reason="No ready GPU exporter for this node")
        if self._closed.is_set():
            return result
        kube = _ReadOnlyKubernetes(cluster)
        if exporter:
            namespace, pod = quote(metrics.namespace, safe=""), quote(exporter, safe="")
            path = f"/api/v1/namespaces/{namespace}/pods/{pod}:{metrics.port}/proxy/metrics"
            try:
                readings = parse_gpu_metrics(kube.call(["get", "--raw", path]), node["name"])
                result = gpu_status(node, readings, metrics, checked_at=time.time(), source="dcgm")
            except (PlaygroundError, ValueError, KeyError, TypeError, AttributeError):
                result = gpu_status(node, [], metrics, reason="GPU exporter telemetry unavailable")
        if result["state"] == "unknown" and session and not self._closed.is_set():
            try:
                readings = kube.inventory(session)
                result = gpu_status(
                    node, readings, metrics, checked_at=time.time(), source="playground"
                )
            except (PlaygroundError, ValueError, KeyError, TypeError, AttributeError):
                result = gpu_status(
                    node, [], metrics, reason="Owned Playground GPU telemetry unavailable"
                )
        return result

    def _refresh(self, cluster_name: str, nodes: list[dict[str, Any]]) -> None:
        cluster, cache = self.clusters[cluster_name], self._caches[cluster_name]
        pool: ThreadPoolExecutor | None = None
        try:
            exporters: dict[str, str] = {}
            try:
                metrics = cluster.gpu_metrics
                raw = _ReadOnlyKubernetes(cluster).call(
                    ["get", "pods", "-n", metrics.namespace, "-l", metrics.selector, "-o", "json"]
                )
                payload = json.loads(raw)
                for pod in payload.get("items", []):
                    if pod.get("status", {}).get("phase") != "Running" or not any(
                        condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in pod.get("status", {}).get("conditions", [])
                    ):
                        continue
                    name, node_name = pod["metadata"]["name"], pod.get("spec", {}).get("nodeName")
                    if isinstance(name, str) and isinstance(node_name, str):
                        exporters.setdefault(node_name, name)
            except (PlaygroundError, ValueError, KeyError, TypeError, AttributeError):
                pass
            sessions = self._session_targets(cluster)
            if self._closed.is_set():
                return
            targets = [
                node
                for node in nodes
                if node.get("ready") is not False
                and node.get("gpu_count") != 0
                and not node.get("gpu_mig")
            ]
            pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gpu-observation")
            futures = {
                pool.submit(
                    self._collect,
                    cluster,
                    node,
                    exporters.get(node["name"]),
                    sessions.get(node["name"]),
                ): node["name"]
                for node in targets
            }
            for future in as_completed(futures):
                if self._closed.is_set():
                    break
                name = futures[future]
                try:
                    status = future.result()
                except Exception:
                    node = next(node for node in nodes if node["name"] == name)
                    status = gpu_status(node, [], cluster.gpu_metrics)
                with cache.lock:
                    for cached_node in cache.nodes or []:
                        if cached_node["name"] == name:
                            cached_node["gpu"] = status
                            break
                    cache.updated_at = time.time()
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            with cache.lock:
                cache.refreshing = False
                cache.updated_at = time.time()

    def close(self) -> None:
        # Active reads finish within their bounded subprocess deadlines.
        self._closed.set()
