"""Content-addressed identities for optimization scenarios and memory recall."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from euboulia.models import JSONValue
from euboulia.optimization.config import OptimizationConfig, WorkloadPointConfig
from euboulia.optimization.facets import derive_sglang_launch_facets

IDENTITY_SCHEMA = "euboulia.scenario.v3"


@dataclass(frozen=True, slots=True)
class ScenarioIdentity:
    """Separated display aliases, semantic identity, and recall compatibility."""

    spec_digest: str
    model_digest: str
    workload_digest: str
    protocol_digest: str
    runtime_digest: str
    hardware_digest: str
    compatibility_digest: str
    aliases: Mapping[str, JSONValue]
    compatibility_facets: Mapping[str, JSONValue]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema": IDENTITY_SCHEMA,
            "spec_digest": self.spec_digest,
            "model_digest": self.model_digest,
            "workload_digest": self.workload_digest,
            "protocol_digest": self.protocol_digest,
            "runtime_digest": self.runtime_digest,
            "hardware_digest": self.hardware_digest,
            "compatibility_digest": self.compatibility_digest,
            "aliases": dict(self.aliases),
            "compatibility_facets": dict(self.compatibility_facets),
        }


def scenario_identity(config: OptimizationConfig, hardware_fingerprint: str) -> ScenarioIdentity:
    """Build an alias-independent, versioned identity for one normalized scenario."""

    model_payload = _model_payload(config)
    workload_payload = _workload_payload(config)
    protocol_payload = _protocol_payload(config)
    runtime_payload = _runtime_payload(config)
    model_digest = _digest("model", model_payload)
    workload_digest = _digest("workload", workload_payload)
    protocol_digest = _digest("protocol", protocol_payload)
    runtime_digest = _digest("runtime", runtime_payload)
    declared_hardware = {} if config.target is None else dict(config.target.hardware)
    hardware_digest = _digest(
        "hardware",
        {"fingerprint": hardware_fingerprint, "declared": declared_hardware},
    )
    hard_facets: dict[str, JSONValue] = {
        "framework": config.framework.value,
        "model_digest": model_digest,
        "hardware": (
            declared_hardware
            if declared_hardware
            else {"gpu_count": 0 if config.target is None else len(config.target.gpus)}
        ),
        "target_parameters": dict(config.baseline.target_parameters),
        "runtime_abi": _runtime_abi(config),
        "launch": _launch_facets(config),
    }
    compatibility_facets: dict[str, JSONValue] = {
        "hard": hard_facets,
        "soft": {
            "framework_revision": config.baseline.source_revision,
            "hardware_fingerprint": hardware_fingerprint,
            "workload_digest": workload_digest,
            "protocol_digest": protocol_digest,
        },
    }
    compatibility_digest = _digest("compatibility", hard_facets)
    spec_digest = _digest(
        "spec",
        {
            "model_digest": model_digest,
            "workload_digest": workload_digest,
            "protocol_digest": protocol_digest,
            "runtime_digest": runtime_digest,
            "hardware_digest": hardware_digest,
        },
    )
    return ScenarioIdentity(
        spec_digest=spec_digest,
        model_digest=model_digest,
        workload_digest=workload_digest,
        protocol_digest=protocol_digest,
        runtime_digest=runtime_digest,
        hardware_digest=hardware_digest,
        compatibility_digest=compatibility_digest,
        aliases={
            "campaign": config.name,
            "models": {
                "target": config.models.target.name,
                "drafts": [draft.name for draft in config.models.drafts],
            },
            "workload": config.workload_suite.name,
            "points": [point.name for point in config.workload_suite.points],
            "baseline": config.baseline.name,
        },
        compatibility_facets=compatibility_facets,
    )


def point_digest(point: WorkloadPointConfig) -> str:
    """Return the content identity of a workload point, excluding its alias."""

    return _digest("workload-point", _point_payload(point))


def _digest(kind: str, value: object) -> str:
    envelope = {"schema": IDENTITY_SCHEMA, "kind": kind, "value": value}
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _model_payload(config: OptimizationConfig) -> dict[str, object]:
    def artifact_payload(index: int, revision: str, weights: str | None) -> dict[str, object]:
        return {"position": index, "revision": revision, "weights_manifest_sha256": weights}

    return {
        "target": artifact_payload(
            0,
            config.models.target.revision,
            config.models.target.weights_manifest_sha256,
        ),
        "drafts": [
            artifact_payload(index, draft.revision, draft.weights_manifest_sha256)
            for index, draft in enumerate(config.models.drafts, start=1)
        ],
    }


def _point_payload(point: WorkloadPointConfig) -> dict[str, object]:
    return {
        "input_tokens": point.input_tokens,
        "output_tokens": point.output_tokens,
        "concurrency": point.concurrency,
        "num_prompts": point.num_prompts,
        "request_rate": point.request_rate,
    }


def _workload_payload(config: OptimizationConfig) -> dict[str, object]:
    return {
        "dataset": config.workload_suite.dataset,
        "request_rate": config.workload_suite.request_rate,
        # Sequence is deliberately preserved: execution order can affect cache/JIT state.
        "points": [_point_payload(point) for point in config.workload_suite.points],
    }


def _point_references(config: OptimizationConfig) -> dict[str, str]:
    occurrences: Counter[str] = Counter()
    references: dict[str, str] = {}
    for point in config.workload_suite.points:
        digest = point_digest(point)
        occurrences[digest] += 1
        references[point.name] = f"{digest}:{occurrences[digest]}"
    return references


def _protocol_payload(config: OptimizationConfig) -> dict[str, object]:
    evaluation = config.optimization.evaluation
    promotion = evaluation.promotion
    point_references = _point_references(config)
    return {
        "benchmark": {
            "mode": config.benchmark.mode,
            "base_args": list(config.benchmark.base_args),
            "parameters": dict(config.benchmark.parameters),
        },
        "evaluation": {
            "metric": evaluation.metric,
            "direction": evaluation.direction.value,
            "minimum": evaluation.min_relative_improvement,
            "regression": evaluation.max_regression,
            "noise": evaluation.noise_tolerance,
            "baseline_value": None if config.target is not None else evaluation.baseline_value,
            "baseline_values": {
                point_references[alias]: value
                for alias, value in config.baseline.metric_values.items()
            },
            "promotion": (
                None
                if promotion is None
                else {
                    "primary_points": [
                        point_references[alias] for alias in promotion.primary_points
                    ],
                    "require_all_points_valid": promotion.require_all_points_valid,
                    "min_relative_improvement": promotion.min_relative_improvement,
                    "max_regression_per_point": promotion.max_regression_per_point,
                    "noise_tolerance": promotion.noise_tolerance,
                }
            ),
            "tiers": [
                {
                    "kind": tier.kind.value,
                    "warmups": tier.warmups,
                    "repetitions": tier.repetitions,
                    "timeout_seconds": tier.timeout_seconds,
                    "required_metrics": list(tier.required_metrics),
                    "commands": [
                        {
                            # Command names are display aliases; argv/env are executable content.
                            "argv": list(command.argv),
                            "timeout_seconds": command.timeout_seconds,
                            "env": dict(sorted(command.env.items())),
                        }
                        for command in tier.commands
                    ],
                }
                for tier in evaluation.tiers
            ],
        },
    }


def _model_aliases(config: OptimizationConfig) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for index, model in enumerate((config.models.target, *config.models.drafts)):
        placeholder = f"<model:{index}>"
        aliases[model.path] = placeholder
        aliases[model.served_name] = placeholder
        aliases[model.name] = placeholder
    return aliases


def _launch_facets(config: OptimizationConfig) -> dict[str, JSONValue]:
    target = config.target
    if target is None:
        return {}
    aliases = _model_aliases(config)
    return {
        name: _replace_alias_json(value, aliases)
        for name, value in derive_sglang_launch_facets(target.launch.options).items()
    }


def _runtime_payload(config: OptimizationConfig) -> dict[str, object]:
    target = config.target
    if target is None:
        return {
            "framework": config.framework.value,
            "framework_revision": config.baseline.source_revision,
            "target_parameters": dict(config.baseline.target_parameters),
            "managed": None,
        }

    aliases = _model_aliases(config)

    runtime: object = None
    if target.runtime is not None:
        container = target.runtime.expected.container
        runtime = {
            "container": (
                None
                if container is None
                else {"image": container.image, "digest": container.digest}
            ),
            "components": {
                name: {
                    "version": component.version,
                    "revision": component.revision,
                    "digest": component.digest,
                    "dirty": component.dirty,
                    "metadata": dict(component.metadata),
                }
                for name, component in sorted(target.runtime.expected.components.items())
            },
        }

    build: object = None
    if target.build is not None:
        build = [
            {
                "argv": _replace_aliases(command.argv, aliases),
                "timeout_seconds": command.timeout_seconds,
                "env": _replace_alias_mapping(command.env, aliases),
            }
            for command in target.build.commands
        ]
    return {
        "framework": config.framework.value,
        "framework_revision": config.baseline.source_revision,
        "target_parameters": dict(config.baseline.target_parameters),
        "managed": {
            "provider": target.provider.value,
            "launch_argv": _replace_aliases(config.target_launch_argv, aliases),
            "launch_env": _replace_alias_mapping(target.launch.env, aliases),
            "build": build,
            "provenance": dict(target.provenance),
            "runtime": runtime,
        },
    }


def _runtime_abi(config: OptimizationConfig) -> JSONValue:
    target = config.target
    if target is None or target.runtime is None:
        return None
    container = target.runtime.expected.container
    return {
        "container": (
            None
            if container is None
            else {"image": container.image, "digest": container.digest}
        ),
        "components": {
            name: {
                "version": component.version,
                "revision": component.revision,
                "digest": component.digest,
                "dirty": component.dirty,
            }
            for name, component in sorted(target.runtime.expected.components.items())
        },
    }


def _replace_aliases(values: Sequence[str], aliases: Mapping[str, str]) -> list[str]:
    return [aliases.get(value, value) for value in values]


def _replace_alias_mapping(
    values: Mapping[str, str | None], aliases: Mapping[str, str]
) -> dict[str, str | None]:
    return {
        key: None if value is None else aliases.get(value, value)
        for key, value in sorted(values.items())
    }


def _replace_alias_json(value: JSONValue, aliases: Mapping[str, str]) -> JSONValue:
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [_replace_alias_json(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _replace_alias_json(item, aliases) for key, item in value.items()}
    return value


__all__ = ["IDENTITY_SCHEMA", "ScenarioIdentity", "point_digest", "scenario_identity"]
