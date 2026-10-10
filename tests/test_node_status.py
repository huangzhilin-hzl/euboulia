from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from euboulia.playground.config import Cluster, GpuMetrics
from euboulia.playground.kubernetes import Kubernetes, PlaygroundError, manifest, node_gpu_inventory
from euboulia.playground.node_status import (
    NodeMonitor,
    _ReadOnlyKubernetes,
    gpu_status,
    parse_gpu_metrics,
)


@pytest.fixture
def cluster() -> Cluster:
    return Cluster(
        name="test",
        context="explicit",
        namespace="molou",
        template={
            "apiVersion": "v1",
            "kind": "Pod",
            "spec": {"containers": [{"name": "worker", "image": "test/cuda"}]},
        },
        container="worker",
        python="python3",
        workdir="/workspace/molou",
        kubectl="kubectl",
        kubeconfig=None,
        nodes=(),
        startup_timeout=30,
    )


def node(count: int | None = 2, *, ready: bool = True, mig: bool = False) -> dict[str, Any]:
    return {"name": "node-a", "ip": "10.0.0.1", "ready": ready, "gpu_count": count, "gpu_mig": mig}


def reading(index: int, *, util: object = 0, used: object = 76) -> dict[str, Any]:
    return {
        "index": index,
        "uuid": f"GPU-{index}",
        "utilization": util,
        "memory_used_mb": used,
        "memory_free_mb": 100000,
    }


def metrics_output(count: int = 2, *, used: object = 76, util: object = 0) -> str:
    lines = []
    for index in range(count):
        labels = f'gpu="{index}",UUID="GPU-{index}",Hostname="node-a"'
        for metric, value in [("GPU_UTIL", util), ("FB_USED", used), ("FB_FREE", 100000)]:
            lines.append(f"DCGM_FI_DEV_{metric}{{{labels}}} {value}")
    return "\n".join(lines)


def classify(
    readings: list[dict[str, Any]], target: dict[str, Any] | None = None
) -> dict[str, Any]:
    return gpu_status(
        target or node(), readings, GpuMetrics(), checked_at=time.time(), source="dcgm"
    )


def test_idle_requires_every_physical_gpu_and_memory_idle() -> None:
    result = classify([reading(0), reading(1)])
    assert result["state"] == "idle"
    assert (result["idle"], result["measured"], result["total"]) == (2, 2, 2)
    assert result["max_utilization"] == 0 and result["max_memory_used_mb"] == 76
    result = classify([reading(0, used=91000), reading(1)])
    assert result["state"] == "busy" and result["idle"] == 1
    assert classify([reading(0, util=1), reading(1)])["state"] == "busy"


@pytest.mark.parametrize("invalid", [None, "N/A", "NaN", "inf", -1, 9.223372036854776e18])
def test_invalid_utilization_never_establishes_idle(invalid: object) -> None:
    assert classify([reading(0, util=invalid), reading(1)])["state"] == "unknown"


@pytest.mark.parametrize("invalid", [None, "N/A", "NaN", "inf", -1, 9.223372036854776e18])
def test_invalid_memory_never_establishes_idle(invalid: object) -> None:
    assert classify([reading(0, used=invalid), reading(1)])["state"] == "unknown"


def test_partial_inventory_unknown_but_any_observed_busy_gpu_is_busy() -> None:
    assert classify([reading(0)])["state"] == "unknown"
    assert classify([reading(0, used=91000)])["state"] == "busy"
    assert classify([reading(0), reading(1)], node(None))["state"] == "unknown"
    assert classify([reading(0), reading(1)], node(1))["state"] == "unknown"


def test_unknown_mappings_mig_and_stale_cannot_be_idle() -> None:
    duplicate_index = reading(1) | {"index": 0}
    assert classify([reading(0), duplicate_index])["state"] == "unknown"
    assert classify([reading(0), reading(0)])["state"] == "unknown"
    assert classify([reading(0), reading(1)], node(mig=True))["state"] == "unknown"
    old = gpu_status(node(), [reading(0), reading(1)], GpuMetrics(), checked_at=time.time() - 46)
    assert old["state"] == "unknown" and old["reason"] == "GPU telemetry is stale"
    assert classify([], node(0))["state"] == "no_gpu"
    assert classify([], node(2, ready=False))["state"] == "not_ready"


def test_threshold_configuration_and_inventory_memory_total() -> None:
    readings = [reading(0, util=2, used=512), reading(1, util=2, used=512)]
    for item in readings:
        item.pop("memory_free_mb")
        item["memory_total_mb"] = 100000
    result = gpu_status(
        node(),
        readings,
        GpuMetrics(idle_memory_mb=512, idle_utilization_percent=2),
        checked_at=time.time(),
    )
    assert result["state"] == "idle"


def test_dcgm_duplicate_pod_labels_are_deduplicated_by_uuid() -> None:
    output = metrics_output()
    duplicate = output.replace('Hostname="node-a"', 'Hostname="node-a",pod="another"')
    records = parse_gpu_metrics(output + "\n" + duplicate, "node-a")
    assert len(records) == 2 and classify(records)["state"] == "idle"
    conflict = duplicate.replace("} 76", "} 77")
    assert classify(parse_gpu_metrics(output + "\n" + conflict, "node-a"))["state"] == "unknown"


@pytest.mark.parametrize(
    "replacement",
    [
        ('Hostname="node-a"', 'Hostname="other"'),
        ('Hostname="node-a"', 'NodeName="other"'),
        ('gpu="1"', 'gpu="0"'),
        ('UUID="GPU-1"', 'UUID="GPU-0"'),
        ('UUID="GPU-0"', 'UUID="MIG-0"'),
        ('Hostname="node-a"', 'Hostname="node-a",GPU_I_ID="1"'),
        ('Hostname="node-a"', 'Hostname="node-a",GPU_I_PROFILE="3g.48gb"'),
    ],
)
def test_dcgm_rejects_wrong_node_mappings_and_mig(replacement: tuple[str, str]) -> None:
    with pytest.raises(ValueError):
        parse_gpu_metrics(metrics_output().replace(*replacement), "node-a")


def test_dcgm_hostname_optional_and_missing_fields_remain_unknown() -> None:
    output = metrics_output().replace(',Hostname="node-a"', "")
    assert classify(parse_gpu_metrics(output, "node-a"))["state"] == "idle"
    partial = "\n".join(line for line in output.splitlines() if "FB_FREE" not in line)
    assert classify(parse_gpu_metrics(partial, "node-a"))["state"] == "unknown"


def test_dcgm_explicit_old_sample_timestamps_cannot_look_fresh() -> None:
    def timestamped(seconds: float) -> str:
        stamp = int(seconds * 1000)
        return "\n".join(f"{line} {stamp}" for line in metrics_output().splitlines())

    recent = classify(parse_gpu_metrics(timestamped(time.time() - 2), "node-a"))
    assert recent["state"] == "idle"
    old = classify(parse_gpu_metrics(timestamped(time.time() - 86400), "node-a"))
    assert old["state"] == "unknown" and old["reason"] == "GPU telemetry is stale"
    mixed = metrics_output() + "\n" + timestamped(time.time() - 86400)
    assert classify(parse_gpu_metrics(mixed, "node-a"))["state"] == "unknown"
    with pytest.raises(ValueError, match="future"):
        parse_gpu_metrics(timestamped(time.time() + 60), "node-a")
    with pytest.raises(ValueError, match="timestamp"):
        parse_gpu_metrics(metrics_output() + " nonsense", "node-a")


def test_advertised_capacity_aliases_are_not_added_and_mig_is_detected() -> None:
    item = {"status": {"capacity": {"cpu": "64", "nvidia.com/gpu": "8", "nvidia.com/B300": "8"}}}
    assert node_gpu_inventory(item) == (8, False)
    item["status"]["capacity"]["nvidia.com/H20.mig-3g.48gb"] = "16"
    assert node_gpu_inventory(item) == (8, True)
    assert node_gpu_inventory({"status": {"capacity": {"cpu": "64"}}}) == (None, False)
    assert node_gpu_inventory({"status": {"capacity": {}}}) == (None, False)
    assert node_gpu_inventory({"status": {"capacity": {"nvidia.com/gpu": "0"}}}) == (0, False)
    assert node_gpu_inventory(
        {
            "status": {"capacity": {"nvidia.com/gpu": "0"}},
            "metadata": {"labels": {"custom/gpu-type": "B300"}},
        }
    ) == (None, False)
    assert node_gpu_inventory({}) == (None, False)
    assert node_gpu_inventory({"status": {"capacity": {"nvidia.com/gpu": "N/A"}}}) == (None, False)
    assert node_gpu_inventory(
        {"status": {"capacity": {}}, "metadata": {"labels": {"custom/gpu-type": "B300"}}}
    ) == (None, False)


def test_mig_capability_and_disabled_operator_configuration_remain_physical() -> None:
    item = {
        "status": {"capacity": {"nvidia.com/gpu": "8"}},
        "metadata": {
            "labels": {
                "nvidia.com/mig.capable": "true",
                "nvidia.com/mig.strategy": "none",
                "nvidia.com/mig.config": "all-disabled",
                "nvidia.com/mig.config.state": "success",
            }
        },
    }
    assert node_gpu_inventory(item) == (8, False)
    item["metadata"]["labels"]["nvidia.com/mig.strategy"] = "single"
    assert node_gpu_inventory(item) == (8, False)
    item["metadata"]["labels"]["nvidia.com/mig.config"] = "all-3g.48gb"
    assert node_gpu_inventory(item) == (8, True)
    item["metadata"]["labels"]["nvidia.com/mig.config"] = "all-disabled"
    item["metadata"]["labels"]["custom/gpu-mode"] = "mig"
    assert node_gpu_inventory(item) == (8, True)


@pytest.mark.parametrize("source", ["dcgm", "playground"])
@pytest.mark.parametrize("used,state", [(76, "unknown"), (91000, "busy")])
def test_missing_gpu_advertisement_keeps_node_selectable_and_observes_existing_sources(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    used: int,
    state: str,
) -> None:
    session = {
        "id": "session",
        "pod": "owned-pod",
        "node": "node-a",
        "uid": "uid-a",
        "cluster": "test",
        "cluster_fingerprint": cluster.fingerprint,
        "status": "ready",
    }
    owned = manifest(cluster, session)
    owned["metadata"]["uid"] = "uid-a"
    calls: list[list[str]] = []

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ["get", "nodes"]:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "node-a"},
                            "status": {
                                "capacity": {"cpu": "64", "memory": "512Gi"},
                                "conditions": [{"type": "Ready", "status": "True"}],
                                "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}],
                            },
                        }
                    ]
                }
            )
        if args[:2] == ["get", "pods"]:
            return exporter_payload() if source == "dcgm" else '{"items": []}'
        if args[:2] == ["get", "--raw"]:
            return metrics_output(used=used)
        if args[:2] == ["get", "pod"]:
            return json.dumps(owned)
        assert args[:2] == ["exec", "owned-pod"]
        return f"0, GPU-0, B300, 100000, {used}, 0\n1, GPU-1, B300, 100000, {used}, 0"

    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor(
        {"test": cluster}, sessions=lambda: [session] if source == "playground" else []
    )
    try:
        initial = monitor.snapshot("test")["nodes"][0]
        assert initial["gpu_count"] is None and initial["gpu"]["state"] == "unknown"
        result = wait_refresh(monitor)["nodes"][0]["gpu"]
        assert result["state"] == state
        assert result["source"] == source and result["measured"] == 2
        assert result["checked_at"] is not None
        operation = ["get", "--raw"] if source == "dcgm" else ["exec", "owned-pod"]
        assert any(args[:2] == operation for args in calls)
    finally:
        monitor.close()


def wait_refresh(monitor: NodeMonitor) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = monitor.snapshot("test")
        if not result["refreshing"]:
            return result
        time.sleep(0.005)
    pytest.fail("node telemetry refresh did not complete")


def exporter_payload() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "exporter-a"},
                    "spec": {"nodeName": "node-a"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
            ]
        }
    )


def test_monitor_returns_while_refreshing_singleflight_and_reuses_cache(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls = {"nodes": 0, "pods": 0, "metrics": 0}

    def nodes(self: Any) -> list[dict[str, Any]]:
        calls["nodes"] += 1
        return [node()]

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        if args[:2] == ["get", "pods"]:
            calls["pods"] += 1
            return exporter_payload()
        calls["metrics"] += 1
        started.set()
        assert release.wait(1)
        return metrics_output()

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", nodes)
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor({"test": cluster})
    try:
        initial = monitor.snapshot("test")
        assert initial["refreshing"] and initial["nodes"][0]["gpu"]["state"] == "unknown"
        assert started.wait(1)
        for _ in range(5):
            assert monitor.snapshot("test", refresh=True)["refreshing"]
        assert calls == {"nodes": 1, "pods": 1, "metrics": 1}
        release.set()
        result = wait_refresh(monitor)
        assert result["nodes"][0]["gpu"]["state"] == "idle"
        assert result["updated_at"] is not None
        result["nodes"][0]["gpu"]["state"] = "busy"
        assert monitor.snapshot("test")["nodes"][0]["gpu"]["state"] == "idle"
        assert calls == {"nodes": 1, "pods": 1, "metrics": 1}
    finally:
        release.set()
        monitor.close()


def test_failed_refresh_never_inherits_green(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail = False

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        if fail:
            raise PlaygroundError("unavailable")
        return exporter_payload() if args[:2] == ["get", "pods"] else metrics_output()

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor({"test": cluster})
    try:
        monitor.snapshot("test")
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "idle"
        fail = True
        monitor.snapshot("test", refresh=True)
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "unknown"
    finally:
        monitor.close()


def test_fresh_status_survives_inflight_refresh_but_failure_replaces_it(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    slow_failure = False

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        if args[:2] == ["get", "pods"]:
            return exporter_payload()
        if slow_failure:
            assert release.wait(1)
            raise PlaygroundError("timeout")
        return metrics_output()

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor({"test": cluster})
    try:
        monitor.snapshot("test")
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "idle"
        slow_failure = True
        pending = monitor.snapshot("test", refresh=True)
        assert pending["refreshing"] and pending["nodes"][0]["gpu"]["state"] == "idle"
        release.set()
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "unknown"
    finally:
        release.set()
        monitor.close()


def test_node_discovery_failure_uses_unknown_cached_nodes_without_retry_storm(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    fail = False

    def nodes(self: Any) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if fail:
            raise PlaygroundError("node read failed")
        return [node()]

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", nodes)
    monkeypatch.setattr(
        _ReadOnlyKubernetes,
        "call",
        lambda self, args, **kw: (
            exporter_payload() if args[:2] == ["get", "pods"] else metrics_output()
        ),
    )
    monitor = NodeMonitor({"test": cluster})
    try:
        monitor.snapshot("test")
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "idle"
        fail = True
        failure = monitor.snapshot("test", refresh=True)
        assert failure["nodes"][0]["gpu"]["state"] == "unknown"
        assert failure["nodes"][0]["gpu"]["reason"] == "Node discovery failed"
        assert not failure["refreshing"]
        for _ in range(5):
            assert monitor.snapshot("test")["nodes"][0]["gpu"]["state"] == "unknown"
        assert calls == 2
    finally:
        monitor.close()


@pytest.mark.parametrize(
    "replacement,state",
    [(node(0), "no_gpu"), (node(ready=False), "not_ready"), (node(mig=True), "unknown")],
)
def test_new_inventory_overrides_prior_green_immediately(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
    replacement: dict[str, Any],
    state: str,
) -> None:
    target = node()
    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [copy.deepcopy(target)])
    monkeypatch.setattr(
        _ReadOnlyKubernetes,
        "call",
        lambda self, args, **kw: (
            exporter_payload() if args[:2] == ["get", "pods"] else metrics_output()
        ),
    )
    monitor = NodeMonitor({"test": cluster})
    try:
        monitor.snapshot("test")
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "idle"
        target = replacement
        assert monitor.snapshot("test", refresh=True)["nodes"][0]["gpu"]["state"] == state
    finally:
        monitor.close()


def test_close_does_not_wait_for_inflight_remote_reads(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        if args[:2] == ["get", "pods"]:
            return exporter_payload()
        started.set()
        assert release.wait(1)
        return metrics_output()

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor({"test": cluster})
    try:
        monitor.snapshot("test")
        assert started.wait(1)
        before = time.monotonic()
        monitor.close()
        assert time.monotonic() - before < 0.1
    finally:
        release.set()
        monitor.close()


def test_monitor_stale_snapshot_becomes_unknown_after_close(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(
        _ReadOnlyKubernetes,
        "call",
        lambda self, args, **kw: (
            exporter_payload() if args[:2] == ["get", "pods"] else metrics_output()
        ),
    )
    monitor = NodeMonitor({"test": cluster})
    monitor.snapshot("test")
    result = wait_refresh(monitor)
    assert result["nodes"][0]["gpu"]["state"] == "idle"
    monitor.close()
    checked_at = result["nodes"][0]["gpu"]["checked_at"]
    monkeypatch.setattr("euboulia.playground.node_status.time.time", lambda: checked_at + 46)
    stale = monitor.snapshot("test")["nodes"][0]["gpu"]
    assert stale["state"] == "unknown" and stale["idle"] == 0


def test_owned_session_fallback_revalidates_uid_and_reads_live_inventory(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = {
        "id": "session",
        "pod": "owned-pod",
        "node": "node-a",
        "uid": "uid-a",
        "cluster": "test",
        "cluster_fingerprint": cluster.fingerprint,
        "status": "disconnected",
        "gpus": [reading(0), reading(1)],
    }
    pod = manifest(cluster, session)
    pod["metadata"]["uid"] = "uid-a"
    calls: list[list[str]] = []

    def call(self: Any, args: list[str], **kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ["get", "pods"]:
            return '{"items": []}'
        if args[:2] == ["get", "pod"]:
            return json.dumps(pod)
        assert args[:2] == ["exec", "owned-pod"]
        return "0, GPU-0, B300, 100000, 91000, 0\n1, GPU-1, B300, 100000, 76, 0"

    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", call)
    monitor = NodeMonitor({"test": cluster}, sessions=lambda: [copy.deepcopy(session)])
    try:
        monitor.snapshot("test")
        result = wait_refresh(monitor)["nodes"][0]["gpu"]
        assert result["state"] == "busy" and result["source"] == "playground"
        assert any(args[0] == "exec" for args in calls)
        calls.clear()
        pod["metadata"]["uid"] = "replacement"
        monitor.snapshot("test", refresh=True)
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "unknown"
        assert not any(args[0] == "exec" for args in calls)
    finally:
        monitor.close()


@pytest.mark.parametrize(
    "update",
    [{"uid": ""}, {"cluster_fingerprint": "wrong"}, {"status": "released"}, {"cluster": "other"}],
)
def test_untrusted_sessions_never_become_exec_targets(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
    update: dict[str, str],
) -> None:
    session = {
        "id": "session",
        "pod": "owned-pod",
        "node": "node-a",
        "uid": "uid-a",
        "cluster": "test",
        "cluster_fingerprint": cluster.fingerprint,
        "status": "ready",
    }
    session.update(update)
    monkeypatch.setattr(_ReadOnlyKubernetes, "nodes", lambda self: [node()])
    monkeypatch.setattr(_ReadOnlyKubernetes, "call", lambda self, args, **kw: '{"items": []}')
    monkeypatch.setattr(Kubernetes, "inventory", lambda *args: pytest.fail("untrusted exec"))
    monitor = NodeMonitor({"test": cluster}, sessions=lambda: [session])
    try:
        monitor.snapshot("test")
        assert wait_refresh(monitor)["nodes"][0]["gpu"]["state"] == "unknown"
    finally:
        monitor.close()


def test_transport_request_and_subprocess_deadlines_are_bounded(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> Any:
        observed.update(args=args, **kwargs)
        return type("Result", (), {"returncode": 0, "stdout": "{}"})()

    monkeypatch.setattr("euboulia.playground.kubernetes.subprocess.run", run)
    _ReadOnlyKubernetes(replace(cluster, context="explicit-context")).call(
        ["get", "nodes"], timeout=90
    )
    assert observed["timeout"] == 5
    args = observed["args"]
    assert args[args.index("--context") + 1] == "explicit-context"
    assert args.index("--request-timeout=4s") < args.index("get")
