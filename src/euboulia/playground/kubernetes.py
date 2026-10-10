"""Explicit-context kubectl transport and immutable Pod ownership checks."""

from __future__ import annotations

import copy
import csv
import io
import json
import subprocess
from typing import Any

from euboulia.playground.config import Cluster

MANAGED_LABEL = "app.kubernetes.io/managed-by"
SESSION_LABEL = "euboulia.io/playground-session"


class PlaygroundError(RuntimeError):
    """An actionable cluster or execution failure."""


def node_gpu_inventory(item: dict[str, Any]) -> tuple[int | None, bool]:
    """Read advertised physical inventory, never scheduled resource occupancy."""
    capacity = item.get("status", {}).get("capacity")
    labels = item.get("metadata", {}).get("labels", {})
    mig = False
    disabled = {"", "0", "false", "disabled", "all-disabled", "none", "off"}
    for key, value in labels.items():
        name, state = key.lower().split("/")[-1], str(value).lower()
        if name in {"mig.config", "gpu-mig-config", "mig.mode"} and state not in disabled:
            mig = True
        if name in {"gpu-mode", "gpu.mode"} and state == "mig":
            mig = True
    counts = []
    if not isinstance(capacity, dict):
        return None, mig
    for key, value in capacity.items():
        if not key.startswith("nvidia.com/"):
            continue
        if "mig" in key.lower():
            mig = mig or str(value) != "0"
            continue
        try:
            count = int(value)
        except (ValueError, TypeError):
            return None, mig
        if count < 0:
            return None, mig
        counts.append(count)
    # Missing device-plugin advertisement does not establish a physically GPU-free node.
    if not counts:
        return None, mig
    # Vendor aliases can advertise the same devices under multiple resource names.
    count = max(counts, default=0)
    gpu_hint = any("gpu" in key.lower() and value for key, value in labels.items())
    return (None if not count and gpu_hint else count), mig


def manifest(cluster: Cluster, session: dict[str, Any]) -> dict[str, Any]:
    pod = copy.deepcopy(cluster.template)
    pod.pop("status", None)
    meta = pod.setdefault("metadata", {})
    for key in (
        "uid",
        "resourceVersion",
        "creationTimestamp",
        "managedFields",
        "generateName",
        "ownerReferences",
        "finalizers",
        "deletionTimestamp",
        "deletionGracePeriodSeconds",
    ):
        meta.pop(key, None)
    meta.update(name=session["pod"], namespace=cluster.namespace)
    meta.setdefault("labels", {}).update(
        {MANAGED_LABEL: "molou-playground", SESSION_LABEL: session["id"]}
    )
    spec = pod["spec"]
    spec.update(nodeName=session["node"], restartPolicy="Never", automountServiceAccountToken=False)
    spec.pop("activeDeadlineSeconds", None)
    container = spec["containers"][0]
    container["command"] = [cluster.python, "-u", "-c", "import time; time.sleep(10**9)"]
    container["args"] = []
    for key in ("livenessProbe", "readinessProbe", "startupProbe", "lifecycle"):
        container.pop(key, None)
    env = [
        e
        for e in container.get("env", [])
        if e.get("name")
        not in {"NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "CUDA_VISIBLE_DEVICES"}
    ]
    env.extend(
        [
            {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
            {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"},
        ]
    )
    container["env"] = env
    return pod


def parse_gpus(output: str) -> list[dict[str, Any]]:
    result = []
    for row in csv.reader(io.StringIO(output), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 6:
            raise PlaygroundError("nvidia-smi returned an unexpected GPU inventory")
        index, uuid, name, total, used, utilization = (s.strip() for s in row)
        if not index.isdigit() or not uuid.startswith("GPU-"):
            raise PlaygroundError("GPU inventory must contain physical indices and GPU UUIDs")
        result.append(
            {
                "index": int(index),
                "uuid": uuid,
                "name": name,
                "memory_total_mb": total,
                "memory_used_mb": used,
                "utilization": utilization,
            }
        )
    if not result or len({g["index"] for g in result}) != len(result):
        raise PlaygroundError("no unique GPUs visible; check the shared NVIDIA runtime template")
    return result


class Kubernetes:
    def __init__(self, cluster: Cluster) -> None:
        self.cluster = cluster

    def prefix(self) -> list[str]:
        c = self.cluster
        result = [c.kubectl, "--context", c.context, "--namespace", c.namespace]
        if c.kubeconfig is not None:
            result += ["--kubeconfig", str(c.kubeconfig)]
        return result

    def call(self, args: list[str], *, data: str | None = None, timeout: int = 45) -> str:
        try:
            p = subprocess.run(
                [*self.prefix(), "--request-timeout=30s", *args],
                input=data,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PlaygroundError(f"kubectl failed: {exc}") from exc
        if p.returncode:
            raise PlaygroundError(p.stderr.strip()[:2000] or f"kubectl exited {p.returncode}")
        return p.stdout

    def nodes(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.call(["get", "nodes", *self.cluster.nodes, "-o", "json"]))
        except PlaygroundError:
            if not self.cluster.nodes:
                raise
            # Keep explicit targets usable without node-read RBAC, as before.
            return [
                {"name": n, "ip": None, "ready": None, "gpu_count": None, "gpu_mig": False}
                for n in self.cluster.nodes
            ]
        result = []
        for item in payload.get("items", [payload]):
            gpu_count, gpu_mig = node_gpu_inventory(item)
            status = item.get("status", {})
            addresses = status.get("addresses", [])
            ip = next(
                (
                    a["address"]
                    for kind in ("InternalIP", "ExternalIP")
                    for a in addresses
                    if a.get("type") == kind and a.get("address")
                ),
                None,
            )
            result.append(
                {
                    "name": item["metadata"]["name"],
                    "ip": ip,
                    "ready": any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in status.get("conditions", [])
                    ),
                    "gpu_count": gpu_count,
                    "gpu_mig": gpu_mig,
                }
            )
        if self.cluster.nodes:
            return sorted(result, key=lambda n: self.cluster.nodes.index(n["name"]))
        return sorted(result, key=lambda n: n["name"])

    def inspect(self, session: dict[str, Any], *, missing_ok: bool = False) -> dict[str, Any]:
        raw = self.call(["get", "pod", session["pod"], "-o", "json", "--ignore-not-found"])
        if not raw.strip():
            if missing_ok:
                return {}
            raise PlaygroundError("session Pod no longer exists; release it and connect again")
        pod: dict[str, Any] = json.loads(raw)
        m = pod.get("metadata", {})
        labels = m.get("labels", {})
        if (
            m.get("name") != session["pod"]
            or m.get("namespace") != self.cluster.namespace
            or labels.get(MANAGED_LABEL) != "molou-playground"
            or labels.get(SESSION_LABEL) != session["id"]
            or pod.get("spec", {}).get("nodeName") != session["node"]
            or not m.get("uid")
            or (session.get("uid") and session["uid"] != m["uid"])
        ):
            raise PlaygroundError("refusing to operate on a Pod with a different owner or UID")
        return pod

    def exec_args(self, session: dict[str, Any], command: list[str]) -> list[str]:
        return [
            *self.prefix(),
            "exec",
            "-i",
            session["pod"],
            "-c",
            self.cluster.container,
            "--",
            *command,
        ]

    def inventory(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        self.inspect(session)
        result = self.call(
            [
                "exec",
                session["pod"],
                "-c",
                self.cluster.container,
                "--",
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        return parse_gpus(result)

    def delete(self, session: dict[str, Any]) -> None:
        pod = self.inspect(session, missing_ok=True)
        if not pod:
            return
        uid = pod["metadata"]["uid"]
        uri = f"/api/v1/namespaces/{self.cluster.namespace}/pods/{session['pod']}"
        self.call(
            ["delete", f"--raw={uri}", "-f", "-"],
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "gracePeriodSeconds": 5,
                    "preconditions": {"uid": uid},
                }
            ),
        )
