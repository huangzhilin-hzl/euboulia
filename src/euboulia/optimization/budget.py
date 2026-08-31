"""Multi-dimensional, fail-closed budget accounting for iterative runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_iterations: int
    max_failures: int
    max_wall_time_seconds: float

    def __post_init__(self) -> None:
        for name in ("max_iterations", "max_failures"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if isinstance(self.max_wall_time_seconds, bool) or not isinstance(
            self.max_wall_time_seconds, int | float
        ):
            raise TypeError("max_wall_time_seconds must be numeric")
        if self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    iterations_started: int
    failures: int
    elapsed_seconds: float
    exhausted_reason: str | None

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None


class BudgetTracker:
    """Reserve iteration budget before expensive work begins.

    This tracker is intentionally process-local in the first runtime. The
    reservation is emitted to the event ledger by the runner before any
    workspace or evaluator side effect, so a future distributed implementation
    can replace this class without changing component protocols.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        self._iterations_started = 0
        self._failures = 0

    def reserve_iteration(self) -> BudgetSnapshot:
        snapshot = self.snapshot()
        if snapshot.exhausted:
            return snapshot
        self._iterations_started += 1
        return self.snapshot()

    def record_failure(self) -> BudgetSnapshot:
        self._failures += 1
        return self.snapshot()

    def snapshot(self) -> BudgetSnapshot:
        elapsed = max(0.0, self._clock() - self._started_at)
        reason: str | None = None
        if elapsed >= self.limits.max_wall_time_seconds:
            reason = "wall-time budget exhausted"
        elif self._iterations_started >= self.limits.max_iterations:
            reason = "iteration budget exhausted"
        elif self._failures >= self.limits.max_failures:
            reason = "failure budget exhausted"
        return BudgetSnapshot(
            iterations_started=self._iterations_started,
            failures=self._failures,
            elapsed_seconds=elapsed,
            exhausted_reason=reason,
        )


__all__ = ["BudgetLimits", "BudgetSnapshot", "BudgetTracker"]
