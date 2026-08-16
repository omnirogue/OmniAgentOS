"""Pure, DB-free tests for the DUE-check + stop-condition logic that decides
whether omniagentos.scheduler.routines_tick fires a routine."""

from __future__ import annotations

from datetime import UTC, datetime

from omniagentos.scheduler.routines import (
    cron_is_due,
    event_is_due,
    hard_cap_hit,
    should_fire,
)
from tests.routines.conftest import valid_routine_payload


def _dt(**kwargs: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC).replace(**kwargs)


# --- cron_is_due ---------------------------------------------------------


def test_cron_star_matches_every_minute_and_fires_when_never_fired() -> None:
    now = _dt(hour=9, minute=17)
    assert cron_is_due("* * * * *", now, None) is True


def test_cron_does_not_refire_within_the_same_matched_minute() -> None:
    now = _dt(hour=9, minute=17)
    last_fired = now.strftime("%Y-%m-%dT%H:%M:00Z")
    assert cron_is_due("* * * * *", now, last_fired) is False


def test_cron_refires_once_the_minute_advances() -> None:
    now = _dt(hour=9, minute=18)
    last_fired = "2026-01-01T09:17:00Z"
    assert cron_is_due("* * * * *", now, last_fired) is True


def test_cron_specific_minute_only_matches_that_minute() -> None:
    assert cron_is_due("5 * * * *", _dt(hour=9, minute=5), None) is True
    assert cron_is_due("5 * * * *", _dt(hour=9, minute=6), None) is False


def test_cron_step_field_matches_every_fifth_minute() -> None:
    assert cron_is_due("*/5 * * * *", _dt(hour=9, minute=15), None) is True
    assert cron_is_due("*/5 * * * *", _dt(hour=9, minute=16), None) is False


def test_cron_range_and_list_fields() -> None:
    assert cron_is_due("0 9-17 * * *", _dt(hour=12, minute=0), None) is True
    assert cron_is_due("0 9-17 * * *", _dt(hour=20, minute=0), None) is False
    assert cron_is_due("0 0 1,15 * *", _dt(day=15, hour=0, minute=0), None) is True


def test_cron_day_of_week_sunday_accepts_both_0_and_7() -> None:
    # 2026-01-04 is a Sunday.
    sunday = _dt(day=4, hour=6, minute=0)
    assert cron_is_due("0 6 * * 0", sunday, None) is True
    assert cron_is_due("0 6 * * 7", sunday, None) is True
    assert cron_is_due("0 6 * * 1", sunday, None) is False


def test_cron_six_field_expression_ignores_leading_seconds() -> None:
    assert cron_is_due("30 5 * * * *", _dt(hour=0, minute=5), None) is True


def test_cron_malformed_expression_never_due() -> None:
    assert cron_is_due("not a cron", _dt(), None) is False
    assert cron_is_due("* * * *", _dt(), None) is False  # only 4 fields


def test_daily_missed_fire_catches_up_after_more_than_24_hours() -> None:
    now = datetime(2026, 1, 2, 10, 5, tzinfo=UTC)
    assert cron_is_due("0 10 * * *", now, "2026-01-01T09:05:00Z") is True


def test_weekly_missed_fire_catches_up_on_interval_tick() -> None:
    now = datetime(2026, 1, 11, 9, 5, tzinfo=UTC)  # Sunday
    assert cron_is_due("0 9 * * 0", now, "2026-01-04T09:05:00Z") is True


def test_never_fired_daily_schedule_catches_first_offset_tick() -> None:
    now = datetime(2026, 1, 2, 10, 5, tzinfo=UTC)
    assert (
        cron_is_due(
            "0 10 * * *",
            now,
            None,
            schedule_started="2026-01-02T09:55:00Z",
        )
        is True
    )


def test_never_fired_weekly_schedule_catches_first_offset_tick() -> None:
    now = datetime(2026, 1, 11, 9, 5, tzinfo=UTC)
    assert (
        cron_is_due(
            "0 9 * * 0",
            now,
            None,
            schedule_started="2026-01-11T08:55:00Z",
        )
        is True
    )


def test_never_fired_schedule_does_not_backfill_before_creation() -> None:
    now = datetime(2026, 1, 11, 9, 5, tzinfo=UTC)
    assert (
        cron_is_due(
            "0 9 * * 0",
            now,
            None,
            schedule_started="2026-01-11T09:01:00Z",
        )
        is False
    )


def test_caught_up_schedule_does_not_refire_without_a_new_scheduled_minute() -> None:
    now = datetime(2026, 1, 11, 9, 10, tzinfo=UTC)
    assert cron_is_due("0 9 * * 0", now, "2026-01-11T09:05:00Z") is False


# --- event_is_due ----------------------------------------------------------


def test_event_due_when_never_fired_and_an_event_exists() -> None:
    assert event_is_due("2026-01-01T00:00:00Z", None) is True


def test_event_not_due_when_no_event_seen() -> None:
    assert event_is_due(None, None) is False
    assert event_is_due(None, "2026-01-01T00:00:00Z") is False


def test_event_due_only_when_newer_than_last_fired() -> None:
    assert event_is_due("2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z") is True
    assert event_is_due("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z") is False
    assert event_is_due("2025-12-31T23:00:00Z", "2026-01-01T00:00:00Z") is False


# --- hard_cap_hit ------------------------------------------------------------


def test_max_iterations_hard_cap() -> None:
    assert hard_cap_hit("max_iterations", 3, total_runs=2, total_cost_usd=0) is False
    assert hard_cap_hit("max_iterations", 3, total_runs=3, total_cost_usd=0) is True
    assert hard_cap_hit("max_iterations", 3, total_runs=4, total_cost_usd=0) is True


def test_human_checkpoint_hard_cap_behaves_like_an_iteration_count() -> None:
    assert hard_cap_hit("human_checkpoint", 1, total_runs=0, total_cost_usd=0) is False
    assert hard_cap_hit("human_checkpoint", 1, total_runs=1, total_cost_usd=0) is True


def test_budget_usd_hard_cap() -> None:
    assert hard_cap_hit("budget_usd", 10.0, total_runs=100, total_cost_usd=9.99) is False
    assert hard_cap_hit("budget_usd", 10.0, total_runs=1, total_cost_usd=10.0) is True


# --- ISSUE-8 safety fix: budget_usd fails CLOSED on an unknown (NULL) rollup ---


def test_budget_usd_hard_cap_fails_closed_on_unknown_total_cost() -> None:
    """A NULL total_cost_usd (migration 119 — at least one contributing run's
    cost was never reported) must count as CAP EXCEEDED for a budget_usd cap,
    never as an exact $0 that could never trip anything."""
    assert hard_cap_hit("budget_usd", 10.0, total_runs=1, total_cost_usd=None) is True


def test_iteration_and_checkpoint_hard_caps_are_unaffected_by_unknown_cost() -> None:
    """Unknown cost is only meaningful to a budget_usd cap; max_iterations and
    human_checkpoint never read total_cost_usd at all."""
    assert hard_cap_hit("max_iterations", 3, total_runs=2, total_cost_usd=None) is False
    assert hard_cap_hit("max_iterations", 3, total_runs=3, total_cost_usd=None) is True
    assert hard_cap_hit("human_checkpoint", 1, total_runs=0, total_cost_usd=None) is False


# --- should_fire (the combined decision) ------------------------------------


_ROLLUP_KEYS = ("status", "total_runs", "accepted_runs", "total_cost_usd", "last_fired")


def _routine(**overrides: object) -> dict[str, object]:
    """A valid routine payload (see conftest.valid_routine_payload) extended
    with the rollup/lifecycle fields should_fire() reads off a real
    RoutinesStore row (status, total_runs, accepted_runs, total_cost_usd,
    last_fired) — defaulted to "brand new, active, never fired"."""
    payload_overrides = {k: v for k, v in overrides.items() if k not in _ROLLUP_KEYS}
    payload_overrides.setdefault("trigger_config", {"cron": "* * * * *"})
    payload = valid_routine_payload(**payload_overrides)
    payload["status"] = overrides.get("status", "active")
    payload["total_runs"] = overrides.get("total_runs", 0)
    payload["accepted_runs"] = overrides.get("accepted_runs", 0)
    payload["total_cost_usd"] = overrides.get("total_cost_usd", 0.0)
    payload["last_fired"] = overrides.get("last_fired")
    return payload


def test_should_fire_true_for_a_due_active_routine() -> None:
    fire, reason = should_fire(_routine(), now=_dt(hour=9, minute=0))
    assert fire is True
    assert reason == ""


def test_should_fire_false_when_disabled() -> None:
    fire, reason = should_fire(_routine(status="disabled"), now=_dt())
    assert fire is False
    assert "not active" in reason


def test_should_fire_false_when_auto_paused() -> None:
    fire, reason = should_fire(_routine(status="auto_paused"), now=_dt())
    assert fire is False


def test_should_fire_false_when_hard_cap_reached() -> None:
    routine = _routine(hard_cap_type="max_iterations", hard_cap_value=2, total_runs=2)
    fire, reason = should_fire(routine, now=_dt())
    assert fire is False
    assert "hard stop-condition" in reason


# --- ISSUE-8 safety fix: budget_usd + an unknown (NULL) rollup fails closed --


def test_should_fire_false_when_budget_cap_total_cost_is_unknown() -> None:
    """A budget_usd-capped routine whose total_cost_usd rollup is NULL
    (migration 119 — at least one contributing run's cost was never reported)
    must not fire: treating unknown cost as "under budget" is exactly the
    lie ISSUE-8 removed from the dashboard, and firing again on unverifiable
    spend is worse than a false pause."""
    routine = _routine(hard_cap_type="budget_usd", hard_cap_value=10.0, total_cost_usd=None)
    fire, reason = should_fire(routine, now=_dt(hour=9, minute=0))
    assert fire is False
    assert "unknown" in reason.lower()


def test_should_fire_true_when_budget_cap_total_cost_is_known_and_under() -> None:
    """Regression guard: a KNOWN cost under the cap still fires normally."""
    routine = _routine(hard_cap_type="budget_usd", hard_cap_value=10.0, total_cost_usd=5.0)
    fire, reason = should_fire(routine, now=_dt(hour=9, minute=0))
    assert fire is True
    assert reason == ""


def test_should_fire_false_when_budget_cap_total_cost_is_known_and_over() -> None:
    """Regression guard: a KNOWN cost at/over the cap still trips exactly as
    before — the fix only changes the UNKNOWN case."""
    routine = _routine(hard_cap_type="budget_usd", hard_cap_value=10.0, total_cost_usd=10.0)
    fire, reason = should_fire(routine, now=_dt(hour=9, minute=0))
    assert fire is False
    assert "hard stop-condition" in reason
    assert "unknown" not in reason.lower()


def test_should_fire_unaffected_by_unknown_cost_when_hard_cap_is_not_budget() -> None:
    """An unknown total_cost_usd must not spuriously trip a max_iterations or
    human_checkpoint cap — only budget_usd reads the cost at all."""
    routine = _routine(
        hard_cap_type="max_iterations", hard_cap_value=5, total_runs=1, total_cost_usd=None
    )
    fire, reason = should_fire(routine, now=_dt(hour=9, minute=0))
    assert fire is True
    assert reason == ""


def test_should_fire_false_under_acceptance_floor_even_if_status_says_active() -> None:
    # Simulates rollup/status drift (should never happen via RoutinesStore.record_run,
    # which flips status itself — but should_fire re-checks the rollups directly too).
    routine = _routine(total_runs=4, accepted_runs=1)
    fire, reason = should_fire(routine, now=_dt())
    assert fire is False
    assert "acceptance rate" in reason


def test_should_fire_false_when_cron_not_due() -> None:
    routine = _routine(trigger_config={"cron": "0 3 * * *"})
    fire, reason = should_fire(routine, now=_dt(hour=9, minute=0))
    assert fire is False
    assert "not due" in reason


def test_should_fire_uses_creation_time_for_first_daily_catchup() -> None:
    routine = _routine(
        trigger_config={"cron": "0 10 * * *"},
        created_at="2026-01-02T09:55:00Z",
    )
    fire, reason = should_fire(
        routine,
        now=datetime(2026, 1, 2, 10, 5, tzinfo=UTC),
    )
    assert fire is True
    assert reason == ""


def test_should_fire_uses_creation_time_for_first_weekly_catchup() -> None:
    routine = _routine(
        trigger_config={"cron": "0 9 * * 0"},
        created_at="2026-01-11T08:55:00Z",
    )
    fire, reason = should_fire(
        routine,
        now=datetime(2026, 1, 11, 9, 5, tzinfo=UTC),
    )
    assert fire is True
    assert reason == ""


def test_should_fire_event_trigger_uses_latest_event_ts() -> None:
    routine = _routine(trigger_type="event", trigger_config={"event": "goal.metric"})
    fire, _ = should_fire(routine, now=_dt(), latest_event_ts=None)
    assert fire is False
    fire, _ = should_fire(routine, now=_dt(), latest_event_ts="2026-01-01T00:00:00Z")
    assert fire is True
