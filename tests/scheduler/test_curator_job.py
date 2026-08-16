"""``selfimprove-curator`` was DECLARED in scheduler/system_jobs.py's launchd
catalog (so a completed mission could in principle become a skill) but was
never registered as a runnable builtin — nothing in ``BUILTIN_JOBS`` ever
invoked ``curate_sessions``. This file proves the registration and its guard.

DECISIVE
    ``test_curator_registered_under_its_system_jobs_module_label`` — the
    catalog entry's ``module`` string is the exact key ``BUILTIN_JOBS`` uses.
    ``test_curator_tick_invokes_curate_sessions_when_enabled`` — with the flag
    on, the callable actually calls ``curate_sessions`` (stubbed).
    ``test_disabled_curator_tick_settles_neutral_not_adverse`` (2026-08-14
    xcrit F1) — drives the DISABLED tick through REAL settlement
    (``routines_tick.tick`` -> ``routines_settle``), not just the in-memory
    ``BuiltinResult``. Before the ``routines.py`` taxonomy fix,
    ``"curator_disabled"`` was not in ``NEUTRAL_STOP_REASONS``, so
    ``self_reported_outcome()`` classified it ADVERSE at settlement — three
    disabled ticks would trip auto-pause permanently, and flipping the flag
    later could never repair the frozen settled rows.

COUNTERFEIT
    ``test_curator_tick_is_a_noop_when_disabled`` — default OFF must never
    call ``curate_sessions`` at all, so nothing changes in production until an
    operator deliberately flips the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler import builtin_jobs
from omniagentos.scheduler.routines import OUTCOME_NEUTRAL, improve_dispatcher_routine
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from omniagentos.scheduler.system_jobs import CATALOG
from omniagentos.selfimprove.curator import CurateResult
from tests.support.db_template import make_store

# Matches IMPROVE_DISPATCHER_CRON ("*/5 * * * *"), reused so the settlement
# test can build on the same already-validated routine payload shape as
# tests/scheduler/test_builtin_jobs.py's declared-but-unregistered control.
DUE_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _curator_catalog_module() -> str:
    entry = next(job for job in CATALOG if job.key == "selfimprove-curator")
    assert entry.module is not None
    return entry.module


@pytest.fixture(autouse=True)
def _no_inherited_gate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the settlement test's premise: no gate executes here (same rationale
    as tests/scheduler/test_builtin_jobs.py's fixture of the same name) — an
    inherited OMNIAGENTOS_GATE_WORKSPACE would really execute the dispatcher
    template's declared gate command and turn a deterministic neutral-claim
    assertion into an environment-dependent one."""
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "curator_settlement.db")


def test_curator_registered_under_its_system_jobs_module_label() -> None:
    """The declared launchd catalog module is the BUILTIN_JOBS registration key."""
    module = _curator_catalog_module()
    assert module in builtin_jobs.BUILTIN_JOBS
    assert builtin_jobs.BUILTIN_JOBS[module] is builtin_jobs.run_selfimprove_curator_tick


def test_curator_tick_invokes_curate_sessions_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(builtin_jobs.CURATOR_ENABLED_ENV, "1")
    calls: list[dict[str, Any]] = []

    def stub_curate_sessions(**kwargs: Any) -> CurateResult:
        calls.append(kwargs)
        result = CurateResult()
        result.scanned = 3
        result.captured = ["session_1"]
        return result

    monkeypatch.setattr(
        "omniagentos.selfimprove.curator.curate_sessions", stub_curate_sessions
    )

    result = builtin_jobs.run_selfimprove_curator_tick(store=object())

    assert len(calls) == 1
    assert result.accepted is True
    assert result.self_report == "completed"
    assert result.outcome_class == "favourable"
    assert "scanned=3" in result.notes
    assert "captured=1" in result.notes


def test_curator_tick_is_a_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default OFF: curate_sessions must never be invoked."""
    monkeypatch.delenv(builtin_jobs.CURATOR_ENABLED_ENV, raising=False)
    calls: list[dict[str, Any]] = []

    def stub_curate_sessions(**kwargs: Any) -> CurateResult:
        calls.append(kwargs)
        return CurateResult()

    monkeypatch.setattr(
        "omniagentos.selfimprove.curator.curate_sessions", stub_curate_sessions
    )

    result = builtin_jobs.run_selfimprove_curator_tick(store=object())

    assert calls == []
    assert result.accepted is False
    assert result.outcome_class == OUTCOME_NEUTRAL
    assert result.reason == "curator_disabled"
    assert result.self_report == "disabled"


def test_curator_tick_explicitly_off_is_also_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any value other than the literal "1" stays disabled -- no silent truthy coercion."""
    monkeypatch.setenv(builtin_jobs.CURATOR_ENABLED_ENV, "0")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.selfimprove.curator.curate_sessions",
        lambda **kwargs: calls.append(kwargs) or CurateResult(),
    )

    result = builtin_jobs.run_selfimprove_curator_tick(store=object())

    assert calls == []
    assert result.reason == "curator_disabled"


def test_curator_tick_never_raises_when_curate_sessions_faults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same posture as every other builtin: a curator fault must not stop the tick loop."""
    monkeypatch.setenv(builtin_jobs.CURATOR_ENABLED_ENV, "1")

    def boom(**kwargs: Any) -> CurateResult:
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr("omniagentos.selfimprove.curator.curate_sessions", boom)

    result = builtin_jobs.run_selfimprove_curator_tick(store=object())

    assert result.accepted is False
    assert "curator tick raised" in result.notes


# ---------------------------------------------------------------------------
# xcrit F1 DECISIVE: the disabled tick must settle NEUTRAL, not ADVERSE
# ---------------------------------------------------------------------------


def test_disabled_curator_tick_settles_neutral_not_adverse(database: SqliteStore) -> None:
    """Drive a real due routine through ``routines_tick.tick`` (production
    entry) with the curator flag left at its default OFF, and read back the
    SETTLED ``routine_runs`` row -- not the in-memory ``BuiltinResult``.

    Before the ``routines.py`` NEUTRAL_STOP_REASONS fix, "curator_disabled"
    fell through ``self_reported_outcome`` to ADVERSE, so this settled as
    gate_passed=False / accepted=False / outcome_class="adverse" even though
    the tick behaved correctly and merely declined to run.
    """
    template = improve_dispatcher_routine()
    template["name"] = "curator-settlement-control"
    template["task_template"]["input"]["module"] = "omniagentos.selfimprove.curator"
    routine = RoutinesStore(database).create_routine(template)

    result = tick(database, load_policy(), now=DUE_NOW)

    fired = [e for e in result["fired"] if e["routine_id"] == routine["id"]]
    assert len(fired) == 1, result
    assert fired[0]["fired"] is True
    assert fired[0].get("builtin") == "omniagentos.selfimprove.curator"

    runs = RoutinesStore(database).list_runs(routine["id"])
    assert len(runs) == 1
    row = runs[0]
    assert row["stop_reason"] == "curator_disabled"
    assert row["outcome_class"] == "neutral", row
    assert row["accepted"] is None, "a disabled tick is not a rejection"
    assert row["gate_passed"] is None, "no gate executed here (no workspace configured)"


def test_three_disabled_curator_ticks_do_not_trip_auto_pause(database: SqliteStore) -> None:
    """The concrete failure mode F1 describes: three neutral non-results in a
    row must leave the routine active, exactly like the dream cycle's
    NO_INPUT case (test_builtin_jobs.py::test_no_input_cycle_is_neutral_not_accepted).
    """
    template = improve_dispatcher_routine()
    template["name"] = "curator-auto-pause-control"
    template["task_template"]["input"]["module"] = "omniagentos.selfimprove.curator"
    routine = RoutinesStore(database).create_routine(template)

    routines = RoutinesStore(database)
    for minute in (0, 5, 10):
        now = datetime(2026, 7, 29, 12, minute, tzinfo=UTC)
        result = tick(database, load_policy(), now=now)
        fired = [e for e in result["fired"] if e["routine_id"] == routine["id"]]
        assert len(fired) == 1, result
        assert fired[0]["fired"] is True

    runs = routines.list_runs(routine["id"])
    assert len(runs) == 3
    for row in runs:
        assert row["accepted"] is None, "a disabled tick is not an acceptance"
        assert row["outcome_class"] == "neutral"
        assert row["stop_reason"] == "curator_disabled"

    status = routines.get_routine(routine["id"])
    assert status is not None
    assert status["status"] == "active", status.get("auto_pause_reason")
    assert status["neutral_runs"] == 3
    assert status["acceptance_rate"] is None

    # 4th tick still fires (would be skipped if auto-paused).
    fourth = tick(database, load_policy(), now=datetime(2026, 7, 29, 12, 15, tzinfo=UTC))
    fired_fourth = [e for e in fourth["fired"] if e["routine_id"] == routine["id"]]
    assert len(fired_fourth) == 1
    assert fired_fourth[0]["fired"] is True
