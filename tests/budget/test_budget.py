from __future__ import annotations

import pytest

from omniagentos.budget import check
from omniagentos.contracts import BudgetSpec


@pytest.mark.parametrize(
    ("spec", "usage", "dimension"),
    [
        (BudgetSpec(wall_ms_max=99), (100, 0, 0.0), "wall_ms"),
        (BudgetSpec(tokens_max=99), (0, 100, 0.0), "tokens"),
        (BudgetSpec(cost_usd_max=20.0), (0, 0, 20.01), "cost_usd"),
    ],
)
def test_each_budget_dimension_can_deny(
    spec: BudgetSpec, usage: tuple[int, int, float], dimension: str
) -> None:
    decision = check(spec, *usage)

    assert not decision.allowed
    assert dimension in decision.reason


def test_unlimited_budget_allows_any_usage() -> None:
    assert check(BudgetSpec(), 1_000_000, 1_000_000, 1_000_000.0).allowed


def test_exact_boundary_is_allowed() -> None:
    decision = check(BudgetSpec(wall_ms_max=100, tokens_max=200, cost_usd_max=20.0), 100, 200, 20.0)

    assert decision.allowed
    assert decision.reason == ""


def test_first_exceeded_dimension_has_clear_reason() -> None:
    decision = check(BudgetSpec(cost_usd_max=20.0), 0, 0, 21.3)

    assert decision.reason == "cost_usd 21.30 > cap 20.00"


def test_iteration_cap_denies_when_turns_exceed_max(  # AC-policy loop stop
) -> None:
    """max_turns is the per-run iteration cap (the "Ralph Wiggum" stop)."""
    decision = check(BudgetSpec(max_turns=5), 0, 0, 0.0, used_turns=6)

    assert not decision.allowed
    assert decision.reason == "turns 6 > cap 5"


def test_iteration_cap_boundary_and_default_are_allowed() -> None:
    # Exactly at the cap is fine.
    assert check(BudgetSpec(max_turns=5), 0, 0, 0.0, used_turns=5).allowed
    # Four-argument callers (no turns) keep their exact behavior: no turn cap fires.
    assert check(BudgetSpec(max_turns=5), 0, 0, 0.0).allowed
