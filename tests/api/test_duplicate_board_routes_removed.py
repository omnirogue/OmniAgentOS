"""The two duplicate board routes are gone, and the survivor still does its job.

* ``GET /api/collab/board`` was a SECOND listing of ``board_tasks`` — 2.16 MB on
  the live board, unauthenticated, no reconcile, and no caller in the dashboard.
* ``POST /api/collab/board/{id}/archive`` was a second archive that only stamped
  ``archived_at``: archiving a card through it left the card's run/session
  RUNNING. The intake archive pauses linked work first, which is the whole
  difference, so it is the one that survived.

The second test is the one that matters: removing a duplicate is only safe if
the survivor's distinguishing behaviour is pinned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import agents_view
from omniagentos.sessions.dal import SessionsDal
from tests.support.db_template import migrated_db


@pytest.fixture(autouse=True)
def _no_live_agent_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the board-read path hermetic in the offline lane.

    Reading ``GET /api/board`` runs ``sync_external_sessions_to_board``, which
    enriches Claude rows via ``agents_view.collect_all`` — that spawns the
    ``claude`` provider CLI once per profile, which the offline pytest lane
    refuses with ``OfflineLaneViolation``. These tests exercise the board ROUTES,
    not Agent View enrichment, so default the collector to an empty result.
    """
    monkeypatch.setattr(agents_view, "collect_all", lambda: {})


def _run(coro: Any) -> httpx.Response:
    return asyncio.run(coro)


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    db = str(tmp_path / "dup-routes.db")
    migrated_db(SqliteStore, db)
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    from omniagentos.api.routes import sessions as sessions_routes

    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: sessions)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield {"client": client, "collab": collab, "sessions": sessions, "db": db}
    finally:
        app.dependency_overrides.clear()
        asyncio.run(client.aclose())


def test_duplicate_board_list_is_gone(board: dict[str, Any]) -> None:
    response = _run(board["client"].get("/api/collab/board"))
    assert response.status_code in (404, 405), response.text
    # …and the survivor answers.
    assert _run(board["client"].get("/api/board")).status_code == 200


def test_duplicate_archive_route_is_gone(board: dict[str, Any]) -> None:
    card = BoardTask(title="archive me")
    board["collab"].create_board_task(card)
    response = _run(board["client"].post(f"/api/collab/board/{card.id}/archive"))
    assert response.status_code in (404, 405), response.text
    assert (board["collab"].get_board_task(card.id) or {}).get("archived_at") is None


def test_restore_survives_on_the_collab_surface(board: dict[str, Any]) -> None:
    """Only ARCHIVE was a duplicate; restore has no intake counterpart."""
    card = BoardTask(title="restore me")
    board["collab"].create_board_task(card)
    board["collab"].update_board_task(card.id, {"archived_at": "2026-08-04T00:00:00Z"})
    response = _run(board["client"].post(f"/api/collab/board/{card.id}/restore"))
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None


def test_surviving_archive_still_pauses_linked_work(board: dict[str, Any]) -> None:
    """The reason the collab twin was the one deleted, asserted end to end."""
    collab: CollabStore = board["collab"]
    store = collab._store
    store.create_task(
        {
            "id": "tsk_pause",
            "title": "pausable",
            "created_at": "2026-08-04T00:00:00Z",
            "updated_at": "2026-08-04T00:00:00Z",
        }
    )
    store.enqueue_run(
        {
            "id": "run_pause",
            "task_id": "tsk_pause",
            "harness": "mock",
            "state": "running",
            "created_at": "2026-08-04T00:00:00Z",
            "updated_at": "2026-08-04T00:00:00Z",
            "queued_at": "2026-08-04T00:00:00Z",
            "trace_id": "trc_pause",
        }
    )
    board["sessions"].create_session(
        {
            "id": "ses_pause",
            "source": "bridge",
            "project_dir": "/tmp/pause",
            "state": "running",
            "model": "opus",
        }
    )
    card = BoardTask(title="live work", result_ref="ses_pause")
    collab.create_board_task(card)
    collab.update_board_task(card.id, {"run_id": "run_pause"})

    response = _run(board["client"].post(f"/api/board/{card.id}/archive"))
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["archived_at"] is not None
    assert row["paused_run"] == "run_pause"
    assert row["paused_session"] == "ses_pause"

    # Not just reported — actually requested on both linked lanes.
    assert store.get_run("run_pause")["cancel_requested"] in (1, True)
    session = board["sessions"].get_session("ses_pause")
    assert session["kill_requested"] in (1, True)


def test_archiving_an_already_archived_card_pauses_nothing(board: dict[str, Any]) -> None:
    """Archive stays idempotent: a second call is not a second cancel storm."""
    card = BoardTask(title="twice")
    board["collab"].create_board_task(card)
    first = _run(board["client"].post(f"/api/board/{card.id}/archive")).json()
    second = _run(board["client"].post(f"/api/board/{card.id}/archive")).json()
    assert second["archived_at"] == first["archived_at"]
    assert second["paused_run"] is None and second["paused_session"] is None
