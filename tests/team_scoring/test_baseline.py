"""The baseline, and the ratio computed against it.

``production_x`` is the only number in the report with a denominator, so it is
the only one that can be wrong in the two classic ways: dividing by zero, and
counting the denominator as part of the numerator.
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.team.report import gather, render
from omniagentos.team.scoring import (
    BASELINE_SOURCE,
    baseline_points,
    compute_scores,
    production_x,
)
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW, WINDOW_START


def _baseline_card(make_task: Callable[..., str], owner: str, size: str, ref: str) -> str:
    """A P7-shaped BASE-* card: done, verified, sourced to the baseline week."""
    return make_task(
        owner=owner,
        size=size,
        ref=ref,
        title="Baseline week Mon 08-03 -> Sun 08-09",
        source=BASELINE_SOURCE,
        acceptance="",
        evidence=[("doc", f"baseline-2026-08-03-{ref.lower()}", "pass")],
        verified_at=IN_WINDOW,
    )


def test_baseline_points_are_scored_by_the_same_size_ladder(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    _baseline_card(make_task, employees["alice"], "L", "BASE-ALICE")
    _baseline_card(make_task, employees["owner"], "M", "BASE-OPS")
    _baseline_card(make_task, employees["bob"], "S", "BASE-BOB")

    assert baseline_points(team_store, employees["alice"]) == 8
    assert baseline_points(team_store, employees["owner"]) == 3
    assert baseline_points(team_store, employees["bob"]) == 1


def test_a_baseline_card_is_never_this_period_s_output(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    """The denominator must not appear in the numerator.

    The BASE-* cards are verified at IMPORT time, which lands inside the first
    live window. Counting them there would hand everyone a free 1.0x on day one
    without finishing anything — the single largest fabrication risk in the
    whole design, and the reason the exclusion is recorded by name rather than
    filtered away silently.
    """
    alice = employees["alice"]
    _baseline_card(make_task, alice, "L", "BASE-ALICE")

    breakdown = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[alice]
    assert breakdown.score == 0
    assert breakdown.counted == []
    assert [entry["reason"] for entry in breakdown.excluded] == ["baseline_period"]
    assert breakdown.excluded[0]["ref"] == "BASE-ALICE"
    # ...while still being the denominator.
    assert baseline_points(team_store, alice) == 8


def test_production_x_is_points_over_baseline(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    alice = employees["alice"]
    _baseline_card(make_task, alice, "L", "BASE-ALICE")
    make_task(
        owner=alice,
        size="M",
        ref="SP-9",
        title="This week's work",
        evidence=[("test_run", "tr-week", "pass")],
        verified_at=IN_WINDOW,
    )
    breakdown = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[alice]
    assert breakdown.score == 3
    assert production_x(breakdown.score, baseline_points(team_store, alice)) == 3 / 8


def test_a_missing_or_zero_baseline_is_none_never_a_number() -> None:
    """Two ways to have no denominator, one honest answer to both."""
    assert production_x(5, 0) is None
    assert production_x(5, None) is None
    assert production_x(0, 0) is None
    # A real baseline still divides normally, including down to zero output.
    assert production_x(0, 8) == 0.0


def test_the_report_says_no_baseline_rather_than_inventing_a_ratio(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    """Nobody has a baseline card, so nobody gets a multiplier — in the TEXT too."""
    make_task(
        owner=employees["alice"],
        size="M",
        ref="SP-10",
        title="Work with no baseline to compare against",
        evidence=[("test_run", "tr-nb", "pass")],
        verified_at=IN_WINDOW,
    )
    gathered = gather(team_store, DAY)
    text = render(gathered)

    assert all(person["production_x"] is None for person in gathered["people"])
    assert gathered["team"]["production_x"] is None
    assert "no baseline" in text
    assert "x (" not in text  # no "0.0x (0% to 10x)" anywhere
    assert "0.0x" not in text


def test_baseline_ignores_a_baseline_card_that_was_never_verified(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    """An unverified baseline card is not a measurement of anything."""
    make_task(
        owner=employees["bob"],
        size="L",
        ref="BASE-BOB",
        title="Baseline, unverified",
        source=BASELINE_SOURCE,
        acceptance="",
        done=True,
    )
    assert baseline_points(team_store, employees["bob"]) == 0
