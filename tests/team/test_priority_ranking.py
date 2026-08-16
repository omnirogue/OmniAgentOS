"""Queue reads rank by priority, then by age.

The pool read is LIMITed, so the rank has to happen in SQL: a page taken before
the sort would hide exactly the urgent card the ranking exists to surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore


def _goal(store: SqliteStore, goals_store: CompanyGoalsStore) -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_rank", "rank", "Rank Co", "active", utc_now_iso()),
    )
    goal = goals_store.create_goal(
        org_company_id="co_rank", title="General engineering", horizon="quarter"
    )
    return str(goal["id"])


def test_ready_bucket_ranks_urgent_first_then_oldest(
    team_store: TeamStore,
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
) -> None:
    # Explicit created_at: several cards made in the same second would otherwise
    # tie and fall through to the id tie-break, which proves nothing about age.
    cards = [
        ("R1", "Old normal", "normal", "2026-08-01T00:00:00Z"),
        ("R2", "Low", "low", "2026-08-02T00:00:00Z"),
        ("R3", "Urgent", "urgent", "2026-08-03T00:00:00Z"),
        ("R4", "High", "high", "2026-08-04T00:00:00Z"),
        ("R5", "Second urgent", "urgent", "2026-08-05T00:00:00Z"),
        ("R6", "Nonsense", "wat", "2026-08-06T00:00:00Z"),
    ]
    for ref, title, priority, created_at in cards:
        make_card(
            title=title,
            ref=ref,
            owner_employee_id=employees["bob"],
            priority=priority,
            created_at=created_at,
        )

    ready = team_store.team_queues()[employees["bob"]].ready

    assert [card.ref for card in ready] == ["R3", "R5", "R4", "R1", "R2", "R6"]
    assert [card.priority for card in ready[:2]] == ["urgent", "urgent"]


def test_pool_ranks_urgent_first_even_when_truncated(
    collab_store: CollabStore,
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    team_store: TeamStore,
) -> None:
    goal_id = _goal(store, goals_store)
    for index in range(3):
        collab_store.create_board_task(
            BoardTask(
                title=f"Pool {index}", goal_id=goal_id, acceptance_criteria="the pool contract"
            )
        )
    collab_store.create_board_task(
        BoardTask(
            title="Pool urgent",
            goal_id=goal_id,
            acceptance_criteria="the pool contract",
            priority="urgent",
        )
    )

    assert [card.title for card in team_store.pool_cards(limit=1)] == ["Pool urgent"]
    assert [card.priority for card in team_store.pool_cards()] == [
        "urgent",
        "normal",
        "normal",
        "normal",
    ]


def test_queue_cards_carry_priority_into_the_api_payload(
    team_store: TeamStore,
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
) -> None:
    make_card(title="Urgent", owner_employee_id=employees["alice"], priority="urgent")

    payload: dict[str, Any] = team_store.team_queues(employee_ids=[employees["alice"]])[
        employees["alice"]
    ].model_dump_with_counts()

    assert payload["ready"][0]["priority"] == "urgent"
