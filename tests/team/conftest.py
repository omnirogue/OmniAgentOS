"""Fixtures for the Team Work OS core data layer (migration 123).

Every fixture puts BOTH stores on the SAME :class:`SqliteStore`, which is the
composition the production wiring uses: one connection, one writer lock, one
``BEGIN IMMEDIATE``. Two stores over two locks on one file would serialize only
through SQLite's busy handler, and the hierarchy/done rules are exactly the ones
whose correctness depends on the read and the write being in one transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def collab_store(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "team.db")


@pytest.fixture
def store(collab_store: CollabStore) -> SqliteStore:
    return collab_store._store


@pytest.fixture
def team_store(store: SqliteStore) -> TeamStore:
    return TeamStore(store)


@pytest.fixture
def goals_store(store: SqliteStore) -> CompanyGoalsStore:
    return CompanyGoalsStore(store)


@pytest.fixture
def employees(goals_store: CompanyGoalsStore) -> dict[str, str]:
    """The three people the Team Work OS rules name, with roles set."""
    roster = {
        "owner": ("emp_owner", "the operator", "operator"),
        "alice": ("emp_alice", "Alice", "reviewer-merger"),
        "bob": ("emp_bob", "Bob", "candidate-author"),
    }
    for employee_id, name, role in roster.values():
        goals_store.ensure_employee(employee_id=employee_id, name=name, role=role)
    return {key: value[0] for key, value in roster.items()}


@pytest.fixture
def api(
    collab_store: CollabStore,
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[httpx.AsyncClient]:
    """The Team API over the SAME stores the fixtures above hand out.

    Deliberately the same objects, not a second store on the same file: a route
    and the test that seeded it must share one writer lock, or the test races
    the request it just made. Mirrors ``tests/team_api/test_routes.py``'s
    fixture, plus its hermetic fleet-ledger guard — no unit test may read the
    serving checkout's real ledger.
    """
    monkeypatch.setenv("OMNI_TEAM_FLEET_LEDGER", str(tmp_path / "no-such-ledger.jsonl"))
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab_store
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Session-Token": token.load_or_create_token()},
    )
    try:
        yield client
    finally:
        app.dependency_overrides = previous
        asyncio.run(client.aclose())


@pytest.fixture
def automation_goals(store: SqliteStore, goals_store: CompanyGoalsStore) -> dict[str, str]:
    """The automation goal ladder, as the operator creates it through the API.

    Live DATA, not schema (this package ships no migration), so every surface
    that files an automation proposal needs it seeded — a database without the
    parent goal refuses proposal creation outright rather than persisting a
    goal-less card that could never reach the pool.
    """
    from omniagentos.team.contracts import AUTOMATION_PARENT_GOAL_ID

    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) "
        "VALUES ('co_omniagentos', 'omniagentos', 'OmniAgentOS', 'active', "
        "'2026-08-14T00:00:00Z')"
    )
    goals_store.create_goal(
        org_company_id="co_omniagentos",
        title="Automate 100% of the operator's tasks",
        horizon="long_term",
        goal_id=AUTOMATION_PARENT_GOAL_ID,
    )
    created: dict[str, str] = {}
    for name in ("email & comms", "content & marketing", "ads", "dev tooling"):
        goal = goals_store.create_goal(
            org_company_id="co_omniagentos",
            title=f"Automations — {name}",
            horizon="short_term",
            parent_goal_id=AUTOMATION_PARENT_GOAL_ID,
        )
        created[name] = str(goal["id"])
    return created


@pytest.fixture
def make_card(collab_store: CollabStore) -> Callable[..., BoardTask]:
    """Create a board card and return it. Keyword args mirror ``BoardTask``."""

    def factory(**fields: Any) -> BoardTask:
        fields.setdefault("title", "A card")
        card = BoardTask(**fields)
        collab_store.create_board_task(card)
        return card

    return factory
