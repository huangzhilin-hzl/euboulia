"""Host-only configuration; credentials and Pod templates never enter the browser."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    packages: tuple[str, ...]
    system_site_packages: bool
    env: dict[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            [self.name, self.packages, self.system_site_packages, self.env], sort_keys=True
        )
        return hashlib.sha256(value.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Cluster:
    name: str
    context: str
    namespace: str
    template: dict[str, Any]
    container: str
    python: str
    workdir: str
    kubectl: str
    kubeconfig: Path | None
    nodes: tuple[str, ...]
    startup_timeout: int

    @property
    def fingerprint(self) -> str:
        # Template fixes must not prevent releasing an already owned Pod.
        value = json.dumps([self.context, self.namespace, str(self.kubeconfig)], sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class PlaygroundConfig:
    source: Path
    storage: Path
    clusters: dict[str, Cluster]
    profiles: dict[str, Profile]
    run_timeout: int
    setup_timeout: int
    max_output_bytes: int


def mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be a mapping with string keys")
    return value


def string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def integer(value: object, name: str, minimum: int = 1, maximum: int = 86400) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    if unknown := raw.keys() - allowed:
        raise ValueError(f"unknown {name} fields: {', '.join(sorted(unknown))}")


def _path(value: object, source: Path) -> Path:
    path = Path(string(value, "path")).expanduser()
    return (source.parent / path).resolve() if not path.is_absolute() else path.resolve()


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return tuple(string(v, name) for v in value)


def _identifier(value: object, name: str) -> str:
    result = string(value, name)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", result) or len(result) > 253:
        raise ValueError(f"invalid {name}: {result!r}")
    return result


def load_config(path: Path | None = None) -> PlaygroundConfig:
    source = (path or Path.home() / ".config/euboulia/playground.yaml").expanduser().resolve()
    try:
        raw = mapping(yaml.safe_load(source.read_text()), "playground")
    except FileNotFoundError as exc:
        raise ValueError(
            f"playground config missing: {source}; copy examples/playground.yaml and "
            "examples/playground-pod.yaml to a private local directory, then edit them"
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {source}") from exc
    _keys(
        raw,
        {
            "version",
            "storage",
            "clusters",
            "profiles",
            "run_timeout_seconds",
            "setup_timeout_seconds",
            "max_output_bytes",
        },
        "playground",
    )
    if integer(raw.get("version"), "version") != 1:
        raise ValueError("playground version must be 1")
    storage = _path(raw.get("storage", "~/.local/share/euboulia/playground"), source)
    if storage in {Path(storage.anchor), Path.home().resolve(), source.parent}:
        raise ValueError("storage must be a dedicated playground directory")
    clusters = {}
    for name, data in mapping(raw.get("clusters"), "clusters").items():
        _identifier(name, "cluster name")
        c = mapping(data, f"clusters.{name}")
        _keys(
            c,
            {
                "context",
                "namespace",
                "pod_template",
                "container",
                "python",
                "workdir",
                "kubectl",
                "kubeconfig",
                "nodes",
                "startup_timeout_seconds",
                "gpu_access",
            },
            f"clusters.{name}",
        )
        if c.get("gpu_access") != "shared-nvidia-runtime":
            raise ValueError(f"clusters.{name}.gpu_access must be shared-nvidia-runtime")
        template_path = _path(c.get("pod_template"), source)
        try:
            template = mapping(yaml.safe_load(template_path.read_text()), "Pod template")
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid Pod YAML: {template_path}") from exc
        if template.get("apiVersion") != "v1" or template.get("kind") != "Pod":
            raise ValueError("pod_template must be a v1 Pod")
        spec = mapping(template.get("spec"), "Pod spec")
        containers = spec.get("containers")
        if not isinstance(containers, list) or len(containers) != 1:
            raise ValueError("playground Pod must have exactly one container")
        if spec.get("initContainers") or spec.get("ephemeralContainers"):
            raise ValueError("playground Pod must not have init/ephemeral containers")
        container = mapping(containers[0], "container")
        container_name = _identifier(c.get("container", container.get("name")), "container")
        if container_name != container.get("name"):
            raise ValueError("configured container does not match the Pod template")
        string(container.get("image"), "container.image")
        resources = mapping(container.get("resources", {}), "resources")
        for section in ("requests", "limits"):
            for key in mapping(resources.get(section, {}), f"resources.{section}"):
                if "/" in key:
                    raise ValueError(
                        "shared-nvidia-runtime must not request extended resources "
                        f"({key}); use a dedicated template without GPU reservations"
                    )
        workdir = PurePosixPath(string(c.get("workdir", "/workspace/molou"), "workdir"))
        if not workdir.is_absolute() or ".." in workdir.parts or len(workdir.parts) < 3:
            raise ValueError("workdir must be a dedicated absolute Pod directory")
        context = string(c.get("context"), "context")
        python = string(c.get("python", "python3"), "python")
        if context.startswith("-") or python.startswith("-"):
            raise ValueError("context/python must not start with '-'")
        clusters[name] = Cluster(
            name=name,
            context=context,
            namespace=_identifier(c.get("namespace", "molou"), "namespace"),
            template=template,
            container=container_name,
            python=python,
            workdir=str(workdir),
            kubectl=string(c.get("kubectl", "kubectl"), "kubectl"),
            kubeconfig=_path(c["kubeconfig"], source) if "kubeconfig" in c else None,
            nodes=tuple(_identifier(n, "node") for n in _strings(c.get("nodes", []), "nodes")),
            startup_timeout=integer(c.get("startup_timeout_seconds", 300), "startup timeout"),
        )
    if not clusters:
        raise ValueError("configure at least one cluster")
    defaults = {
        "cutedsl": {"label": "CuTe DSL", "packages": ["nvidia-cutlass-dsl"]},
        "tilelang": {"label": "TileLang", "packages": ["tilelang"]},
        "python": {"label": "Python / PyTorch", "packages": []},
    }
    profiles = {}
    for name, data in mapping(raw.get("profiles", defaults), "profiles").items():
        _identifier(name, "profile name")
        p = mapping(data, f"profiles.{name}")
        _keys(p, {"label", "packages", "system_site_packages", "env"}, f"profiles.{name}")
        packages = _strings(p.get("packages", []), "packages")
        if any(item.startswith("-") for item in packages):
            raise ValueError("packages must be requirements, not pip options")
        site = p.get("system_site_packages", True)
        if not isinstance(site, bool):
            raise ValueError("system_site_packages must be a boolean")
        env = mapping(p.get("env", {}), "profile.env")
        for key, value in env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str):
                raise ValueError(
                    "profile.env requires environment variable names and string values"
                )
            if key in {
                "CUDA_VISIBLE_DEVICES",
                "NVIDIA_VISIBLE_DEVICES",
                "PYTHONHOME",
                "PYTHONPATH",
            }:
                raise ValueError(f"profile.env.{key} is managed by the playground")
            if "\x00" in value:
                raise ValueError("environment values must not contain NUL")
        profiles[name] = Profile(name, string(p.get("label", name), "label"), packages, site, env)
    if not profiles:
        raise ValueError("configure at least one profile")
    return PlaygroundConfig(
        source,
        storage,
        clusters,
        profiles,
        integer(raw.get("run_timeout_seconds", 120), "run timeout"),
        integer(raw.get("setup_timeout_seconds", 900), "setup timeout"),
        integer(
            raw.get("max_output_bytes", 8 * 1024 * 1024), "max output", 1024, 128 * 1024 * 1024
        ),
    )
