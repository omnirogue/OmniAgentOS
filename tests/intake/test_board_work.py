from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.main  # noqa: F401  -- break the package's documented import cycle.
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake import service
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.service import reconcile_board
from omniagentos.sessions.dal import SessionsDal

WORK_KEYS = {
    "kind",
    "state",
    "agent",
    "steps_done",
    "steps_total",
    "current_step",
    "files_count",
    "cost_usd",
    "last_activity_at",
    "error",
}


def _run_card(collab: CollabStore, suffix: str) -> str:
    store = collab._store
    task_id = f"tsk_{suffix}"
    run_id = f"run_{suffix}"
    store.create_task(
        {
            "id": task_id,
            "title": suffix,
            "created_at": "2026-07-22T00:00:00Z",
            "updated_at": "2026-07-22T00:00:00Z",
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "harness": "mock",
            "state": "running",
            "cost_usd": 1.25,
            "created_at": "2026-07-22T00:00:00Z",
            "updated_at": "2026-07-22T00:01:00Z",
            "queued_at": "2026-07-22T00:00:00Z",
            "trace_id": f"trc_{suffix}",
        }
    )
    store.upsert_step(run_id, 0, {"name": "first", "status": "completed"})
    store.upsert_step(run_id, 1, {"name": "second", "status": "running"})
    card = BoardTask(title=f"run {suffix}")
    collab.create_board_task(card)
    collab.update_board_task(card.id, {"run_id": run_id})
    return card.id


def _session_card(collab: CollabStore, sessions: SessionsDal, suffix: str) -> str:
    session_id = f"ses_{suffix}"
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": "/tmp",
            "state": "running",
            "model": "sol",
            "cost_usd": 0.5,
            "last_activity_at": "2026-07-22T00:02:00Z",
        }
    )
    sessions.set_session_todos(
        session_id,
        json.dumps(
            [
                {"content": "done", "status": "completed"},
                {"content": "working now", "status": "in_progress"},
            ]
        ),
    )
    sessions.set_session_files(session_id, json.dumps(["a.txt", "b.txt"]))
    card = BoardTask(title=f"session {suffix}", result_ref=session_id)
    collab.create_board_task(card)
    return card.id


def _orchestration_card(collab: CollabStore, orchestrations: OrchestrationsDal, suffix: str) -> str:
    orch_id = f"orch_{suffix}"
    card = BoardTask(title=f"orchestration {suffix}", result_ref=orch_id)
    collab.create_board_task(card)
    orchestrations.create(orch_id, board_task_id=card.id, working_dir="/tmp")
    orchestrations.set_status(orch_id, "running", stage="quality review")
    return card.id


class _CountingSessions:
    def __init__(self, delegate: SessionsDal) -> None:
        self.delegate = delegate
        self.calls = 0

    def get_sessions_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return self.delegate.get_sessions_by_ids(ids)


class _CountingOrchestrations:
    def __init__(self, delegate: OrchestrationsDal) -> None:
        self.delegate = delegate
        self.batch_calls = 0
        self.stale_calls = 0

    def get_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        self.batch_calls += 1
        return self.delegate.get_by_ids(ids)

    def mark_stale_failed(self, *, stale_minutes: int) -> list[dict[str, Any]]:
        self.stale_calls += 1
        return self.delegate.mark_stale_failed(stale_minutes=stale_minutes)


def _done_bells(db: str, board_task_id: str) -> list[dict[str, Any]]:
    from omniagentos.notifications.dal import NotificationsDal

    return [
        row
        for row in NotificationsDal(db).list(limit=500)
        if row["kind"] == "done" and row["ref_id"] == board_task_id
    ]


def test_reconcile_transition_to_done_emits_exactly_one_bell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C0: reconcile emits the done bell ONLY when it writes a card TO done.

    A session-linked card observed as ``completed`` transitions in_progress ->
    done here, so this reconcile fires exactly one bell (with the resolved
    workspace + files_count); a SECOND reconcile merely observes the already-done
    card and must not re-bell.
    """
    from omniagentos.api.routes import sessions as sessions_routes

    db = str(tmp_path / "reconcile-bell.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: sessions)

    work = tmp_path / "reconcile-ws"
    (work / "outputs").mkdir(parents=True)
    (work / "outputs" / "final.md").write_text("done", encoding="utf-8")
    session_id = "ses_reconcile"
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(work),
            "state": "completed",
            "model": "sol",
        }
    )
    card = BoardTask(
        title="Reconciled to done",
        result_ref=session_id,
        status=BoardTaskStatus.IN_PROGRESS,
    )
    collab.create_board_task(card)

    reconcile_board(collab._store, collab, sessions_dal=sessions, orchestrations_dal=orchestrations)
    assert (collab.get_board_task(card.id) or {})["status"] == "done"

    bells = _done_bells(db, card.id)
    assert len(bells) == 1
    payload = json.loads(bells[0]["payload_json"])
    assert payload["files_count"] == 1
    assert payload["session_id"] == session_id
    assert Path(payload["workspace"]) == work

    # A second reconcile observes an already-done card -> no second bell.
    reconcile_board(collab._store, collab, sessions_dal=sessions, orchestrations_dal=orchestrations)
    assert len(_done_bells(db, card.id)) == 1

    sessions.close()
    orchestrations.close()


def test_reconcile_queries_are_bounded_for_mixed_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "bounded.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    try:
        for index in range(5):
            _run_card(collab, f"batch{index}")
            _session_card(collab, sessions, f"batch{index}")
            _orchestration_card(collab, orchestrations, f"batch{index}")
            collab.create_board_task(BoardTask(title=f"manual {index}"))

        counts = {"board": 0, "runs": 0, "steps": 0}
        list_board = collab.list_board_tasks
        get_runs = collab._store.get_runs_by_ids
        get_step_counts = collab._store.get_step_counts

        def board_batch(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            counts["board"] += 1
            return list_board(*args, **kwargs)

        def run_batch(ids: list[str]) -> dict[str, dict[str, Any]]:
            counts["runs"] += 1
            return get_runs(ids)

        def step_batch(ids: list[str]) -> dict[str, tuple[int, int]]:
            counts["steps"] += 1
            return get_step_counts(ids)

        monkeypatch.setattr(collab, "list_board_tasks", board_batch)
        monkeypatch.setattr(collab._store, "get_runs_by_ids", run_batch)
        monkeypatch.setattr(collab._store, "get_step_counts", step_batch)
        monkeypatch.setattr(
            collab._store,
            "get_run",
            lambda _run_id: pytest.fail("per-card get_run query"),
        )
        monkeypatch.setattr(
            collab._store,
            "get_steps",
            lambda _run_id: pytest.fail("per-card get_steps query"),
        )
        counting_sessions = _CountingSessions(sessions)
        counting_orchestrations = _CountingOrchestrations(orchestrations)

        rows = reconcile_board(
            collab._store,
            collab,
            sessions_dal=counting_sessions,
            orchestrations_dal=counting_orchestrations,
        )
        assert len(rows) == 20
        assert counts == {"board": 1, "runs": 1, "steps": 1}
        assert counting_sessions.calls == 1
        assert counting_orchestrations.batch_calls == 1
        assert counting_orchestrations.stale_calls == 1
    finally:
        sessions.close()
        orchestrations.close()


def test_board_api_returns_work_payload_for_all_link_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "api-work.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    run_card = _run_card(collab, "api")
    session_card = _session_card(collab, sessions, "api")
    orchestration_card = _orchestration_card(collab, orchestrations, "api")
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    monkeypatch.setattr(service, "_RECONCILE_DAL", sessions)
    monkeypatch.setitem(service._RECONCILE_ORCH_DALS, db, orchestrations)  # noqa: SLF001
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = asyncio.run(client.get("/api/board"))
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()}

        run_work = rows[run_card]["work"]
        assert set(run_work) == WORK_KEYS
        assert run_work["kind"] == "run"
        assert run_work["state"] == "running"
        assert (run_work["steps_done"], run_work["steps_total"]) == (1, 2)
        assert run_work["cost_usd"] == 1.25

        session_work = rows[session_card]["work"]
        assert set(session_work) == WORK_KEYS
        assert session_work["kind"] == "session"
        assert session_work["current_step"] == "working now"
        assert session_work["files_count"] == 2

        orchestration_work = rows[orchestration_card]["work"]
        assert set(orchestration_work) == WORK_KEYS
        assert orchestration_work["kind"] == "orchestration"
        assert orchestration_work["state"] == "running"
        assert orchestration_work["current_step"] == "quality review"
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        service._RECONCILE_ORCH_DALS.pop(db, None)  # noqa: SLF001
        sessions.close()
        orchestrations.close()


def test_reconcile_skips_swarm_cards(tmp_path: Path) -> None:
    """A swarm card's linked session must never drive its board status here —
    the swarm coordinator owns it (claims, attempts, blocked propagation)."""
    db = str(tmp_path / "swarm-skip.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    swarm_card = _session_card(collab, sessions, "swarm_member")
    plain_card = _session_card(collab, sessions, "plain")
    collab._store._write(
        "UPDATE board_tasks SET swarm_run_id = 'swr_test' WHERE id = ?", (swarm_card,)
    )
    try:
        rows = {
            row["id"]: row
            for row in reconcile_board(
                collab._store,
                collab,
                sessions_dal=sessions,
                orchestrations_dal=orchestrations,
            )
        }
        # The plain card follows its running session; the swarm card is left alone.
        assert rows[plain_card]["status"] == "in_progress"
        assert rows[swarm_card]["status"] == "open"
    finally:
        sessions.close()
        orchestrations.close()


def test_reconcile_preserves_human_terminal_cards_against_late_live_reads(tmp_path: Path) -> None:
    db = str(tmp_path / "terminal-guard.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    session_card = _session_card(collab, sessions, "human_done")
    orchestration_card = _orchestration_card(collab, orchestrations, "human_cancelled")
    collab.update_board_task(session_card, {"status": "done"})
    collab.update_board_task(orchestration_card, {"status": "cancelled"})
    try:
        rows = {
            row["id"]: row
            for row in reconcile_board(
                collab._store,
                collab,
                sessions_dal=sessions,
                orchestrations_dal=orchestrations,
            )
        }
        assert rows[session_card]["status"] == "done"
        assert rows[orchestration_card]["status"] == "cancelled"
    finally:
        sessions.close()
        orchestrations.close()


def test_reconcile_without_stale_rows_does_not_wait_for_writer_lock(tmp_path: Path) -> None:
    db = str(tmp_path / "read-only-reconcile.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    writer = sqlite3.connect(db, isolation_level=None, timeout=0.1)
    writer.execute("PRAGMA journal_mode=WAL")
    service._reset_reconcile_stale_throttle(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        rows = reconcile_board(
            collab._store,
            collab,
            sessions_dal=sessions,
            orchestrations_dal=orchestrations,
        )
        elapsed = time.monotonic() - started
        assert rows == []
        # CAUSAL, not calibrated. If reconcile_board took the writer lock it
        # would sit on the store's own busy_timeout (PRAGMA busy_timeout=5000,
        # omniagentos/db/store.py) before giving up, so "did not wait for the
        # writer lock" == "returned well inside 5s". `< 0.25` was a 16-P-core
        # Mac's number and fails on a 2-vCPU CI runner for being slow, which
        # says nothing about the locking behaviour under test.
        store_busy_timeout_seconds = 5.0
        assert elapsed < store_busy_timeout_seconds / 2
    finally:
        writer.rollback()
        writer.close()
        sessions.close()
        orchestrations.close()
