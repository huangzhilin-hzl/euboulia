import pytest

from euboulia.optimization.contracts import IterationState, RunState
from euboulia.optimization.state import Lifecycle, StateTransitionError


def test_lifecycle_walks_the_review_boundary() -> None:
    lifecycle = Lifecycle().move_run(RunState.ITERATING).begin_iteration()
    for state in (
        IterationState.PROFILING,
        IterationState.ANALYZING,
        IterationState.PLANNING,
        IterationState.WAITING_FOR_APPROVAL,
        IterationState.PREPARING_WORKSPACE,
        IterationState.APPLYING_PATCH,
        IterationState.EVALUATING,
        IterationState.RECORDING_MEMORY,
        IterationState.ACCEPTED,
    ):
        lifecycle = lifecycle.move_iteration(state)

    assert lifecycle.iteration is IterationState.ACCEPTED


def test_lifecycle_refuses_to_skip_approval() -> None:
    lifecycle = Lifecycle().move_run(RunState.ITERATING).begin_iteration()
    lifecycle = lifecycle.move_iteration(IterationState.PROFILING)
    lifecycle = lifecycle.move_iteration(IterationState.ANALYZING)
    lifecycle = lifecycle.move_iteration(IterationState.PLANNING)

    with pytest.raises(StateTransitionError, match="planning -> preparing_workspace"):
        lifecycle.move_iteration(IterationState.PREPARING_WORKSPACE)


def test_terminal_run_cannot_restart() -> None:
    lifecycle = Lifecycle().move_run(RunState.ITERATING).move_run(RunState.COMPLETED)

    with pytest.raises(StateTransitionError, match="completed -> iterating"):
        lifecycle.move_run(RunState.ITERATING)
