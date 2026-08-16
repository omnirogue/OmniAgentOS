"""Queue cards name their company and owner as server truth (2026-08-13).

The join is ``goal_id -> company_goals -> org_companies`` and it is LEFT: a
card with no goal (or a goal whose company row is gone) keeps its place in the
queue with NULL company fields — degraded, never dropped.
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore


def _company_goal(
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    *,
    slug: str = "acme",
    name: str = "ACME Corp",
) -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"co_{slug}", slug, name, "active", utc_now_iso()),
    )
    goal = goals_store.create_goal(
        org_company_id=f"co_{slug}", title=f"General engineering — {name}", horizon="quarter"
    )
    return str(goal["id"])


def test_pool_cards_carry_company_and_owner_fields(
    collab_store: CollabStore,
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    team_store: TeamStore,
) -> None:
    goal_id = _company_goal(store, goals_store)
    collab_store.create_board_task(
        BoardTask(title="Pool card", goal_id=goal_id, acceptance_criteria="the pool contract")
    )

    (card,) = team_store.pool_cards()
    assert card.company_slug == "acme"
    assert card.company_name == "ACME Corp"
    assert card.owner_employee_id is None  # the pool is ownerless by definition

    payload = team_store.pool_payload()
    assert payload["cards"][0]["company_slug"] == "acme"
    assert payload["cards"][0]["company_name"] == "ACME Corp"


def test_queue_buckets_carry_company_and_owner(
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    team_store: TeamStore,
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
) -> None:
    goal_id = _company_goal(store, goals_store)
    make_card(title="Scoped", owner_employee_id=employees["bob"], goal_id=goal_id)
    make_card(title="Unscoped", owner_employee_id=employees["bob"])

    ready = team_store.team_queues(employee_ids=[employees["bob"]])[employees["bob"]].ready
    by_title = {card.title: card for card in ready}

    assert by_title["Scoped"].company_slug == "acme"
    assert by_title["Scoped"].company_name == "ACME Corp"
    assert by_title["Scoped"].owner_employee_id == employees["bob"]
    # No goal -> no company, and the card is still in the queue (LEFT join).
    assert by_title["Unscoped"].company_slug is None
    assert by_title["Unscoped"].company_name is None


def test_priority_ranking_survives_the_company_join(
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    team_store: TeamStore,
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
) -> None:
    """The join must not perturb the base branch's rank order or its LIMIT."""
    goal_id = _company_goal(store, goals_store)
    make_card(
        title="Normal",
        owner_employee_id=employees["alice"],
        goal_id=goal_id,
        created_at="2026-08-01T00:00:00Z",
    )
    make_card(
        title="Urgent",
        owner_employee_id=employees["alice"],
        priority="urgent",
        created_at="2026-08-02T00:00:00Z",
    )

    ready = team_store.team_queues(employee_ids=[employees["alice"]])[employees["alice"]].ready
    assert [card.title for card in ready] == ["Urgent", "Normal"]
    assert ready[1].company_slug == "acme"
