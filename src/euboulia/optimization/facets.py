"""Derived semantic facets for framework-native launch options."""

from __future__ import annotations

from collections.abc import Mapping

from euboulia.models import JSONScalar, JSONValue

_PARALLELISM_OPTIONS = frozenset(
    {
        "--cp-size",
        "--cp-strategy",
        "--dp-size",
        "--enable-cp-decode-attn-tp",
        "--enable-dp-attention",
        "--enable-dp-lm-head",
        "--enable-prefill-cp",
        "--ep-size",
        "--expert-parallel-size",
        "--moe-dense-tp-size",
        "--nnodes",
        "--node-rank",
        "--pp-size",
        "--tensor-parallel-size",
        "--tp-size",
    }
)

_MODEL_EXECUTION_OPTIONS = frozenset(
    {
        "--dtype",
        "--kv-cache-dtype",
        "--load-format",
        "--quantization",
        "--quantization-param-path",
    }
)


def _facet_name(option: str, *, prefix: str = "", suffix: str = "") -> str:
    name = option.removeprefix("--")
    if prefix:
        name = name.removeprefix(prefix)
    if suffix:
        name = name.removesuffix(suffix)
    return name.strip("-").replace("-", "_")


def _normalized_value(option: str, value: JSONScalar) -> JSONScalar:
    if not isinstance(value, str):
        return value
    if (
        option.endswith("-backend")
        or option in _MODEL_EXECUTION_OPTIONS
        or option == "--speculative-algorithm"
    ):
        return value.casefold()
    return value


def derive_sglang_launch_facets(
    options: Mapping[str, JSONScalar],
) -> Mapping[str, JSONValue]:
    """Extract recall facets without creating a second author-facing schema.

    Prefix/suffix rules intentionally cover future ``--speculative-*`` and
    ``--*-backend`` switches. Unknown options remain part of the full scenario
    digest through the compiled launch argv, even when they have no derived
    compatibility facet yet.
    """

    groups: dict[str, dict[str, JSONValue]] = {
        "backends": {},
        "model_execution": {},
        "parallelism": {},
        "speculative": {},
    }
    for option, raw_value in sorted(options.items()):
        if raw_value is None or raw_value is False:
            continue
        value = _normalized_value(option, raw_value)
        if option.startswith("--speculative-"):
            groups["speculative"][_facet_name(option, prefix="speculative-")] = value
        elif option.endswith("-backend"):
            groups["backends"][_facet_name(option, suffix="-backend")] = value
        elif option in _PARALLELISM_OPTIONS:
            groups["parallelism"][_facet_name(option)] = value
        elif option in _MODEL_EXECUTION_OPTIONS:
            groups["model_execution"][_facet_name(option)] = value
    return {name: values for name, values in groups.items() if values}


__all__ = ["derive_sglang_launch_facets"]
