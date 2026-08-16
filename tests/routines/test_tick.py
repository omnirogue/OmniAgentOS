"""Integration tests for omniagentos.scheduler.routines_tick: does a DUE
routine actually fire (create a task + run + routine_runs row), and do
paused / over-cap / sub-50%-acceptance / gate-missing routines correctly
refuse to fire."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # matches "* * * * *" at any minute


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "routines_tick.db")


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


@pytest.fixture
def policy():
    return load_policy()


def _cron_routine(routines: RoutinesStore, **overrides: object) -> dict:
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Routine-fired task", "harness": "mock"},
    )
    payload.update(overrides)
    return routines.create_routine(payload)


def test_due_valid_routine_fires_creates_run_and_routine_run(database, routines, policy) -> None:
    created = _cron_routine(routines)

    result = tick(database, policy, now=NOW)

    assert len(result["fired"]) == 1
    entry = result["fired"][0]
    assert entry["routine_id"] == created["id"]
    assert entry["fired"] is True
    assert entry["task_id"]
    assert entry["run_id"]

    task = database.get_task(entry["task_id"])
    assert task is not None
    assert task["title"] == "Routine-fired task"

    run = database.get_run(entry["run_id"])
    assert run is not None
    assert run["task_id"] == entry["task_id"]
    assert run["state"] == "queued"

    updated = routines.get_routine(created["id"])
    assert updated["total_runs"] == 1
    assert updated["last_fired"] is not None

    runs = routines.list_runs(created["id"])
    assert len(runs) == 1
    assert runs[0]["run_id"] == entry["run_id"]
    assert runs[0]["iteration"] == 1


def test_second_tick_in_the_same_minute_does_not_refire(database, routines, policy) -> None:
    _cron_routine(routines)
    tick(database, policy, now=NOW)
    result = tick(database, policy, now=NOW)
    assert result["fired"] == []
    assert result["skipped"][0]["reason"] == "cron trigger not due"


def test_disabled_routine_does_not_fire(database, routines, policy) -> None:
    created = _cron_routine(routines, status="disabled")

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    assert any(s["routine_id"] == created["id"] for s in result["skipped"])
    assert routines.get_routine(created["id"])["total_runs"] == 0
    assert routines.list_runs(created["id"]) == []


def test_routine_over_its_max_iterations_hard_cap_does_not_fire(database, routines, policy) -> None:
    created = _cron_routine(routines, hard_cap_type="max_iterations", hard_cap_value=2)
    routines.record_run(created["id"], {"iteration": 1, "accepted": True, "cost_usd": 0.0})
    routines.record_run(created["id"], {"iteration": 2, "accepted": True, "cost_usd": 0.0})

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    reason = next(s["reason"] for s in result["skipped"] if s["routine_id"] == created["id"])
    assert "hard stop-condition" in reason
    # Unchanged: the hard cap block happened before any new run was recorded.
    assert routines.get_routine(created["id"])["total_runs"] == 2


def test_routine_over_its_budget_hard_cap_does_not_fire(database, routines, policy) -> None:
    created = _cron_routine(routines, hard_cap_type="budget_usd", hard_cap_value=5.0)
    routines.record_run(created["id"], {"iteration": 1, "accepted": True, "cost_usd": 6.0})

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    assert routines.get_routine(created["id"])["total_runs"] == 1


def _settled_run(iteration: int, accepted: bool, **overrides: object) -> dict[str, object]:
    """A run in the shape production settles it — gate verdict plus finish
    stamp. Runs missing either are pending and never reach the floor."""
    run: dict[str, object] = {
        "iteration": iteration,
        "gate_passed": accepted,
        "accepted": accepted,
        "cost_usd": 0.0,
        "stop_reason": "gate_passed" if accepted else "gate_failed",
        "finished_at": f"2026-01-01T08:0{iteration}:00Z",
    }
    run.update(overrides)
    return run


def test_sub_50pct_acceptance_routine_does_not_fire(database, routines, policy) -> None:
    created = _cron_routine(routines)
    # 3 settled runs, 1 accepted -> 33% acceptance, below the 50% floor ->
    # record_run itself auto-pauses the routine (RoutinesStore.record_run).
    routines.record_run(created["id"], _settled_run(1, True))
    routines.record_run(created["id"], _settled_run(2, False))
    routines.record_run(created["id"], _settled_run(3, False))
    assert routines.get_routine(created["id"])["status"] == "auto_paused"

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    # No new run was recorded on top of the 3 that tripped auto-pause.
    assert routines.get_routine(created["id"])["total_runs"] == 3


def test_routine_whose_settlements_are_all_ungateable_still_fires(
    database, routines, policy
) -> None:
    """I-0, the live outage: with no gate workspace every settlement came back
    ``gate_evidence_unavailable`` written as a rejection, so acceptance could
    never clear the floor and the tick skipped both autonomous routines forever.
    Un-gateable settlements carry no acceptance signal and must not suppress
    firing, whichever shape they were written in."""
    created = _cron_routine(routines)
    for iteration in range(1, 5):
        routines.record_run(
            created["id"],
            _settled_run(iteration, False, stop_reason="gate_evidence_unavailable"),
        )

    result = tick(database, policy, now=NOW)

    assert [entry["routine_id"] for entry in result["fired"]] == [created["id"]]
    assert not [s for s in result["skipped"] if s["routine_id"] == created["id"]]


def test_a_routine_the_tick_suppresses_reports_itself_as_paused(database, routines, policy) -> None:
    """I-6: a routine the tick refuses to fire must never still describe itself
    as healthy. The outage was invisible because rejected settlements left
    status='active' with an empty auto_pause_reason while the tick skipped the
    routine on the floor every five minutes."""
    created = _cron_routine(routines)
    pending = [
        routines.record_run(created["id"], {"iteration": i, "run_id": f"run-{i}", "cost_usd": 0.0})
        for i in range(1, 4)
    ]
    for i, run in enumerate(pending, start=1):
        routines.settle_run(
            run["id"],
            gate_passed=False,
            accepted=False,
            finished_at=f"2026-01-01T08:0{i}:00Z",
            stop_reason="gate_failed",
        )

    routine = routines.get_routine(created["id"])
    assert routine is not None
    assert routine["status"] == "auto_paused"
    assert "50%" in routine["auto_pause_reason"]

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    for skip in result["skipped"]:
        current = routines.get_routine(skip["routine_id"])
        assert current is not None
        suppressed_by_the_floor = "acceptance rate" in skip["reason"]
        reports_healthy = current["status"] == "active" and not current["auto_pause_reason"]
        assert not (suppressed_by_the_floor and reports_healthy), (
            f"routine {skip['routine_id']} is skipped for {skip['reason']!r} "
            f"but reports status={current['status']!r} / "
            f"auto_pause_reason={current['auto_pause_reason']!r}"
        )


def test_routine_missing_its_objective_gate_is_never_fired(database, routines, policy) -> None:
    """A routine that reached the table WITH a gate (required at create time)
    but is later corrupted to be missing one (e.g. a direct DB edit bypassing
    RoutinesStore) must never fire — the tick's belt-and-braces
    re-validation has to catch it, not just the DAL's write-time check."""
    created = _cron_routine(routines)
    # Simulate a row that bypassed RoutinesStore.update_routine's revalidation
    # (which would have rejected this): blank out the gate's required config.
    database._write("UPDATE routines SET gate_config_json = '{}' WHERE id = ?", (created["id"],))

    result = tick(database, policy, now=NOW)

    assert result["fired"] == []
    reason = next(s["reason"] for s in result["skipped"] if s["routine_id"] == created["id"])
    assert "invalid routine" in reason
    assert routines.get_routine(created["id"])["total_runs"] == 0
    assert routines.list_runs(created["id"]) == []


def test_event_triggered_routine_fires_only_on_a_new_matching_event(
    database, routines, policy
) -> None:
    # insert_event() stamps real wall-clock timestamps (contracts.utc_now_iso),
    # so this test drives tick() with a real "now" too, matching production
    # (where `now` always IS the real clock) rather than the fixed historical
    # NOW the cron tests use.
    now = datetime.now(UTC)
    created = routines.create_routine(
        valid_routine_payload(
            trigger_type="event",
            trigger_config={"event": "goal.metric"},
            task_template={"title": "Event task", "harness": "mock"},
        )
    )

    # No matching event yet -> not due.
    result = tick(database, policy, now=now)
    assert result["fired"] == []

    database.insert_event("goal.metric", "test", "metric.recorded")

    # Re-read the clock: last_fired is stamped from tick's `now`, so firing with
    # a `now` captured before the insert would leave the event permanently newer
    # than the fire and the routine due forever. In production `now` is always
    # the real clock at tick time, which is what this reproduces.
    now = datetime.now(UTC)
    result = tick(database, policy, now=now)
    assert len(result["fired"]) == 1
    assert result["fired"][0]["routine_id"] == created["id"]

    # No newer event since the last fire -> not due again.
    result = tick(database, policy, now=now)
    assert result["fired"] == []


def test_fire_failure_is_recorded_and_still_stamps_last_fired(database, routines, policy) -> None:
    """A task_template that fails to instantiate (bad discipline_id) still
    gets a routine_runs row (accepted=False) instead of silently vanishing,
    so a persistently broken template eventually trips the acceptance
    floor."""
    created = _cron_routine(
        routines,
        task_template={
            "title": "Broken",
            "discipline_id": "does-not-exist",
            "harness": "cli-grok",
        },
    )

    result = tick(database, policy, now=NOW)

    assert result["fired"][0]["fired"] is False
    assert "fire_failed" in result["fired"][0]["reason"]
    updated = routines.get_routine(created["id"])
    assert updated["total_runs"] == 1
    assert updated["accepted_runs"] == 0
    assert updated["last_fired"] is not None
    runs = routines.list_runs(created["id"])
    assert runs[0]["accepted"] is False
    assert runs[0]["stop_reason"] == "fire_failed"


def test_dry_run_reports_without_writing(database, routines, policy) -> None:
    created = _cron_routine(routines)

    result = tick(database, policy, now=NOW, dry_run=True)

    assert result["dry_run"] is True
    assert len(result["fired"]) == 1
    assert routines.get_routine(created["id"])["total_runs"] == 0
    assert routines.list_runs(created["id"]) == []
