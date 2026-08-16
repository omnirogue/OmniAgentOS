"""Deterministic unit coverage for intake board hygiene."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake.board_sweep import STALE_MARKER, sweep_board
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.sessions.dal import SessionsDal

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _set_updated(collab: CollabStore, task_id: str, value: datetime) -> None:
    collab._store._write(
        "UPDATE board_tasks SET updated_at = ? WHERE id = ?",
        (value.strftime("%Y-%m-%dT%H:%M:%SZ"), task_id),
    )


def _setup(tmp_path: Path) -> tuple[CollabStore, SessionsDal]:
    db = str(tmp_path / "sweep.db")
    return CollabStore(db), SessionsDal(db)


def test_sweep_archives_terminal_and_orphan_cards(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    try:
        terminal = BoardTask(title="finished", status=BoardTaskStatus.DONE)
        orphan = BoardTask(title="probe orphan")
        collab.create_board_task(terminal)
        collab.create_board_task(orphan)
        collab.update_board_task(orphan.id, {"run_id": "run_missing"})
        _set_updated(collab, terminal.id, NOW - timedelta(hours=49))
        _set_updated(collab, orphan.id, NOW - timedelta(minutes=61))

        report = sweep_board(collab._store, collab, sessions, now=NOW)

        assert report == {"archived": 2, "blocked": 0}
        assert collab.get_board_task(terminal.id)["archived"] is True  # type: ignore[index]
        assert collab.get_board_task(orphan.id)["archived"] is True  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_skips_swarm_cards(tmp_path: Path) -> None:
    """Swarm cards belong to their run's coordinator — never archived/blocked here."""
    collab, sessions = _setup(tmp_path)
    try:
        swarm_done = BoardTask(title="swarm done", status=BoardTaskStatus.DONE)
        swarm_stale = BoardTask(title="swarm waiting on deps", status=BoardTaskStatus.IN_PROGRESS)
        collab.create_board_task(swarm_done)
        collab.create_board_task(swarm_stale)
        for task_id in (swarm_done.id, swarm_stale.id):
            collab._store._write(
                "UPDATE board_tasks SET swarm_run_id = 'swr_test' WHERE id = ?", (task_id,)
            )
            _set_updated(collab, task_id, NOW - timedelta(hours=72))

        report = sweep_board(collab._store, collab, sessions, now=NOW)

        assert report == {"archived": 0, "blocked": 0}
        assert collab.get_board_task(swarm_done.id)["archived"] is False  # type: ignore[index]
        stale = collab.get_board_task(swarm_stale.id)
        assert stale["status"] == BoardTaskStatus.IN_PROGRESS.value  # type: ignore[index]
        assert STALE_MARKER not in str(stale["description"])  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_archives_blocked_faster_than_done(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    try:
        blocked = BoardTask(title="blocked", status=BoardTaskStatus.BLOCKED)
        done = BoardTask(title="done", status=BoardTaskStatus.DONE)
        collab.create_board_task(blocked)
        collab.create_board_task(done)
        updated = NOW - timedelta(hours=5)
        _set_updated(collab, blocked.id, updated)
        _set_updated(collab, done.id, updated)

        cancelled = BoardTask(title="cancelled", status=BoardTaskStatus.CANCELLED)
        collab.create_board_task(cancelled)
        _set_updated(collab, cancelled.id, updated)

        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 2,
            "blocked": 0,
        }
        assert collab.get_board_task(blocked.id)["archived"] is True  # type: ignore[index]
        assert collab.get_board_task(cancelled.id)["archived"] is True  # type: ignore[index]
        assert collab.get_board_task(done.id)["archived"] is False  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_does_not_archive_blocked_too_soon(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    try:
        card = BoardTask(title="recently blocked", status=BoardTaskStatus.BLOCKED)
        collab.create_board_task(card)
        _set_updated(collab, card.id, NOW - timedelta(hours=2))

        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 0,
            "blocked": 0,
        }
        assert collab.get_board_task(card.id)["archived"] is False  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_respects_failed_hours_env_override(tmp_path: Path, monkeypatch) -> None:
    import omniagentos.intake.board_sweep as board_sweep_module

    monkeypatch.setenv("OMNIAGENTOS_BOARD_SWEEP_FAILED_HOURS", "6")
    importlib.reload(board_sweep_module)
    try:
        collab, sessions = _setup(tmp_path)
        try:
            recent = BoardTask(title="six hour cutoff", status=BoardTaskStatus.BLOCKED)
            old = BoardTask(title="past six hour cutoff", status=BoardTaskStatus.BLOCKED)
            collab.create_board_task(recent)
            collab.create_board_task(old)
            _set_updated(collab, recent.id, NOW - timedelta(hours=5))
            _set_updated(collab, old.id, NOW - timedelta(hours=7))

            assert board_sweep_module.sweep_board(collab._store, collab, sessions, now=NOW) == {
                "archived": 1,
                "blocked": 0,
            }
            assert collab.get_board_task(recent.id)["archived"] is False  # type: ignore[index]
            assert collab.get_board_task(old.id)["archived"] is True  # type: ignore[index]
        finally:
            sessions.close()
    finally:
        monkeypatch.delenv("OMNIAGENTOS_BOARD_SWEEP_FAILED_HOURS")
        importlib.reload(board_sweep_module)


def test_sweep_blocks_stale_once_then_archives(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    try:
        card = BoardTask(title="stale", description="waiting", status=BoardTaskStatus.IN_PROGRESS)
        collab.create_board_task(card)
        _set_updated(collab, card.id, NOW - timedelta(hours=25))

        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 0,
            "blocked": 1,
        }
        blocked = collab.get_board_task(card.id)
        assert blocked is not None
        assert blocked["status"] == "blocked"
        assert blocked["description"].count(STALE_MARKER) == 1

        # A retry that sees a stale in-progress card again must not keep growing
        # its description with duplicate auto-block markers.
        collab._store._write(
            "UPDATE board_tasks SET status = ? WHERE id = ?",
            (BoardTaskStatus.IN_PROGRESS.value, card.id),
        )
        _set_updated(collab, card.id, NOW - timedelta(hours=25))
        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 0,
            "blocked": 1,
        }
        assert collab.get_board_task(card.id)["description"].count(STALE_MARKER) == 1  # type: ignore[index]

        _set_updated(collab, card.id, NOW - timedelta(hours=49))
        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 1,
            "blocked": 0,
        }
        assert collab.get_board_task(card.id)["archived"] is True  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_does_not_touch_pending_approval_card(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    try:
        store = collab._store
        store.create_task(
            {
                "id": "tsk_pending",
                "title": "pending approval",
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T00:00:00Z",
            }
        )
        store.enqueue_run(
            {
                "id": "run_pending",
                "task_id": "tsk_pending",
                "harness": "mock",
                "state": "queued",
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T00:00:00Z",
                "queued_at": "2026-07-20T00:00:00Z",
                "trace_id": "trace_pending",
            }
        )
        store._write(
            "INSERT INTO approvals (id, run_id, action_class, proposed_action, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "apr_pending",
                "run_pending",
                "consequential",
                "do work",
                "pending",
                "2026-07-20T00:00:00Z",
            ),
        )
        card = BoardTask(title="approval", status=BoardTaskStatus.DONE)
        collab.create_board_task(card)
        collab.update_board_task(card.id, {"run_id": "run_pending"})
        _set_updated(collab, card.id, NOW - timedelta(hours=49))

        assert sweep_board(store, collab, sessions, now=NOW) == {"archived": 0, "blocked": 0}
        assert collab.get_board_task(card.id)["archived"] is False  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_does_not_archive_terminal_card_with_live_run(tmp_path: Path) -> None:
    """A done card with a still-running linked run must not be archived."""
    collab, sessions = _setup(tmp_path)
    try:
        store = collab._store
        store.create_task(
            {
                "id": "tsk_live_run",
                "title": "live",
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T00:00:00Z",
            }
        )
        store.enqueue_run(
            {
                "id": "run_live_state",
                "task_id": "tsk_live_run",
                "harness": "mock",
                "state": "running",
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T00:00:00Z",
                "queued_at": "2026-07-20T00:00:00Z",
                "trace_id": "trace_live",
            }
        )
        card = BoardTask(title="done with live run", status=BoardTaskStatus.DONE)
        collab.create_board_task(card)
        collab.update_board_task(card.id, {"run_id": "run_live_state"})
        _set_updated(collab, card.id, NOW - timedelta(hours=49))

        assert sweep_board(store, collab, sessions, now=NOW) == {"archived": 0, "blocked": 0}
        assert collab.get_board_task(card.id)["archived"] is False  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_does_not_archive_terminal_card_with_live_session(tmp_path: Path) -> None:
    """A done card with a still-running linked session must not be archived."""
    collab, sessions = _setup(tmp_path)
    try:
        sessions.create_session(
            {
                "id": "ses_still_running",
                "source": "bridge",
                "project_dir": str(tmp_path),
                "state": "running",
                "model": "test",
            }
        )
        card = BoardTask(title="done with live session", status=BoardTaskStatus.DONE)
        collab.create_board_task(card)
        collab.update_board_task(card.id, {"result_ref": "ses_still_running"})
        _set_updated(collab, card.id, NOW - timedelta(hours=49))

        assert sweep_board(collab._store, collab, sessions, now=NOW) == {
            "archived": 0,
            "blocked": 0,
        }
        assert collab.get_board_task(card.id)["archived"] is False  # type: ignore[index]
    finally:
        sessions.close()


def test_sweep_handles_orphan_and_stale_orchestration_cards(tmp_path: Path) -> None:
    collab, sessions = _setup(tmp_path)
    orchestrations = OrchestrationsDal(str(tmp_path / "sweep.db"))
    try:
        orphan = BoardTask(title="orphan orchestration", result_ref="orch_missing")
        stale = BoardTask(
            title="stale orchestration",
            status=BoardTaskStatus.IN_PROGRESS,
            result_ref="orch_stale",
        )
        collab.create_board_task(orphan)
        collab.create_board_task(stale)
        orchestrations.create("orch_stale", board_task_id=stale.id, working_dir=str(tmp_path))
        orchestrations._connection.execute(  # noqa: SLF001 -- deterministic fixture.
            "UPDATE orchestrations SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            ("orch_stale",),
        )
        _set_updated(collab, orphan.id, NOW - timedelta(minutes=61))
        _set_updated(collab, stale.id, NOW - timedelta(hours=25))

        assert sweep_board(
            collab._store,
            collab,
            sessions,
            now=NOW,
            orchestrations_dal=orchestrations,
        ) == {"archived": 1, "blocked": 1}
        assert collab.get_board_task(orphan.id)["archived"] is True  # type: ignore[index]
        stale_row = collab.get_board_task(stale.id)
        assert stale_row is not None
        assert stale_row["status"] == "blocked"
        assert stale_row["description"] == STALE_MARKER
        assert any(
            event["type"] == "board.updated" and event["target_id"] == stale.id
            for event in collab._store.get_events_after(0)
        )
    finally:
        sessions.close()
        orchestrations.close()
