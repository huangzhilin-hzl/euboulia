"""Execution identity shared by all run entry points."""

from __future__ import annotations

import secrets
import time

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_TIMESTAMP_MS = (1 << 48) - 1


def new_run_uid() -> str:
    """Return a time-sortable ULID with a stable execution-identity prefix."""

    timestamp_ms = time.time_ns() // 1_000_000
    if timestamp_ms > _MAX_TIMESTAMP_MS:  # pragma: no cover - beyond year 10889
        raise OverflowError("current time cannot be represented by a ULID")
    value = (timestamp_ms << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD_BASE32[value & 0x1F]
        value >>= 5
    return "run-" + "".join(encoded)


def normalize_run_name(value: str | None) -> str | None:
    """Normalize optional display metadata without turning it into a path key."""

    if value is None:
        return None
    selected = value.strip()
    if not selected:
        raise ValueError("name must not be empty")
    if len(selected) > 128:
        raise ValueError("name must not exceed 128 characters")
    return selected
