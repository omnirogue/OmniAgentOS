"""WP10 startup resume sweep: ``swarm.scheduler.resume_stale_swarms``.

Adopt ONLY runs whose coordinator heartbeat went stale (fresh heartbeats mean a
live coordinator and are never touched — the adopt CAS is the second guard
inside ``resume_swarm`` itself), run WP3's provider orphan reconcile, and stay
best-effort throughout: any single failure is isolated, and the helper never
raises into supervisor startup.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import omniagentos.swarm.scheduler as scheduler_module
from omniagentos.collab.store import CollabStore
from omniagentos.swarm.contracts import TERMINAL_RUN_STATUSES
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.scheduler import resume_stale_swarms
from tests.swarm.scheduler_fakes import FakeGit, make_harness, make_scheduler, wait_until


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "resume-sweep.db")
    CollabStore(db)  # migrates the shared schema (incl. 044)
    return db


class _FakeScheduler:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.resumed: list[str] = []
        self._fail_for = fail_for or set()

    def resume_swarm(self, run_id: str, *, block: bool = False) -> Any:
        if run_id in self._fail_for:
            raise RuntimeError(f"boom for {run_id}")
        self.resumed.append(run_id)
        return object()


def _seed_run(dal: SwarmDal, db: str, *, status: str, heartbeat_age_minutes: float | None) -> str:
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal=f"run {status}", source="test")["id"])
    if status != "queued":
        assert dal.set_run_status(run_id, status)
    if heartbeat_age_minutes is None:
        stamp = None
    else:
        stamp = (datetime.now(UTC) - timedelta(minutes=heartbeat_age_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    connection = sqlite3.connect(db)
    try:
        connection.execute("UPDATE swarm_runs SET heartbeat_at = ? WHERE id = ?", (stamp, run_id))
        connection.commit()
    finally:
        connection.close()
    return run_id


def _age_heartbeat(db: str, run_id: str, *, minutes: float) -> str:
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE swarm_runs SET heartbeat_at = ? WHERE id = ?",
            (stamp, run_id),
        )
        connection.commit()
    finally:
        connection.close()
    return stamp


def test_startup_sweep_terminalizes_stale_planning_run_with_error(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, [], integration=False)
    try:
        _age_heartbeat(harness.db_path, harness.run_id, minutes=10.0)
        scheduler = make_scheduler(
            harness,
            git=FakeGit(checkout=False),
            adopt_stale_minutes=2.0,
        )

        resume_stale_swarms(
            scheduler=scheduler,
            dal=harness.dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=harness.db_path,
        )

        terminal = {str(status) for status in TERMINAL_RUN_STATUSES}
        assert wait_until(
            lambda: str((harness.dal.get_run(harness.run_id) or {}).get("status")) in terminal,
            timeout=2.0,
        ), harness.dal.get_run(harness.run_id)
        row = harness.dal.get_run(harness.run_id)
        assert row is not None
        assert str(row["status"]) in terminal
        assert row["error"] is not None
        assert str(row["error"]) != ""
    finally:
        harness.close()


def test_startup_sweep_leaves_fresh_planning_run_alone(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, [], integration=False)
    try:
        heartbeat = _age_heartbeat(harness.db_path, harness.run_id, minutes=0.1)
        scheduler = make_scheduler(
            harness,
            git=FakeGit(checkout=False),
            adopt_stale_minutes=2.0,
        )

        resume_stale_swarms(
            scheduler=scheduler,
            dal=harness.dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=harness.db_path,
        )

        row = harness.dal.get_run(harness.run_id)
        assert row is not None
        assert row["status"] == "planning"
        assert row["heartbeat_at"] == heartbeat
        assert row["error"] is None
    finally:
        harness.close()


def test_adopts_only_stale_running_and_merging(db_path: str) -> None:
    dal = SwarmDal(db_path)
    try:
        stale_running = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=10)
        stale_merging = _seed_run(dal, db_path, status="merging", heartbeat_age_minutes=5)
        never_beat = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=None)
        fresh = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=0.1)
        _seed_run(dal, db_path, status="queued", heartbeat_age_minutes=60)
        _seed_run(dal, db_path, status="completed", heartbeat_age_minutes=60)

        fake = _FakeScheduler()
        reconcile_calls: list[int] = []

        def fake_reconcile() -> dict[str, str]:
            reconcile_calls.append(1)
            return {"ses_dead": "crashed"}

        summary = resume_stale_swarms(
            scheduler=fake,
            dal=dal,
            reconcile_orphans=fake_reconcile,
            stale_minutes=2.0,
            db_path=db_path,
        )

        # Stale running/merging (incl. NULL heartbeat) adopted; fresh untouched;
        # queued (admission-parked, no coordinator) and terminal runs skipped.
        assert set(fake.resumed) == {stale_running, stale_merging, never_beat}
        assert summary["resumed"] == fake.resumed
        assert summary["skipped_fresh"] == [fresh]
        # Orphan reconcile ran exactly once and its result is surfaced.
        assert reconcile_calls == [1]
        assert summary["reconciled"] == {"ses_dead": "crashed"}
        # Fresh run's heartbeat row untouched by the sweep (no adopt write).
        row = dal.get_run(fresh)
        assert row is not None and row["status"] == "running"
    finally:
        dal.close()


def test_reconcile_failure_is_isolated(db_path: str) -> None:
    dal = SwarmDal(db_path)
    try:
        stale = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        fake = _FakeScheduler()

        def broken_reconcile() -> dict[str, str]:
            raise RuntimeError("provider exec unavailable")

        summary = resume_stale_swarms(
            scheduler=fake,
            dal=dal,
            reconcile_orphans=broken_reconcile,
            stale_minutes=2.0,
            db_path=db_path,
        )

        assert fake.resumed == [stale]  # takeover still ran
        assert "reconcile_orphans" in summary["errors"]
    finally:
        dal.close()


def test_one_bad_resume_does_not_stop_the_sweep(db_path: str) -> None:
    dal = SwarmDal(db_path)
    try:
        first = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        second = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        fake = _FakeScheduler(fail_for={first})

        summary = resume_stale_swarms(
            scheduler=fake,
            dal=dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=db_path,
        )

        assert fake.resumed == [second]
        assert first in summary["errors"]
    finally:
        dal.close()


def test_flag_off_skips_takeover_but_still_reconciles(
    db_path: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No injected scheduler + flag off → orphan reconcile only, no coordinator."""
    monkeypatch.delenv("OMNIAGENTOS_SWARM_EXECUTE", raising=False)

    def _no_scheduler() -> Any:
        raise AssertionError("scheduler must not be constructed with the flag off")

    monkeypatch.setattr(scheduler_module, "_default_scheduler", _no_scheduler)
    dal = SwarmDal(db_path)
    try:
        _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        reconcile_calls: list[int] = []

        with caplog.at_level("WARNING", logger=scheduler_module.__name__):
            summary = resume_stale_swarms(
                dal=dal,
                reconcile_orphans=lambda: reconcile_calls.append(1) or {},
                db_path=db_path,
            )

        assert reconcile_calls == [1]
        assert summary.get("skipped_flag_off") is True
        assert summary["recovery_disabled"] is True
        assert summary["errors"] == []
        assert summary["resumed"] == []
        assert any(
            record.levelname == "WARNING"
            and "OMNIAGENTOS_SWARM_EXECUTE" in record.getMessage()
            and "recovery is DISABLED" in record.getMessage()
            for record in caplog.records
        )
    finally:
        dal.close()


def test_flag_on_resume_has_no_recovery_disabled_signal(
    db_path: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler_module, "_default_scheduler", lambda _db: fake)
    dal = SwarmDal(db_path)
    try:
        stale = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)

        with caplog.at_level("WARNING", logger=scheduler_module.__name__):
            summary = resume_stale_swarms(
                dal=dal,
                reconcile_orphans=lambda: {},
                db_path=db_path,
            )

        assert stale in fake.resumed
        assert "recovery_disabled" not in summary
        assert not any("recovery is DISABLED" in record.getMessage() for record in caplog.records)
    finally:
        dal.close()


def test_sweep_never_raises(db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a listing fault stays inside the helper (best-effort contract)."""
    dal = SwarmDal(db_path)
    try:
        monkeypatch.setattr(
            SwarmDal,
            "list_runs",
            lambda self, status=None: (_ for _ in ()).throw(RuntimeError("db")),
        )
        summary = resume_stale_swarms(
            scheduler=_FakeScheduler(),
            dal=dal,
            reconcile_orphans=lambda: {},
            db_path=db_path,
        )
        assert "sweep" in summary["errors"]
    finally:
        dal.close()


def test_periodic_adoption_on_subsequent_sweep(db_path: str) -> None:
    """Supervisor restarts < stale_minutes after coordinator death still adopts the run eventually."""
    dal = SwarmDal(db_path)
    try:
        # 1. Seed a run with a fresh heartbeat (1.0 minute old, which is < 2.0 stale limit)
        run_id = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=1.0)

        fake = _FakeScheduler()

        # 2. First pass: it is fresh, so it should NOT be adopted
        summary1 = resume_stale_swarms(
            scheduler=fake,
            dal=dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=db_path,
        )
        assert run_id not in fake.resumed
        assert run_id in summary1["skipped_fresh"]

        # 3. Simulate passage of time: age the heartbeat to 3.0 minutes old (> 2.0 stale limit)
        connection = sqlite3.connect(db_path)
        try:
            stamp = (datetime.now(UTC) - timedelta(minutes=3.0)).strftime("%Y-%m-%dT%H:%M:%SZ")
            connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = ? WHERE id = ?", (stamp, run_id)
            )
            connection.commit()
        finally:
            connection.close()

        # 4. Second pass (periodic check): it is now stale, so it MUST be adopted
        summary2 = resume_stale_swarms(
            scheduler=fake,
            dal=dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=db_path,
        )
        assert run_id in fake.resumed
        assert run_id in summary2["resumed"]
    finally:
        dal.close()


def test_campaign_db_adopts_stale_but_not_fresh(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production scheduler and its adopt CAS use the explicitly listed DB."""
    default_db = str(tmp_path / "empty-default.db")
    CollabStore(default_db)
    monkeypatch.setenv("OMNIAGENTOS_DB", default_db)
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
    monkeypatch.setattr(scheduler_module, "_DEFAULT_SCHEDULERS", {})

    launched: list[str] = []

    def fake_launch(_self: Any, run_id: str, *, resumed: bool, block: bool) -> object:
        assert resumed is True and block is False
        launched.append(run_id)
        return object()

    monkeypatch.setattr(scheduler_module.SwarmScheduler, "_launch", fake_launch)
    dal = SwarmDal(db_path)
    try:
        stale = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        fresh = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=0.1)
        fresh_generation = int((dal.get_run(fresh) or {})["lease_generation"])

        summary = resume_stale_swarms(
            db_path=db_path, reconcile_orphans=lambda: {}, stale_minutes=2.0
        )

        assert summary["resumed"] == [stale]
        assert summary["skipped_fresh"] == [fresh]
        assert launched == [stale]
        assert int((dal.get_run(stale) or {})["lease_generation"]) == 1
        assert int((dal.get_run(fresh) or {})["lease_generation"]) == fresh_generation
        default_dal = SwarmDal(default_db)
        try:
            assert default_dal.get_run(stale) is None
        finally:
            default_dal.close()
    finally:
        dal.close()


def test_db_mismatch_is_error_not_skipped_fresh(db_path: str, tmp_path: Path) -> None:
    dal = SwarmDal(db_path)
    try:
        run_id = _seed_run(dal, db_path, status="running", heartbeat_age_minutes=30)
        scheduler = _FakeScheduler()
        scheduler._db_path = str(tmp_path / "wrong.db")  # type: ignore[attr-defined]

        summary = resume_stale_swarms(
            scheduler=scheduler,
            dal=dal,
            reconcile_orphans=lambda: {},
            db_path=db_path,
        )

        assert "db_mismatch" in summary["errors"]
        assert summary["skipped_fresh"] == []
        assert run_id not in scheduler.resumed
    finally:
        dal.close()


def test_empty_database_is_not_a_successful_noop(db_path: str) -> None:
    dal = SwarmDal(db_path)
    try:
        summary = resume_stale_swarms(
            scheduler=_FakeScheduler(),
            dal=dal,
            reconcile_orphans=lambda: {},
            db_path=db_path,
        )
        assert summary["total_runs"] == 0
        assert summary["candidates"] == 0
        assert "empty_database" in summary["errors"]
    finally:
        dal.close()
