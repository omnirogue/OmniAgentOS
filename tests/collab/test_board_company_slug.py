"""Top-level ``company_slug`` on board list reads, via the GOAL join.

Multi-company Work OS (2026-08-13): the collab LIST projection derives
``company_slug`` from ``goal_id -> company_goals -> org_companies`` — the same
join the team queues read, so the board chip and the queue chip can never
disagree. This is a separate channel from the org_json envelope enrichment
(which follows the project chain and stays untouched).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _goal(store: SqliteStore, *, slug: str = "acme", name: str = "ACME Corp") -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"co_{slug}", slug, name, "active", utc_now_iso()),
    )
    goal = CompanyGoalsStore(store).create_goal(
        org_company_id=f"co_{slug}", title=f"General engineering — {name}", horizon="quarter"
    )
    return str(goal["id"])


def test_list_projects_company_slug_through_the_goal_join(
    collab_store: CollabStore, store: SqliteStore
) -> None:
    goal_id = _goal(store)
    scoped = BoardTask(title="Scoped", goal_id=goal_id)
    unscoped = BoardTask(title="Unscoped")
    collab_store.create_board_task(scoped)
    collab_store.create_board_task(unscoped)

    by_id = {task["id"]: task for task in collab_store.list_board_tasks()}
    assert by_id[scoped.id]["company_slug"] == "acme"
    # No goal -> NULL, honestly absent rather than guessed.
    assert by_id[unscoped.id]["company_slug"] is None


def test_live_board_serves_company_slug(
    asgi_client: httpx.AsyncClient, collab_store: CollabStore, store: SqliteStore
) -> None:
    goal_id = _goal(store, slug="widgets", name="Widgets Inc")
    card = BoardTask(title="Widget work", goal_id=goal_id)
    collab_store.create_board_task(card)

    board = _run(asgi_client.get("/api/board")).json()
    served = next((task for task in board if task["id"] == card.id), None)
    assert served is not None
    assert served["company_slug"] == "widgets"


def test_open_tasks_for_carries_the_same_derived_field(
    collab_store: CollabStore, store: SqliteStore
) -> None:
    """Both projected list reads use one projection — the field cannot exist on
    one and be missing from the other."""
    goal_id = _goal(store, slug="both", name="Both Co")
    card = BoardTask(title="Claimable", goal_id=goal_id)
    collab_store.create_board_task(card)

    (task,) = collab_store.open_tasks_for([])
    assert task["company_slug"] == "both"


def test_goal_join_channel_is_independent_of_the_org_envelope(
    collab_store: CollabStore, store: SqliteStore
) -> None:
    """``company_slug`` (goal join) never writes into ``org`` (project chain):
    a goal-scoped card with no project keeps an unstamped envelope."""
    goal_id = _goal(store, slug="chan", name="Channel Co")
    card = BoardTask(title="Goal only", goal_id=goal_id)
    collab_store.create_board_task(card)

    by_id = {task["id"]: task for task in collab_store.list_board_tasks()}
    served = by_id[card.id]
    assert served["company_slug"] == "chan"
    context = (served.get("org") or {}).get("organization_context") or {}
    assert not context.get("company_slug")
