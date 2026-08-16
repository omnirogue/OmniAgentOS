"""Fixtures for P6 (Slack inbound task updates).

Mirrors ``tests/team/conftest.py``: both stores composed on the SAME
``SqliteStore`` (one connection, one writer lock), which is the production
composition ``slack_updates.team_updates_handle`` uses.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def collab_store(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "team_slack.db")


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
    """The full roster :data:`omniagentos.team.slack_updates.KNOWN_EMPLOYEES` names."""
    roster = {
        "owner": ("emp_owner", "the operator", "operator"),
        "alice": ("emp_alice", "Alice", "reviewer-merger"),
        "bob": ("emp_bob", "Bob", "candidate-author"),
        "frank": ("emp_frank", "Frank", "candidate-author"),
    }
    for employee_id, name, role in roster.values():
        goals_store.ensure_employee(employee_id=employee_id, name=name, role=role)
    return {key: value[0] for key, value in roster.items()}


@pytest.fixture
def initech_goal(store: SqliteStore, goals_store: CompanyGoalsStore) -> str:
    """``#initech``'s general-engineering goal — what a company flag resolves to.

    A second, non-general goal is seeded alongside it so a passing resolution
    proves the title filter, not merely "this company has exactly one goal".
    """
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_initech", "initech", "Initech", "active", utc_now_iso()),
    )
    goals_store.create_goal(
        org_company_id="co_initech", title="Ship the launch", horizon="quarter"
    )
    goal = goals_store.create_goal(
        org_company_id="co_initech",
        title="General engineering — Initech",
        horizon="quarter",
    )
    return str(goal["id"])


@pytest.fixture
def make_card(collab_store: CollabStore) -> Callable[..., BoardTask]:
    """Create a board card and return it. Keyword args mirror ``BoardTask``."""

    def factory(**fields: Any) -> BoardTask:
        fields.setdefault("title", "A card")
        card = BoardTask(**fields)
        collab_store.create_board_task(card)
        return card

    return factory


@pytest.fixture
def slack_map(employees: dict[str, str]) -> dict[str, str]:
    """A Slack sender map keyed on plain, readable ids (not the real placeholders)."""
    return {
        "U0TEAM": employees["owner"],
        "U0ALICE": employees["alice"],
        "U0BOB": employees["bob"],
        "U0ANDY": employees["frank"],
    }


def message_event(
    *,
    channel: str = "C0000EXAMPLE",
    user: str = "U0BOB",
    text: str = "done U3 shipped it",
    ts: str = "1700000000.000100",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event
