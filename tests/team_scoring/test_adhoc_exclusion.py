"""Ad-hoc Tasks (source='task-adhoc') are worth ZERO points — v4, the operator 2026-08-13.

The exclusion happens at the card-gathering stage of ``compute_scores``: a Task
never scores, never appears in the refusal (``excluded``) listings, and never
moves anyone's pace — the cheapest way to raise the number stays "finish
verified Work", and handing out minor errands cannot dilute it.
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.team.contracts import TASK_ADHOC_SOURCE
from omniagentos.team.scoring import compute_scores

from .conftest import DAY, IN_WINDOW, WINDOW_START


class TestAdhocExclusion:
    def test_a_verified_sized_adhoc_task_scores_zero(
        self, team_store, make_task: Callable[..., str], employees: dict[str, str]
    ) -> None:
        # An L Task, done AND verified in-window — 8 points if it were Work.
        make_task(
            owner=employees["bob"],
            size="L",
            ref="T1",
            title="Buy the domain",
            source=TASK_ADHOC_SOURCE,
            evidence=[("test_run", "run-T1", "pass")],
            verified_at=IN_WINDOW,
        )
        # A normal M Work card alongside it, same window.
        make_task(
            owner=employees["bob"],
            size="M",
            ref="W1",
            title="Ship the fix",
            evidence=[("test_run", "run-W1", "pass")],
            verified_at=IN_WINDOW,
        )

        scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)
        bob = scores[employees["bob"]]

        assert bob.score == 3  # the Work card alone; the L Task adds nothing
        assert [entry["ref"] for entry in bob.counted] == ["W1"]

    def test_an_adhoc_task_never_appears_in_the_refusal_listings(
        self, team_store, make_task: Callable[..., str], employees: dict[str, str]
    ) -> None:
        # Done-but-unverified Work in-window IS a refusal worth listing…
        make_task(
            owner=employees["bob"],
            ref="W2",
            title="Work, done not verified",
            evidence=[("test_run", "run-W2", "pass")],
            done=True,
            updated_at=IN_WINDOW,
        )
        # …but the same shape as a Task is not even gathered.
        make_task(
            owner=employees["bob"],
            ref="T2",
            title="Task, done not verified",
            source=TASK_ADHOC_SOURCE,
            evidence=[("test_run", "run-T2", "pass")],
            done=True,
            updated_at=IN_WINDOW,
        )

        scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)
        bob = scores[employees["bob"]]

        assert [item["ref"] for item in bob.excluded] == ["W2"]
        assert all(item.get("ref") != "T2" for item in bob.excluded)

    def test_work_scoring_is_unchanged_by_the_filter(
        self, team_store, make_task: Callable[..., str], employees: dict[str, str]
    ) -> None:
        make_task(
            owner=employees["alice"],
            size="S",
            ref="W3",
            evidence=[("test_run", "run-W3", "pass")],
            verified_at=IN_WINDOW,
        )
        make_task(
            owner=employees["alice"],
            size="L",
            ref="W4",
            evidence=[("test_run", "run-W4", "pass")],
            verified_at=IN_WINDOW,
        )

        scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)

        assert scores[employees["alice"]].score == 9  # S=1 + L=8, exactly as before
