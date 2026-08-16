"""Canary for the suite-wide pinned clock (Sol review of #497, finding 3).

The autouse ``_pin_store_clock`` fixture freezes ambient time at noon of DAY,
which makes "use the caller's explicit day" and "ignore it and use now()"
observationally identical for any assertion phrased against DAY. This module
holds the discriminating assertions: they query a window that EXCLUDES the
pinned date, so a regression that drops an explicit day/period argument in
favour of the ambient clock turns them red while every DAY-phrased test stays
green.
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.team.scoring import compute_scores
from omniagentos.team.store import TeamStore

from .conftest import DAY, WINDOW_START

DAY_BEFORE_PINNED = "2026-08-13"


def test_explicit_period_end_is_respected_under_the_pinned_clock(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    bob = employees["bob"]
    make_task(
        owner=bob,
        size="M",
        ref="CAN-1",
        title="Verified on the pinned day",
        evidence=[("commit", "canary", "pass")],
        verified_at=f"{DAY}T10:00:00Z",
    )
    # Window ending BEFORE the pinned day: the card must be outside it. A
    # mutant that replaces period_end with "today per the ambient clock"
    # (= the pinned DAY) counts the card here and fails.
    early = compute_scores(
        team_store, period_start=WINDOW_START, period_end=DAY_BEFORE_PINNED
    )[bob]
    assert early.score == 0
    assert early.counted == []
    # Same card, window ending on the pinned day: counted. This pins that the
    # zero above comes from the WINDOW argument, not from a broken card.
    full = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[bob]
    assert full.score > 0
