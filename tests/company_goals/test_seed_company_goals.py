"""The Globex/AcmeUni/Hooli goal-ladder seeder: exact titles, idempotent.

The short_term title prefix ``General engineering — `` is a HARD contract —
the Slack ``#company`` flag and the board's company-goal lookup resolve on it
(``title LIKE 'General engineering%'``), so these tests pin the exact strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.company_goals.models import LONG_TERM, SHORT_TERM
from omniagentos.company_goals.seed_company_goals import (
    GOAL_OWNER,
    POOL_CARD_SOURCE,
    SEED_COMPANIES,
    seed_company_goals,
)
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "seed.db")


@pytest.fixture
def goals_store(store: SqliteStore) -> CompanyGoalsStore:
    return CompanyGoalsStore(store)


@pytest.fixture
def team_store(store: SqliteStore) -> TeamStore:
    return TeamStore(store)


def test_seeds_the_exact_ladder_for_all_three_companies(
    goals_store: CompanyGoalsStore, team_store: TeamStore
) -> None:
    outcomes = seed_company_goals(goals_store)

    assert {slug for slug, _ in SEED_COMPANIES} == set(outcomes)
    pool_cards = team_store.pool_cards()
    pool_by_id = {card.id: card for card in pool_cards}
    for slug, name in SEED_COMPANIES:
        assert outcomes[slug]["outcome"] == "created"
        child = goals_store.get_goal(outcomes[slug]["short_term"])
        parent = goals_store.get_goal(outcomes[slug]["long_term"])
        assert child is not None and parent is not None
        # EXACT title contract, including the em-dash spacing.
        assert child["title"] == f"General engineering — {name}"
        assert child["horizon"] == SHORT_TERM
        assert child["parent_goal_id"] == parent["id"]
        assert child["owner_employee_id"] == GOAL_OWNER
        assert parent["horizon"] == LONG_TERM
        assert parent["owner_employee_id"] == GOAL_OWNER
        assert parent["org_company_id"] == child["org_company_id"]

        # Every clause of the pool predicate (team/store.py:pool_cards): the
        # card this module minted is actually claimable, not just present.
        assert outcomes[slug]["card_created"] is True
        card_id = outcomes[slug]["card"]
        assert card_id in pool_by_id
        card = pool_by_id[card_id]
        assert card.status == "open"
        assert card.owner_employee_id is None
        assert card.source == POOL_CARD_SOURCE
        assert card.company_slug == slug


def test_rerun_is_idempotent(goals_store: CompanyGoalsStore) -> None:
    first = seed_company_goals(goals_store)
    second = seed_company_goals(goals_store)

    assert all(value["outcome"] == "created" for value in first.values())
    assert all(value["outcome"] == "existing" for value in second.values())
    for slug, _name in SEED_COMPANIES:
        company_row = goals_store._connection.execute(
            "SELECT id FROM org_companies WHERE slug = ?", (slug,)
        ).fetchone()
        assert company_row is not None
        goals = goals_store.list_goals(org_company_id=str(company_row["id"]))
        assert len(goals) == 2  # one parent, one child — never a second ladder

        # The card must not be re-minted on the second run either.
        assert second[slug]["card_created"] is False
        assert second[slug]["card"] == first[slug]["card"]
        cards = goals_store._connection.execute(
            "SELECT COUNT(*) AS n FROM board_tasks WHERE goal_id = ? AND source = ?",
            (first[slug]["short_term"], POOL_CARD_SOURCE),
        ).fetchone()
        assert int(cards["n"]) == 1


def test_a_preexisting_general_engineering_goal_skips_the_ladder_but_still_gets_a_card(
    store: SqliteStore, goals_store: CompanyGoalsStore, team_store: TeamStore
) -> None:
    from omniagentos.contracts import utc_now_iso

    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) "
        "VALUES ('co_acmeuni', 'acmeuni', 'AcmeUni', 'active', ?)",
        (utc_now_iso(),),
    )
    parent = goals_store.create_goal(
        org_company_id="co_acmeuni", title="AcmeUni already has a ladder", horizon=LONG_TERM
    )
    child = goals_store.create_goal(
        org_company_id="co_acmeuni",
        title="General engineering — AcmeUni (hand-made)",
        horizon=SHORT_TERM,
        parent_goal_id=str(parent["id"]),
    )

    outcomes = seed_company_goals(goals_store)

    assert outcomes["acmeuni"]["outcome"] == "existing"
    assert outcomes["acmeuni"]["short_term"] == str(child["id"])
    assert outcomes["acmeuni"]["card_created"] is True
    assert outcomes["globex"]["outcome"] == "created"
    assert outcomes["hooli"]["outcome"] == "created"

    # The pre-existing, hand-made ladder is the "goal exists but card does not"
    # case the plan calls out by name — it must still get a pool-eligible card.
    pool_slugs = {card.company_slug for card in team_store.pool_cards()}
    assert pool_slugs == {"acmeuni", "globex", "hooli"}

    # And re-running must not mint a second card for it.
    rerun = seed_company_goals(goals_store)
    assert rerun["acmeuni"]["outcome"] == "existing"
    assert rerun["acmeuni"]["card_created"] is False
    assert rerun["acmeuni"]["card"] == outcomes["acmeuni"]["card"]


def test_dry_run_writes_nothing(goals_store: CompanyGoalsStore) -> None:
    outcomes = seed_company_goals(goals_store, dry_run=True)
    assert all(value["outcome"] == "would-create" for value in outcomes.values())
    assert goals_store.list_goals() == []
    row = goals_store._connection.execute("SELECT COUNT(*) AS n FROM org_companies").fetchone()
    assert int(row["n"]) == 0
