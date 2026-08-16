"""Regression tests for acceptance-rate auto-pause bug with pending gate results.

Bug: The acceptance-rate floor calculation was counting PENDING gate results
as rejections, causing immediate auto-pause of a routine that fired 3 times
before any gates had been settled.

Fix requirement: Only count SETTLED runs (where gate_passed is NOT NULL) when
computing the acceptance rate. An all-pending window must never trigger auto-pause.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FINISHED_AT = "2026-01-01T09:01:00Z"


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "acceptance_rate_test.db")


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


def test_three_pending_runs_must_not_autopause(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Regression: 3 fired runs with all gate results pending should NOT auto-pause.

    This is the exact scenario that killed the production routines:
    - Record 3 runs directly with gate_passed=None (pending)
    - The bug counted pending runs as rejections, causing auto-pause at 0%
    - The fix should exclude pending runs from the acceptance-rate calculation

    Expected: routine remains active after 3 pending runs
    Bug behavior: routine is auto-paused after 3 pending runs
    """
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Pending gate test", "harness": "mock"},
    )
    routine = routines.create_routine(payload)
    assert routine["status"] == "active"
    assert routine["total_runs"] == 0

    # Directly record 3 runs with pending gate results (simulating what tick does)
    # This is what happened in production: 3 runs were fired and recorded with
    # notes='fired: task and run created; gate result pending'
    for i in range(3):
        run_data = {
            "run_id": f"run-pending-{i}",
            "iteration": i + 1,
            "gate_passed": None,  # PENDING
            "accepted": None,  # Not yet settled
            "notes": "fired: task and run created; gate result pending",
        }
        routines.record_run(routine["id"], run_data)

    # After 3 fires with pending gates, the routine should still be active
    # The bug causes it to be auto-paused because it counts pending as 0 acceptances
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["status"] == "active", (
        f"BUG: Routine was auto-paused when all 3 runs have pending gate results. "
        f"Status: {updated_routine['status']}, reason: '{updated_routine['auto_pause_reason']}'. "
        f"Pending runs must be excluded from acceptance-rate calculation."
    )
    assert updated_routine["total_runs"] == 3
    assert updated_routine["accepted_runs"] == 0


def test_three_settled_rejections_must_autopause(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Verification: 3 settled rejections should still auto-pause as expected.

    This ensures we didn't break the intended auto-pause behavior. When 3 runs
    are fully settled with all rejections (gate_passed=False), the routine
    should auto-pause due to 0% acceptance rate.
    """
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Rejection test", "harness": "mock"},
    )
    routine = routines.create_routine(payload)
    assert routine["status"] == "active"

    # Record 3 runs as settled rejections
    for i in range(3):
        run_data = {
            "run_id": f"run-rejected-{i}",
            "iteration": i + 1,
            "gate_passed": False,  # SETTLED: rejected
            "accepted": False,
            "finished_at": FINISHED_AT,
        }
        routines.record_run(routine["id"], run_data)

    # After settling 3 rejections, the routine SHOULD be auto-paused
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["status"] == "auto_paused", (
        f"Routine SHOULD be auto-paused after 3 settled rejections (0% acceptance rate). "
        f"Status: {updated_routine['status']}"
    )
    assert updated_routine["total_runs"] == 3
    assert updated_routine["accepted_runs"] == 0
    assert updated_routine["acceptance_rate"] == 0.0


def test_mixed_pending_and_settled_only_counts_settled(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Acceptance rate should only be computed over SETTLED runs.

    With 2 settled rejections and 1 pending:
    - Only the 2 settled rejections count toward acceptance rate
    - Acceptance rate = 0 / 2 = 0%
    - But we need at least 3 SETTLED runs before auto-pausing
    - So routine should NOT auto-pause yet
    """
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Mixed settle test", "harness": "mock"},
    )
    routine = routines.create_routine(payload)

    # Record 2 settled rejections and 1 pending
    for i in range(3):
        if i < 2:
            # First 2: settled rejections
            run_data = {
                "run_id": f"run-rejected-{i}",
                "iteration": i + 1,
                "gate_passed": False,
                "accepted": False,
                "finished_at": FINISHED_AT,
            }
        else:
            # Third: pending
            run_data = {
                "run_id": f"run-pending-{i}",
                "iteration": i + 1,
                "gate_passed": None,  # PENDING
                "accepted": None,
            }
        routines.record_run(routine["id"], run_data)

    # With only 2 settled runs, we should NOT auto-pause (need 3 settled minimum)
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["status"] == "active", (
        f"Routine should NOT auto-pause with only 2 settled rejections (need 3+ settled). "
        f"Status: {updated_routine['status']}"
    )
    assert updated_routine["total_runs"] == 3


def test_firing_path_must_not_skip_routine_with_pending_runs(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Regression: should_fire() must NOT skip routines with only pending runs.

    This is the firing-path counterpart to the record/settle bug. Even though
    the routine has persisted acceptance_rate=0 (from counting 3 pending runs as
    rejections), should_fire() must compute the actual settled rate and NOT skip
    the routine due to the floor check.

    Live evidence: after unpausing the routine, the tick still skipped it because
    should_fire() was reading the poisoned persisted counters.
    """
    from omniagentos.scheduler.routines import should_fire

    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Firing test", "harness": "mock"},
    )
    routine = routines.create_routine(payload)

    # Record 3 runs with pending gates
    for i in range(3):
        run_data = {
            "run_id": f"run-pending-fire-{i}",
            "iteration": i + 1,
            "gate_passed": None,  # PENDING
            "accepted": None,
        }
        routines.record_run(routine["id"], run_data)

    # Get the routine with poisoned persisted counters
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["total_runs"] == 3
    assert updated_routine["accepted_runs"] == 0
    assert updated_routine["acceptance_rate"] == 0.0
    assert updated_routine["status"] == "active"

    # should_fire() should NOT skip it due to acceptance floor
    # when passed the actual settled counts (which the tick does)
    fire, reason = should_fire(
        updated_routine,
        now=NOW,
        settled_runs=0,  # No settled runs (all pending)
        settled_accepted=0,
    )
    assert fire is True, (
        f"BUG: should_fire() skipped routine with pending runs. "
        f"Reason: {reason}. "
        f"Routine has persisted acceptance_rate=0% but all 3 runs are still pending. "
        f"should_fire() must compute settled-only rate, not use poisoned persisted counters."
    )


def test_firing_path_still_skips_on_settled_rejections(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Verification: should_fire() MUST still skip if 3+ runs are settled rejections.

    Ensure the firing-path fix doesn't break the intended floor behavior.
    """
    from omniagentos.scheduler.routines import should_fire

    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Settled rejection fire test", "harness": "mock"},
    )
    routine = routines.create_routine(payload)

    # Record 3 runs as settled rejections
    for i in range(3):
        run_data = {
            "run_id": f"run-rejected-fire-{i}",
            "iteration": i + 1,
            "gate_passed": False,
            "accepted": False,
            "finished_at": FINISHED_AT,
        }
        routines.record_run(routine["id"], run_data)

    # Get the routine (auto-paused after 3 settled rejections)
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["status"] == "auto_paused"
    assert updated_routine["acceptance_rate"] == 0.0

    # should_fire() should already skip it because status=auto_paused
    fire, reason = should_fire(updated_routine, now=NOW)
    assert fire is False
    assert "status" in reason.lower()
