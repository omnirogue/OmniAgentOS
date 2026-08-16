"""Standing coverage guard: every seeded company has >=1 pool-eligible card.

The multi-company Work OS draws autonomous work from ONE company-agnostic
pool (``TeamStore.pool_cards``, ``omniagentos/team/store.py``). The mechanical
defect this guards against is a favourable-absence one: ``seed_company_goals``
mints a goal ladder for :data:`SEED_COMPANIES` but, without the card-creation
call, never gives the pool anything to select — every company reads as
"configured" while contributing zero claimable work.

This test reuses the REAL pool predicate via ``TeamStore.pool_cards()``
rather than restating the SQL clauses inline: a restated predicate silently
stops matching the moment the real one changes (incomplete-propagation), and
that failure mode would make this guard worthless without ever going red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.company_goals.seed_company_goals import SEED_COMPANIES, seed_company_goals
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "seed_pool.db")


@pytest.fixture
def goals_store(store: SqliteStore) -> CompanyGoalsStore:
    return CompanyGoalsStore(store)


@pytest.fixture
def team_store(store: SqliteStore) -> TeamStore:
    return TeamStore(store)


def test_no_seeded_company_is_pool_eligible_before_seeding(team_store: TeamStore) -> None:
    """Red-first proof: an empty database has zero pool-eligible cards."""
    assert team_store.pool_cards() == []
    assert team_store.pool_depth() == 0


def test_every_seeded_company_has_at_least_one_pool_eligible_card(
    goals_store: CompanyGoalsStore, team_store: TeamStore
) -> None:
    seed_company_goals(goals_store)

    cards = team_store.pool_cards()
    slugs_with_cards = {card.company_slug for card in cards}

    for slug, name in SEED_COMPANIES:
        assert slug in slugs_with_cards, (
            f"{name} ({slug}) has zero pool-eligible cards after seeding — "
            "the goal ladder was minted with no claimable rung"
        )

    assert team_store.pool_depth() >= len(SEED_COMPANIES)


def test_rerunning_the_seeder_keeps_every_company_eligible_without_duplicating(
    goals_store: CompanyGoalsStore, team_store: TeamStore
) -> None:
    seed_company_goals(goals_store)
    seed_company_goals(goals_store)

    cards = team_store.pool_cards()
    slugs_with_cards = {card.company_slug for card in cards}
    assert {slug for slug, _ in SEED_COMPANIES} <= slugs_with_cards

    for slug, _name in SEED_COMPANIES:
        matching = [card for card in cards if card.company_slug == slug]
        assert len(matching) == 1, (
            f"{slug} has {len(matching)} pool-eligible cards after two seed "
            "runs; re-running must not duplicate the card"
        )
