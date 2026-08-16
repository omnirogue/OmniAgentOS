"""GET /api/board/{id}/sessions + POST /api/board/{id}/pause.

The unified session lookup is what lets the task-details page show the
models working an ACTIVE card (result_ref is orch_/null until DONE, so the
old ses_-only fallback rendered nothing); pause is the board's stop-without-
archive action, covering the orchestrate lane the archive helper used to
miss.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.categories import get_longhaul_store
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.longhaul import LonghaulStore
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.token import load_or_create_token
from tests.support.db_template import migrated_db


@pytest.fixture
def board_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db = str(tmp_path / "board.db")
    migrated_db(SqliteStore, db)
    collab = CollabStore(db)
    sessions_dal = SessionsDal(db)
    longhaul = LonghaulStore(db)
    from omniagentos.api.routes import sessions as sessions_routes

    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: sessions_dal)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[get_longhaul_store] = lambda: longhaul
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield {
            "client": client,
            "collab": collab,
            "sessions": sessions_dal,
            "db": db,
            "token": load_or_create_token(),
        }
    finally:
        app.dependency_overrides.clear()
        asyncio.run(client.aclose())


def _seed_orchestrated_card(
    fx: dict[str, Any], *, session_state: str = "running"
) -> tuple[str, str, str]:
    """Card whose result_ref is a live orchestration with one step session."""
    for suffix, state in (("live", session_state), ("done", "completed")):
        fx["sessions"].create_session(
            {
                "id": f"ses_step_{suffix}",
                "source": "bridge",
                "project_dir": "/tmp/w",
                "state": state,
                "model": "fable" if suffix == "live" else "sonnet",
                "provider": "claude",
                "title": f"step {suffix}",
            }
        )
    orch = OrchestrationsDal(fx["db"])
    orch_id = "orch_test1"
    card = BoardTask(title="Active orchestrated work", result_ref=orch_id)
    fx["collab"].create_board_task(card)
    orch.create(orch_id, board_task_id=card.id, working_dir="/tmp/w", goal="g")
    orch.set_status(orch_id, "running")
    orch.record_plan(orch_id, '{"steps": []}', ["prep", "build"])
    orch.step_session(orch_id, 0, "ses_step_done")
    orch.step_session(orch_id, 1, "ses_step_live")
    orch.close()
    return card.id, orch_id, "ses_step_live"


def test_sessions_endpoint_resolves_orchestration_steps(board_fixture: dict[str, Any]) -> None:
    fx = board_fixture
    card_id, orch_id, live_id = _seed_orchestrated_card(fx)
    response = asyncio.run(
        fx["client"].get(
            f"/api/board/{card_id}/sessions",
            headers={"X-Session-Token": fx["token"]},
        )
    )
    assert response.status_code == 200
    body = response.json()
    ids = {row["id"] for row in body["sessions"]}
    assert ids == {"ses_step_done", "ses_step_live"}
    assert body["live_session_id"] == live_id
    assert body["orchestration"]["id"] == orch_id
    assert body["orchestration"]["status"] == "running"
    live = next(row for row in body["sessions"] if row["id"] == live_id)
    assert live["model"] == "fable"
    assert live["source"] == "orchestration"
    assert live["step_title"] == "build"


def test_sessions_endpoint_direct_ses_ref(board_fixture: dict[str, Any]) -> None:
    fx = board_fixture
    fx["sessions"].create_session(
        {
            "id": "ses_direct1",
            "source": "bridge",
            "project_dir": "/tmp/w",
            "state": "running",
            "model": "haiku",
        }
    )
    card = BoardTask(title="Fast-lane card", result_ref="ses_direct1")
    fx["collab"].create_board_task(card)
    response = asyncio.run(
        fx["client"].get(
            f"/api/board/{card.id}/sessions",
            headers={"X-Session-Token": fx["token"]},
        )
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["sessions"]] == ["ses_direct1"]
    assert body["sessions"][0]["source"] == "direct"
    assert body["live_session_id"] == "ses_direct1"
    assert body["orchestration"] is None


def test_sessions_endpoint_404_unknown_card(board_fixture: dict[str, Any]) -> None:
    fx = board_fixture
    response = asyncio.run(
        fx["client"].get(
            "/api/board/btk_missing/sessions",
            headers={"X-Session-Token": fx["token"]},
        )
    )
    assert response.status_code == 404


def test_pause_cancels_orchestration_and_is_resumable(board_fixture: dict[str, Any]) -> None:
    fx = board_fixture
    card_id, orch_id, live_id = _seed_orchestrated_card(fx)
    fx["collab"].update_board_task(card_id, {"status": BoardTaskStatus.IN_PROGRESS.value})

    response = asyncio.run(fx["client"].post(f"/api/board/{card_id}/pause", json={}))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["resumable"] is True
    assert body["paused_run"] == orch_id
    assert body["paused_session"] == live_id

    # The live step session got a cancel request (request_cancel sets the
    # kill_requested flag with cancel attribution); the finished one untouched.
    live = fx["sessions"].get_session(live_id)
    assert live is not None and live["kill_requested"] == 1
    assert live["killed_by"] == "cancel_requested"
    done = fx["sessions"].get_session("ses_step_done")
    assert done is not None and done["kill_requested"] == 0

    # Orchestration is terminal-cancelled (resumable via the retry route).
    orch = OrchestrationsDal(fx["db"])
    try:
        row = orch.get(orch_id)
        assert row is not None and row["status"] == "cancelled"
    finally:
        orch.close()

    card = fx["collab"].get_board_task(card_id)
    assert card is not None and card["status"] == "cancelled"


def test_pause_is_idempotent_and_keeps_done_cards(board_fixture: dict[str, Any]) -> None:
    fx = board_fixture
    card = BoardTask(title="Finished card")
    fx["collab"].create_board_task(card)
    fx["collab"].update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
    response = asyncio.run(fx["client"].post(f"/api/board/{card.id}/pause", json={}))
    assert response.status_code == 200
    row = fx["collab"].get_board_task(card.id)
    assert row is not None and row["status"] == "done"  # done cards stay done


def test_sessions_endpoint_run_root_returns_all_member_attempts(
    board_fixture: dict[str, Any],
) -> None:
    """The run ROOT card resolves EVERY member attempt across the run — the
    'Open full view' surface renders all transcripts from this."""
    import json as jsonlib
    import sqlite3

    fx = board_fixture
    for suffix in ("m1", "m2"):
        fx["sessions"].create_session(
            {
                "id": f"ses_swarm_{suffix}",
                "source": "bridge",
                "project_dir": "/tmp/w",
                "state": "running" if suffix == "m2" else "completed",
                "model": "sonnet",
                "provider": "claude",
            }
        )
    run_id = "swr_root_test1"
    root = BoardTask(title="Swarm: build a site")
    member = BoardTask(title="member task")
    fx["collab"].create_board_task(root)
    fx["collab"].create_board_task(member)
    conn = sqlite3.connect(fx["db"])
    try:
        now = "2026-07-24T00:00:00Z"
        conn.execute(
            "INSERT INTO swarm_runs (id, status, goal, board_task_id, working_dir, "
            "plan_json, created_at, updated_at, source) VALUES (?, 'running', 'g', ?, '/tmp/w', "
            "'{}', ?, ?, ?)",
            (run_id, root.id, now, now, 'test'),
        )
        conn.execute(
            "UPDATE board_tasks SET swarm_run_id = ? WHERE id IN (?, ?)",
            (run_id, root.id, member.id),
        )
        # One CLOSED attempt + one LIVE attempt (the schema's one-live-
        # attempt-per-card invariant).
        conn.execute(
            "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
            "provider, model, tier, session_id, started_at, ended_at, end_reason, detail) "
            "VALUES ('swa_1', ?, ?, 1, 'claude', 'sonnet', 'simple', "
            "'ses_swarm_m1', ?, ?, 'completed', 'Acceptance confirmed')",
            (run_id, member.id, now, now),
        )
        conn.execute(
            "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
            "provider, model, session_id, started_at) "
            "VALUES ('swa_2', ?, ?, 2, 'claude', 'sonnet', 'ses_swarm_m2', ?)",
            (run_id, member.id, now),
        )
        conn.commit()
    finally:
        conn.close()

    response = asyncio.run(
        fx["client"].get(
            f"/api/board/{root.id}/sessions",
            headers={"X-Session-Token": fx["token"]},
        )
    )
    assert response.status_code == 200
    body = response.json()
    assert {row["id"] for row in body["sessions"]} == {"ses_swarm_m1", "ses_swarm_m2"}
    assert body["live_session_id"] == "ses_swarm_m2"
    assert all(row["source"] == "swarm" for row in body["sessions"])
    closed = next(row for row in body["sessions"] if row["id"] == "ses_swarm_m1")
    assert closed["tier"] == "simple"
    assert closed["end_reason"] == "completed"
    assert closed["detail"] == "Acceptance confirmed"
    del jsonlib  # imported for parity with sibling tests; schema uses raw SQL


def test_longhaul_endpoint_returns_every_swarm_attempt_for_board_task(
    board_fixture: dict[str, Any],
) -> None:
    """The Runs tab's endpoint must not claim an attempted swarm task is empty."""
    fx = board_fixture
    card = BoardTask(title="Three reviewed attempts")
    fx["collab"].create_board_task(card)
    run_id = "swr_attempt_truth"
    now = "2026-07-28T12:00:00Z"
    connection = sqlite3.connect(fx["db"])
    try:
        connection.execute(
            "INSERT INTO swarm_runs (id, status, goal, board_task_id, working_dir, "
            "plan_json, created_at, updated_at, source) VALUES (?, 'completed', 'g', ?, '/tmp/w', "
            "'{}', ?, ?, 'test')",
            (run_id, card.id, now, now),
        )
        connection.execute(
            "UPDATE board_tasks SET swarm_run_id = ? WHERE id = ?",
            (run_id, card.id),
        )
        for seq, model, tier in (
            (1, "grok-4.5", "simple"),
            (2, "grok-4.5", "standard"),
            (3, "gpt-5.6-sol", "complex"),
        ):
            connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, provider, model, tier, "
                "started_at, ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, 'grok', ?, ?, ?, ?, 'review_denied', ?)",
                (
                    f"swa_truth_{seq}",
                    run_id,
                    card.id,
                    seq,
                    model,
                    tier,
                    now,
                    now,
                    f"Acceptance not met on attempt {seq}",
                ),
            )
        connection.commit()
    finally:
        connection.close()

    response = asyncio.run(
        fx["client"].get(
            f"/api/board/{card.id}/longhaul",
            headers={"X-Session-Token": fx["token"]},
        )
    )

    assert response.status_code == 200
    attempts = response.json()["attempts"]
    assert len(attempts) == 3
    assert [attempt["id"] for attempt in attempts] == [
        "swa_truth_1",
        "swa_truth_2",
        "swa_truth_3",
    ]
    assert [attempt["tier"] for attempt in attempts] == ["simple", "standard", "complex"]
    assert all(attempt["end_reason"] == "review_denied" for attempt in attempts)
    assert attempts[0]["detail"] == "Acceptance not met on attempt 1"
