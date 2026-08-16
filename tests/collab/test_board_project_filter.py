"""Project attribution on the board: the run chain, and the endpoint filter (S4).

TWO different resolutions, deliberately kept apart:

* ``CollabStore.board_task_project_map`` resolves a card's project one hop away
  through the run chain — ``board_tasks.run_id`` → ``runs.task_id`` →
  ``tasks.project_id``. These tests seed that real chain (projects → tasks →
  runs → board cards) and assert the mapping directly. It used to be applied by
  ``GET /api/collab/board``, which has been removed (a duplicate listing of the
  same table that no screen called).
* ``GET /api/board`` — the one surviving list — filters on the STORED
  migration-087 ``board_tasks.project_id`` column and does NOT backfill from the
  run chain: a card with no stored project reports ``null`` rather than a
  heuristically inferred project (P0-7, "honesty over heuristic backfills").
  Both halves are asserted below so neither can drift silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def seeded_chain(collab_store: CollabStore) -> dict[str, str]:
    """Seed two projects, each with task → run → board card, plus a standalone
    card with no run. Returns {"proj-a": card_id, "proj-b": card_id,
    "standalone": card_id}."""
    now = utc_now_iso()
    conn = collab_store._connection
    for proj_id, task_id, run_id in (
        ("proj-a", "tsk_proj_a", "run_proj_a"),
        ("proj-b", "tsk_proj_b", "run_proj_b"),
    ):
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (proj_id, f"Project {proj_id}", now),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, state, created_at, updated_at, project_id) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (task_id, f"Task on {proj_id}", now, now, proj_id),
        )
        conn.execute(
            "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
            "created_at, updated_at) VALUES (?, ?, 'agent', ?, 'queued', ?, ?, ?)",
            (run_id, task_id, f"trace_{run_id}", now, now, now),
        )
    # Commit BEFORE any CollabStore write: _write rolls back a pending implicit
    # transaction on the shared connection before opening its own.
    conn.commit()

    ids: dict[str, str] = {}
    for proj_id, run_id, title in (
        ("proj-a", "run_proj_a", "Project A card"),
        ("proj-b", "run_proj_b", "Project B card"),
    ):
        card = BoardTask(title=title, description=f"On {proj_id}")
        collab_store.create_board_task(card)
        collab_store.update_board_task(card.id, {"run_id": run_id})
        ids[proj_id] = card.id
    standalone = BoardTask(title="Standalone card", description="No project")
    collab_store.create_board_task(standalone)
    ids["standalone"] = standalone.id
    return ids


class TestRunChainProjectMap:
    """``board_task_project_map``: card → project through the run chain."""

    def test_chain_resolves_each_card_to_its_project(
        self, collab_store: CollabStore, seeded_chain: dict[str, str]
    ) -> None:
        mapping = collab_store.board_task_project_map(seeded_chain.values())
        assert mapping[seeded_chain["proj-a"]] == "proj-a"
        assert mapping[seeded_chain["proj-b"]] == "proj-b"

    def test_card_without_a_run_is_absent_not_guessed(
        self, collab_store: CollabStore, seeded_chain: dict[str, str]
    ) -> None:
        mapping = collab_store.board_task_project_map(seeded_chain.values())
        assert seeded_chain["standalone"] not in mapping

    def test_empty_input_is_an_empty_map(self, collab_store: CollabStore) -> None:
        assert collab_store.board_task_project_map([]) == {}


class TestBoardRouteProjectFilter:
    """``GET /api/board?project_id=`` filters on the STORED 087 column."""

    def test_no_filter_returns_all(
        self, asgi_client: httpx.AsyncClient, seeded_chain: dict[str, str]
    ) -> None:
        resp = _run(asgi_client.get("/api/board"))
        assert resp.status_code == 200
        tasks = resp.json()
        assert {t["id"] for t in tasks} == set(seeded_chain.values())

    def test_filter_matches_the_stored_project(
        self, asgi_client: httpx.AsyncClient, collab_store: CollabStore, seeded_chain: dict[str, str]
    ) -> None:
        collab_store.update_board_task(seeded_chain["proj-a"], {"project_id": "proj-a"})
        resp = _run(asgi_client.get("/api/board?project_id=proj-a"))
        assert resp.status_code == 200
        tasks = resp.json()
        assert [t["id"] for t in tasks] == [seeded_chain["proj-a"]]
        assert tasks[0]["title"] == "Project A card"

    def test_card_with_only_a_run_chain_is_not_backfilled(
        self, asgi_client: httpx.AsyncClient, seeded_chain: dict[str, str]
    ) -> None:
        """proj-b's card has the chain but no STORED project: null, and unscoped.

        The run-chain map above CAN resolve it; the endpoint deliberately does
        not use that to fill the column in. Pinned so a future backfill is a
        decision, not a drift.
        """
        by_id = {t["id"]: t for t in _run(asgi_client.get("/api/board")).json()}
        assert by_id[seeded_chain["proj-b"]]["project_id"] is None
        assert _run(asgi_client.get("/api/board?project_id=proj-b")).json() == []

    def test_filter_by_unknown_project_returns_empty(
        self, asgi_client: httpx.AsyncClient, seeded_chain: dict[str, str]
    ) -> None:
        resp = _run(asgi_client.get("/api/board?project_id=proj-unknown"))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_empty_project_id_returns_unfiltered(
        self, asgi_client: httpx.AsyncClient, seeded_chain: dict[str, str]
    ) -> None:
        resp = _run(asgi_client.get("/api/board?project_id="))
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_whitespace_project_id_matches_nothing(
        self, asgi_client: httpx.AsyncClient, seeded_chain: dict[str, str]
    ) -> None:
        """A blank-but-present filter is a filter: no project is named "   ".

        Pinned because it differs from the removed collab twin, which stripped
        the value and served the WHOLE board for it — the more surprising of the
        two answers to give a caller that asked to be scoped.
        """
        resp = _run(asgi_client.get("/api/board?project_id=%20%20%20"))
        assert resp.status_code == 200
        assert resp.json() == []
