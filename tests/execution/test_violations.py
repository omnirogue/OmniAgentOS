"""Lane violation plans are pure, stable, and safe by default."""

from __future__ import annotations

import pytest

from omniagentos.execution.violations import (
    ViolationContext,
    longhaul_waiting_review,
    plan_violation_response,
    runner_quarantine_park,
    swarm_revert_flag,
)


def test_swarm_scope_violation_reverts_and_flags() -> None:
    plan = swarm_revert_flag(ViolationContext("swarm", "scope", "task-1"))

    assert plan.actions == ("revert_worktree", "flag_scope_violation", "notify_operator")
    assert plan.flag == "scope_violation"
    assert plan.park_state is None


def test_swarm_tier_p_violation_is_hard_blocked() -> None:
    plan = swarm_revert_flag(ViolationContext("swarm", "tier_p", "task-1", tier_p=True))

    assert plan.actions == ("revert_worktree", "block_tier_p", "notify_operator")
    assert plan.flag == "tier_p_violation"


def test_runner_violation_quarantines_then_parks() -> None:
    plan = runner_quarantine_park(ViolationContext("runner", "policy", "run-1"))

    assert plan.actions == ("quarantine_commit", "park_run", "notify_operator")
    assert plan.park_state == "waiting_review"


def test_longhaul_violation_waits_for_review() -> None:
    plan = longhaul_waiting_review(ViolationContext("longhaul", "undeclared_write", "lh-1"))

    assert plan.actions == ("set_park_state:waiting_review", "notify_operator")
    assert plan.park_state == "waiting_review"


@pytest.mark.parametrize("lane", ["swarm", "runner", "longhaul"])
def test_dispatch_uses_the_lane_handler(lane: str) -> None:
    plan = plan_violation_response(ViolationContext(lane, "scope", "holder-1"))  # type: ignore[arg-type]

    assert plan.lane == lane


def test_session_violation_fails_closed_until_a_handler_exists() -> None:
    with pytest.raises(ValueError, match="no violation response"):
        plan_violation_response(ViolationContext("session", "scope", "session-1"))
