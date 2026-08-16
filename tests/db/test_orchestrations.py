from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import omniagentos.api.main  # noqa: F401  -- break the package's documented import cycle.
from omniagentos.db.store import SqliteStore
from omniagentos.intake import orchestrations as orchestration_module
from omniagentos.intake.orchestrations import OrchestrationsDal
from tests.support.db_template import make_store


def test_orchestration_dal_lifecycle_and_stale_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "orchestrations.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_live", board_task_id="btk_live", working_dir=str(tmp_path))
        dal.set_status("orch_live", "planning", stage="make plan")
        dal.set_status("orch_live", "running", stage="execute")
        row = dal.get("orch_live")
        assert row is not None
        assert row["status"] == "running"
        assert row["started_at"] is not None
        assert row["stage"] == "execute"

        monkeypatch.setattr(orchestration_module, "utc_now_iso", lambda: "2020-01-01T00:00:00Z")
        dal.create("orch_stale", board_task_id="btk_stale", working_dir=str(tmp_path))
        stale = dal.mark_stale_failed(stale_minutes=10)
        assert [row["id"] for row in stale] == ["orch_stale"]
        assert dal.get("orch_stale")["error"] == (  # type: ignore[index]
            "stale heartbeat — orchestrator process died"
        )

        rows = dal.get_by_ids(["orch_live", "orch_stale", "orch_live"])
        assert set(rows) == {"orch_live", "orch_stale"}
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_orchestration_reads_tolerate_pre_migration_database(tmp_path: Path) -> None:
    dal = OrchestrationsDal(tmp_path / "fresh.db")
    try:
        assert dal.get("orch_missing") is None
        assert dal.get_by_ids(["orch_missing"]) == {}
        assert dal.mark_stale_failed(stale_minutes=10) == []
    finally:
        dal.close()


def test_orchestration_writes_tolerate_pre_migration_database(tmp_path: Path) -> None:
    dal = OrchestrationsDal(tmp_path / "fresh-writes.db")
    try:
        dal.create("orch_missing", board_task_id="btk_missing", working_dir="/tmp")
        dal.set_status("orch_missing", "running", stage="running")
        dal.heartbeat("orch_missing")
        assert dal.get("orch_missing") is None
    finally:
        dal.close()


def test_orchestration_create_reuses_existing_lifecycle_row(tmp_path: Path) -> None:
    db = str(tmp_path / "idempotent.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_reuse", board_task_id="btk_reuse", working_dir="")
        dal.set_status("orch_reuse", "queued", stage="planning")
        dal.create("orch_reuse", board_task_id="btk_reuse", working_dir="/resolved")
        row = dal.get("orch_reuse")
        assert row is not None
        assert row["status"] == "queued"
        assert row["stage"] == "planning"
        assert row["working_dir"] == "/resolved"
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_record_plan_rolls_back_plan_when_step_seed_fails(tmp_path: Path) -> None:
    db = str(tmp_path / "atomic-plan.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_atomic", board_task_id="btk_atomic", working_dir="")
        dal._connection.execute(  # noqa: SLF001 -- deterministic mid-seed failure.
            "CREATE TRIGGER fail_second_orchestration_step "
            "BEFORE INSERT ON orchestration_steps WHEN NEW.seq = 1 "
            "BEGIN SELECT RAISE(ABORT, 'seed failed'); END"
        )

        dal.record_plan("orch_atomic", '{"saved":true}', ["first", "second"])

        assert dal.get("orch_atomic")["plan_json"] is None  # type: ignore[index]
        count = dal._connection.execute(  # noqa: SLF001 -- checkpoint assertion.
            "SELECT COUNT(*) FROM orchestration_steps WHERE run_id = ?",
            ("orch_atomic",),
        ).fetchone()
        assert count[0] == 0
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_step_started_self_heals_missing_checkpoint_row(tmp_path: Path) -> None:
    db = str(tmp_path / "step-upsert.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_step", board_task_id="btk_step", working_dir="")

        dal.step_started("orch_step", 3, 2)

        row = dal._connection.execute(  # noqa: SLF001 -- checkpoint assertion.
            "SELECT status, attempts, title FROM orchestration_steps WHERE run_id = ? AND seq = ?",
            ("orch_step", 3),
        ).fetchone()
        assert dict(row) == {"status": "running", "attempts": 2, "title": ""}
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_zero_stale_minutes_is_clamped_to_two(tmp_path: Path) -> None:
    db = str(tmp_path / "stale-floor.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db, pid_alive=lambda _pid: True)
    now = datetime.now(UTC)
    heartbeat = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        dal.create("orch_recent", board_task_id="btk_recent", working_dir="")
        dal.record_plan("orch_recent", '{"saved":true}', ["pending"])
        dal._connection.execute(  # noqa: SLF001 -- precise conductor fixture.
            "UPDATE orchestrations SET status = 'running', conductor_pid = 123, "
            "conductor_claimed_at = 'claim-recent', heartbeat_at = ?, updated_at = ? "
            "WHERE id = ?",
            (heartbeat, heartbeat, "orch_recent"),
        )

        assert (
            dal.find_resumable(
                stale_minutes=0,
                include_failed_retry=False,
                now=now,
            )
            == []
        )
        assert (
            dal.claim_conductor(
                "orch_recent",
                pid=456,
                stale_minutes=0,
                allow_failed_retry=False,
            )
            is None
        )
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_failed_auto_retry_requires_running_or_pending_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "failed-retry-eligibility.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        for run_id in ("orch_exhausted", "orch_midflight"):
            dal.create(run_id, board_task_id=f"btk_{run_id}", working_dir="")
            dal.record_plan(run_id, '{"saved":true}', ["one", "two"])
        dal.step_finished("orch_exhausted", 0, "failed", 3, "failed")
        dal.step_finished("orch_exhausted", 1, "failed", 3, "failed")
        dal.set_status("orch_exhausted", "failed", error="attempts exhausted")
        dal.set_status("orch_midflight", "failed", error="process died")
        monkeypatch.setenv("OMNIAGENTOS_ORCH_RETRY_BACKOFF_MINUTES", "0")

        resumable = dal.find_resumable(
            stale_minutes=10,
            include_failed_retry=True,
            now=datetime.now(UTC),
        )

        assert [row["id"] for row in resumable] == ["orch_midflight"]
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


# --- Redteam addendum (c): 'blocked_on_review' is a real checkpoint status ----
#
# ``orchestrator.core`` returns a ``blocked_on_review`` TaskOutcome (H2: the
# reviewer's INFRASTRUCTURE failed, so nothing was judged) and hands it to
# ``step_finished``. The orchestration_steps CHECK constraint did not list that
# value, and ``step_finished`` swallows every ``sqlite3.Error`` — so the resume
# state for exactly the steps that need an operator was dropped in silence.


def test_step_finished_persists_blocked_on_review(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = str(tmp_path / "blocked-on-review.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_blocked", board_task_id="btk_blocked", working_dir="")
        dal.record_plan("orch_blocked", '{"saved":true}', ["reviewed step"])
        dal.step_started("orch_blocked", 0, 2)
        dal.step_session("orch_blocked", 0, "ses_blocked")

        with caplog.at_level(logging.DEBUG, logger="omniagentos.intake.orchestrations"):
            dal.step_finished("orch_blocked", 0, "blocked_on_review", 2, "reviewer adapter down")

        # No swallowed sqlite3.Error: the swallow logs at DEBUG on that logger.
        assert "checkpoint unavailable" not in caplog.text
        row = dal._connection.execute(  # noqa: SLF001 -- checkpoint assertion.
            "SELECT status, attempts, output_tail FROM orchestration_steps "
            "WHERE run_id = ? AND seq = ?",
            ("orch_blocked", 0),
        ).fetchone()
        assert dict(row) == {
            "status": "blocked_on_review",
            "attempts": 2,
            "output_tail": "reviewer adapter down",
        }
        # …and the RESUME read — the whole point of the checkpoint — sees it.
        resume = dal.load_resume_state("orch_blocked")
        assert resume is not None
        assert [(step.status, step.session_id) for step in resume.steps] == [
            ("blocked_on_review", "ses_blocked")
        ]
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.


def test_step_status_check_still_refuses_an_unknown_status(tmp_path: Path) -> None:
    """The mutation catcher: widening the vocabulary must not DROP the CHECK.

    A rebuild that simply removed the constraint would make the test above pass
    while letting any typo become durable resume state.
    """
    db = str(tmp_path / "step-status-vocabulary.db")
    store = make_store(SqliteStore, db)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_vocab", board_task_id="btk_vocab", working_dir="")
        dal.record_plan("orch_vocab", '{"saved":true}', ["one"])
        with pytest.raises(sqlite3.IntegrityError):
            dal._connection.execute(  # noqa: SLF001 -- constraint assertion.
                "UPDATE orchestration_steps SET status = 'blocked_on_reviewww' "
                "WHERE run_id = ? AND seq = ?",
                ("orch_vocab", 0),
            )
        # Every status the checkpoint has always written still round-trips.
        for status in ("pending", "running", "done", "unreviewed", "denied", "failed"):
            dal.step_finished("orch_vocab", 0, status, 1, status)
            assert (
                dal._connection.execute(  # noqa: SLF001 -- constraint assertion.
                    "SELECT status FROM orchestration_steps WHERE run_id = ? AND seq = ?",
                    ("orch_vocab", 0),
                ).fetchone()["status"]
                == status
            )
    finally:
        dal.close()
        store._connection.close()  # noqa: SLF001 -- SqliteStore has no public close seam.
