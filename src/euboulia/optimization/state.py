"""Explicit, fail-closed state transitions for optimization runs.

The event ledger records what happened; this module defines which lifecycle
transitions are legal.  Keeping the table small makes replay deterministic and
prevents a resumed run from skipping an approval or validation stage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .contracts import IterationState, RunState


class StateTransitionError(ValueError):
    """Raised when replay or live execution attempts an illegal transition."""


RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.PLANNED: frozenset({RunState.BASELINING, RunState.ITERATING, RunState.CANCELLED}),
    RunState.BASELINING: frozenset(
        {RunState.ITERATING, RunState.WAITING_FOR_APPROVAL, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.ITERATING: frozenset(
        {
            RunState.WAITING_FOR_APPROVAL,
            RunState.COMPLETED,
            RunState.STOPPED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.WAITING_FOR_APPROVAL: frozenset(
        {RunState.ITERATING, RunState.STOPPED, RunState.CANCELLED}
    ),
    RunState.COMPLETED: frozenset(),
    RunState.STOPPED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


ITERATION_TRANSITIONS: Mapping[IterationState, frozenset[IterationState]] = {
    IterationState.CREATED: frozenset({IterationState.PROFILING, IterationState.FAILED}),
    IterationState.PROFILING: frozenset({IterationState.ANALYZING, IterationState.FAILED}),
    IterationState.ANALYZING: frozenset({IterationState.PLANNING, IterationState.FAILED}),
    IterationState.PLANNING: frozenset(
        {
            IterationState.WAITING_FOR_APPROVAL,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.WAITING_FOR_APPROVAL: frozenset(
        {
            IterationState.PREPARING_BASELINE,
            IterationState.PREPARING_WORKSPACE,
            IterationState.RECORDING_MEMORY,
        }
    ),
    IterationState.PREPARING_BASELINE: frozenset(
        {
            IterationState.BUILDING_BASELINE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.BUILDING_BASELINE: frozenset(
        {
            IterationState.STARTING_BASELINE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.STARTING_BASELINE: frozenset(
        {
            IterationState.WAITING_FOR_BASELINE,
            IterationState.STOPPING_BASELINE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.WAITING_FOR_BASELINE: frozenset(
        {
            IterationState.EVALUATING_BASELINE,
            IterationState.STOPPING_BASELINE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.EVALUATING_BASELINE: frozenset(
        {
            IterationState.STOPPING_BASELINE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.STOPPING_BASELINE: frozenset(
        {
            IterationState.PREPARING_WORKSPACE,
            IterationState.RECORDING_MEMORY,
            IterationState.FAILED,
        }
    ),
    IterationState.PREPARING_WORKSPACE: frozenset(
        {IterationState.APPLYING_PATCH, IterationState.INVALID, IterationState.FAILED}
    ),
    IterationState.APPLYING_PATCH: frozenset(
        {
            IterationState.BUILDING,
            IterationState.EVALUATING,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.BUILDING: frozenset(
        {
            IterationState.STARTING_SERVICE,
            IterationState.RECORDING_MEMORY,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.STARTING_SERVICE: frozenset(
        {
            IterationState.WAITING_FOR_READY,
            IterationState.STOPPING_SERVICE,
            IterationState.RECORDING_MEMORY,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.WAITING_FOR_READY: frozenset(
        {
            IterationState.EVALUATING,
            IterationState.STOPPING_SERVICE,
            IterationState.RECORDING_MEMORY,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.EVALUATING: frozenset(
        {
            IterationState.STOPPING_SERVICE,
            IterationState.RECORDING_MEMORY,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.STOPPING_SERVICE: frozenset(
        {
            IterationState.RECORDING_MEMORY,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.RECORDING_MEMORY: frozenset(
        {
            IterationState.ACCEPTED,
            IterationState.REJECTED,
            IterationState.INVALID,
            IterationState.FAILED,
        }
    ),
    IterationState.ACCEPTED: frozenset(),
    IterationState.REJECTED: frozenset(),
    IterationState.INVALID: frozenset(),
    IterationState.FAILED: frozenset(),
}


def validate_run_transition(current: RunState, target: RunState) -> None:
    """Raise unless ``current -> target`` is an allowed run transition."""

    if target not in RUN_TRANSITIONS[current]:
        raise StateTransitionError(f"illegal run transition: {current.value} -> {target.value}")


def validate_iteration_transition(current: IterationState, target: IterationState) -> None:
    """Raise unless ``current -> target`` is an allowed iteration transition."""

    if target not in ITERATION_TRANSITIONS[current]:
        raise StateTransitionError(
            f"illegal iteration transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """Small immutable projection used by the runner and event replay."""

    run: RunState = RunState.PLANNED
    iteration: IterationState | None = None

    def move_run(self, target: RunState) -> Lifecycle:
        validate_run_transition(self.run, target)
        return replace(self, run=target)

    def begin_iteration(self) -> Lifecycle:
        if self.run is not RunState.ITERATING:
            raise StateTransitionError("an iteration can begin only while the run is iterating")
        if self.iteration is not None and self.iteration not in {
            IterationState.ACCEPTED,
            IterationState.REJECTED,
            IterationState.INVALID,
            IterationState.FAILED,
        }:
            raise StateTransitionError("the previous iteration is not terminal")
        return replace(self, iteration=IterationState.CREATED)

    def move_iteration(self, target: IterationState) -> Lifecycle:
        if self.iteration is None:
            raise StateTransitionError("no iteration has started")
        validate_iteration_transition(self.iteration, target)
        return replace(self, iteration=target)


__all__ = [
    "ITERATION_TRANSITIONS",
    "RUN_TRANSITIONS",
    "Lifecycle",
    "StateTransitionError",
    "validate_iteration_transition",
    "validate_run_transition",
]
