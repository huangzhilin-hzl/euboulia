from euboulia.optimization.budget import BudgetLimits, BudgetTracker


def test_budget_reserves_before_iteration() -> None:
    now = [10.0]
    tracker = BudgetTracker(
        BudgetLimits(max_iterations=2, max_failures=2, max_wall_time_seconds=20),
        clock=lambda: now[0],
    )

    first = tracker.reserve_iteration()
    second = tracker.reserve_iteration()

    assert first.exhausted is False
    assert second.exhausted_reason == "iteration budget exhausted"
    assert tracker.reserve_iteration() == second


def test_budget_stops_on_failure_or_wall_time() -> None:
    now = [0.0]
    tracker = BudgetTracker(
        BudgetLimits(max_iterations=4, max_failures=1, max_wall_time_seconds=5),
        clock=lambda: now[0],
    )

    assert tracker.record_failure().exhausted_reason == "failure budget exhausted"

    other = BudgetTracker(
        BudgetLimits(max_iterations=4, max_failures=4, max_wall_time_seconds=5),
        clock=lambda: now[0],
    )
    now[0] = 5.0
    assert other.snapshot().exhausted_reason == "wall-time budget exhausted"
