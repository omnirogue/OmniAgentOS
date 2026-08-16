"""LOOPS-1: scope/purpose validation, draft exemption, next_run helper."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines import (
    RoutineValidationError,
    _cron_field_matches,
    compute_next_run,
    validate_routine,
)
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import (
    apply_routines_meta_migration,
    draft_routine_payload,
    valid_routine_payload,
)
from tests.support.db_template import make_store


@pytest.fixture
def routines(tmp_path: Path) -> RoutinesStore:
    store = make_store(SqliteStore, tmp_path / "routines_meta.db")
    apply_routines_meta_migration(store)
    return RoutinesStore(store)


def test_scope_accepts_known_values() -> None:
    for scope in ("personal", "company", "project", "system"):
        validate_routine(valid_routine_payload(scope=scope))


def test_scope_rejects_bogus() -> None:
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(valid_routine_payload(scope="bogus"))
    assert any("scope" in e for e in excinfo.value.errors)


def test_scope_and_purpose_optional_null() -> None:
    validate_routine(valid_routine_payload())
    validate_routine(valid_routine_payload(scope=None, purpose=None))


def test_purpose_is_free_text() -> None:
    validate_routine(valid_routine_payload(purpose="goal_review"))
    validate_routine(valid_routine_payload(purpose="anything-goes-no-enum"))


def test_draft_exemption_allows_omitting_engine_fields() -> None:
    """LOOPS1-E2: status='disabled' may omit template/trigger/gate/cap/notify."""
    validate_routine(draft_routine_payload(name="chat-draft"))


def test_draft_exemption_still_requires_name() -> None:
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine({"status": "disabled"})
    assert any("name" in e for e in excinfo.value.errors)


def test_active_still_requires_all_engine_fields() -> None:
    """Paired guard: active rows hard-require ALL fields (cf-draft-exemption-on-active)."""
    payload = {
        "name": "almost",
        "status": "active",
    }
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    errors = " ".join(excinfo.value.errors)
    assert "task_template" in errors
    assert "trigger_type" in errors or "trigger" in errors
    assert "gate_type" in errors or "gate" in errors
    assert "hard_cap" in errors


def test_draft_exemption_is_status_keyed_not_missing_fields() -> None:
    """cf-draft-exemption-on-active: exemption keyed only on status=='disabled'."""
    # Same sparse payload as a draft, but status active → must fail.
    sparse = {"name": "sparse", "status": "active"}
    with pytest.raises(RoutineValidationError):
        validate_routine(sparse)
    # And status omitted (treated as non-disabled) → must fail.
    with pytest.raises(RoutineValidationError):
        validate_routine({"name": "sparse-no-status"})


def test_auto_paused_is_not_exempt() -> None:
    with pytest.raises(RoutineValidationError):
        validate_routine({"name": "x", "status": "auto_paused"})


def test_store_round_trips_scope_and_purpose(routines: RoutinesStore) -> None:
    created = routines.create_routine(
        valid_routine_payload(name="meta-rt", scope="system", purpose="maintenance")
    )
    assert created["scope"] == "system"
    assert created["purpose"] == "maintenance"
    fetched = routines.get_routine(created["id"])
    assert fetched is not None
    assert fetched["scope"] == "system"
    assert fetched["purpose"] == "maintenance"
    updated = routines.update_routine(created["id"], {"purpose": "reflection", "scope": "company"})
    assert updated is not None
    assert updated["scope"] == "company"
    assert updated["purpose"] == "reflection"


def test_store_null_meta_round_trip(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload(name="untagged"))
    assert created.get("scope") is None
    assert created.get("purpose") is None


def test_store_draft_create_and_activate_via_update_revalidates(
    routines: RoutinesStore,
) -> None:
    draft = routines.create_routine(draft_routine_payload(name="draft-activate"))
    assert draft["status"] == "disabled"
    # Activate-via-update must validate merged status=active → full fields required.
    with pytest.raises(RoutineValidationError):
        routines.update_routine(draft["id"], {"status": "active"})
    still = routines.get_routine(draft["id"])
    assert still is not None
    assert still["status"] == "disabled"


def test_next_run_equals_cron_matcher_next_fire() -> None:
    """LOOPS1-E3: next_run is computed BY the :276-311 matcher, not re-derived."""
    # Fixed clock: 2026-07-30 10:07 → next */5 is 10:10.
    now = datetime(2026, 7, 30, 10, 7, 0, tzinfo=UTC)
    routine = {
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/5 * * * *"},
        "last_fired": None,
    }
    next_run = compute_next_run(routine, now=now)
    assert next_run == "2026-07-30T10:10:00Z"

    # Independent check against the same field matcher the helper uses.
    minute_f = "*/5"
    assert _cron_field_matches(minute_f, 10, 0, 59)
    assert not _cron_field_matches(minute_f, 7, 0, 59)


def test_next_run_null_for_event_trigger() -> None:
    routine = {
        "trigger_type": "event",
        "trigger_config": {"event": "run.completed"},
    }
    assert compute_next_run(routine) is None


def test_next_run_not_derived_from_last_fired_interval() -> None:
    """cf-next-run-from-last-fired: helper must not be last_fired + fixed interval."""
    now = datetime(2026, 7, 30, 10, 0, 0, tzinfo=UTC)
    # Cron says top of every hour; last_fired 30 min ago. next is 11:00, not 10:30.
    routine = {
        "trigger_type": "cron",
        "trigger_config": {"cron": "0 * * * *"},
        "last_fired": "2026-07-30T09:30:00Z",
    }
    next_run = compute_next_run(routine, now=now)
    assert next_run == "2026-07-30T10:00:00Z"
    assert next_run != "2026-07-30T10:30:00Z"


def test_next_run_annual_cron_returns_next_jan_1() -> None:
    """next_run must search past the 8-day catch-up bound for annual crons."""
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    routine = {
        "trigger_type": "cron",
        "trigger_config": {"cron": "0 0 1 1 *"},
        "last_fired": None,
    }
    next_run = compute_next_run(routine, now=now)
    assert next_run is not None, "annual cron must not return null within 400-day bound"
    assert next_run == "2027-01-01T00:00:00Z"


def test_next_run_always_utc_z_suffix() -> None:
    """Naive local clocks must not be stamped with a false Z."""
    now = datetime(2026, 7, 30, 10, 7, 0)  # naive — treated as UTC, not local
    routine = {
        "trigger_type": "cron",
        "trigger_config": {"cron": "0 15 * * *"},
        "last_fired": None,
    }
    next_run = compute_next_run(routine, now=now)
    assert next_run == "2026-07-30T15:00:00Z"
    assert next_run.endswith("Z")
