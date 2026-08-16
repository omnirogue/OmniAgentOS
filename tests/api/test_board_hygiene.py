"""Board archive endpoints pause linked work before hiding cards."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.sessions.dal import SessionsDal


def test_bulk_archive_pauses_run_and_session_and_skips_unknown(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "board.db")
    collab = CollabStore(db)
    store = collab._store
    dispatched = dispatch_spec(
        store,
        collab,
        load_policy(),
        RefinedSpec(title="Live run", description="do work"),
        harness=HarnessType.MOCK.value,
    )
    run_card_id = str(dispatched["board_task"]["id"])
    run_id = str(dispatched["run_id"])

    session_dal = SessionsDal(db)
    session_dal.create_session(
        {
            "id": "ses_archive_live",
            "source": "bridge",
            "project_dir": str(tmp_path),
            "state": "running",
            "model": "test",
        }
    )
    session_card = BoardTask(title="Live session", result_ref="ses_archive_live")
    collab.create_board_task(session_card)
    from omniagentos.api.routes import sessions

    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: session_dal)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = asyncio.run(
            client.post(
                "/api/board/archive",
                json={"task_ids": [run_card_id, session_card.id, "btk_missing"]},
            )
        )
        assert response.status_code == 200
        assert response.json() == {
            "archived": [run_card_id, session_card.id],
            "skipped": [{"id": "btk_missing", "reason": "not_found"}],
            "paused_runs": [run_id],
            "paused_sessions": ["ses_archive_live"],
        }
        assert store.get_run(run_id)["cancel_requested"] == 1  # type: ignore[index]
        assert session_dal.get_session("ses_archive_live")["kill_requested"] == 1  # type: ignore[index]

        single = asyncio.run(client.post(f"/api/board/{run_card_id}/archive"))
        assert single.status_code == 200
        # Second archive call is a no-op: no re-pause
        assert single.json()["paused_run"] is None
        assert single.json()["paused_session"] is None
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        session_dal.close()


def test_second_archive_is_noop_no_repauses(tmp_path: Path, monkeypatch) -> None:
    """A second archive call on an already-archived card must not re-pause work."""
    db = str(tmp_path / "board.db")
    collab = CollabStore(db)
    store = collab._store
    dispatched = dispatch_spec(
        store,
        collab,
        load_policy(),
        RefinedSpec(title="Work", description="do it"),
        harness=HarnessType.MOCK.value,
    )
    run_card_id = str(dispatched["board_task"]["id"])
    run_id = str(dispatched["run_id"])

    session_dal = SessionsDal(db)
    from omniagentos.api.routes import sessions

    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: session_dal)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        first = asyncio.run(client.post(f"/api/board/{run_card_id}/archive"))
        assert first.status_code == 200
        assert first.json()["paused_run"] == run_id

        # Verify the run was marked for cancellation
        assert store.get_run(run_id)["cancel_requested"] == 1  # type: ignore[index]

        # Second call to the same card must not re-pause
        second = asyncio.run(client.post(f"/api/board/{run_card_id}/archive"))
        assert second.status_code == 200
        assert second.json()["paused_run"] is None
        assert second.json()["paused_session"] is None
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        session_dal.close()


def test_bulk_archive_continues_past_exception_and_reports(tmp_path: Path, monkeypatch) -> None:
    """Bulk archive must continue past per-card exceptions and report them in skipped."""
    db = str(tmp_path / "board.db")
    collab = CollabStore(db)
    store = collab._store

    # Create a normal card
    good_card = BoardTask(title="Good", description="archivable")
    collab.create_board_task(good_card)

    # Create a card that will cause an exception: missing run/session with bad data
    bad_card = BoardTask(title="Bad", run_id="run_missing")
    collab.create_board_task(bad_card)

    # Create another normal card
    good_card2 = BoardTask(title="Good2", description="also archivable")
    collab.create_board_task(good_card2)

    session_dal = SessionsDal(db)
    from omniagentos.api.routes import sessions

    # Mock collab_store.update_board_task to raise an exception for bad_card.id
    original_update = collab.update_board_task

    def mock_update(task_id: str, fields: dict) -> None:
        if task_id == bad_card.id:
            raise RuntimeError("Simulated update failure")
        return original_update(task_id, fields)

    monkeypatch.setattr(collab, "update_board_task", mock_update)
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: session_dal)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = asyncio.run(
            client.post(
                "/api/board/archive",
                json={"task_ids": [good_card.id, bad_card.id, good_card2.id]},
            )
        )
        assert response.status_code == 200
        resp_json = response.json()

        # good_card and good_card2 should be archived
        assert good_card.id in resp_json["archived"]
        assert good_card2.id in resp_json["archived"]

        # bad_card should be in skipped with error reason
        assert len(resp_json["skipped"]) == 1
        assert resp_json["skipped"][0]["id"] == bad_card.id
        assert resp_json["skipped"][0]["reason"].startswith("error:")
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        session_dal.close()
