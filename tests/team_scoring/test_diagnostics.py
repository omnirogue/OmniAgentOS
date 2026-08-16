"""Work-shape diagnostics: measured when there is something to measure, None otherwise.

The rule this file exists to hold is "a rate from zero samples is None, never
0". A 0% first-pass rate reads as "this person's work keeps failing"; the truth
in that case is "nothing was measured", and the two must never look alike in a
report three people are ranked by.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from omniagentos.team.diagnostics import compute_diagnostics
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW, WINDOW_START

BUCKETS_IN_WINDOW = 7 * 24


def _diagnostics(
    team_store: TeamStore, outcomes: dict[str, int] | None = None
) -> dict[str, object]:
    return {
        employee_id: value
        for employee_id, value in compute_diagnostics(
            team_store,
            period_start=WINDOW_START,
            period_end=DAY,
            verified_outcomes=outcomes or {},
        ).items()
    }


def test_no_sessions_is_unmeasured_not_zero(
    team_store: TeamStore, employees: dict[str, str]
) -> None:
    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)[
        employees["alice"]
    ]
    assert measured.session_count == 0
    assert measured.avg_active_sessions is None
    assert measured.peak_sessions is None
    assert measured.outcomes_per_session is None
    assert measured.merged_prs_per_session is None
    assert measured.first_pass_success is None


def test_concurrency_is_the_mean_and_peak_of_hourly_overlap(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
) -> None:
    """Two sessions running side by side for two hours: peak 2, mean 4/168."""
    bob = employees["bob"]
    first = make_task(owner=bob, size="M", ref="C-1", title="One")
    second = make_task(owner=bob, size="M", ref="C-2", title="Two")
    add_session(task_id=first, started_at="2026-08-12T09:00:00Z", ended_at="2026-08-12T11:00:00Z")
    add_session(task_id=second, started_at="2026-08-12T09:00:00Z", ended_at="2026-08-12T11:00:00Z")

    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)[bob]
    assert measured.session_count == 2
    assert measured.peak_sessions == 2
    assert measured.avg_active_sessions == 4 / BUCKETS_IN_WINDOW


def test_a_session_that_never_ended_is_open_to_the_window_edge_not_forever(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
) -> None:
    """``ended_at IS NULL`` means "still running" — clamped to the window end.

    Left unclamped, one never-closed row would report as concurrent in every
    future window forever.
    """
    bob = employees["bob"]
    task = make_task(owner=bob, size="M", ref="C-3", title="Still going")
    add_session(task_id=task, started_at="2026-08-14T22:00:00Z", ended_at=None, end_reason=None)

    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)[bob]
    assert measured.session_count == 1
    assert measured.peak_sessions == 1
    # 22:00-23:00 and 23:00-00:00 -> exactly two buckets at the tail of the window.
    assert measured.avg_active_sessions == 2 / BUCKETS_IN_WINDOW


def test_sessions_on_an_unowned_card_belong_to_nobody(
    team_store: TeamStore,
    employees: dict[str, str],
    collab_store: object,
    add_session: Callable[..., str],
) -> None:
    from omniagentos.collab.contracts import BoardTask
    from omniagentos.collab.store import CollabStore

    store: CollabStore = collab_store  # type: ignore[assignment]
    card = BoardTask(title="Swarm card")
    store.create_board_task(card)
    add_session(task_id=card.id)

    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)
    assert all(item.session_count == 0 for item in measured.values())


def test_merged_prs_count_only_merged_passing_prs(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    alice = employees["alice"]
    task = make_task(owner=alice, size="M", ref="PR-1", title="PR work")
    bulk_evidence(
        task_id=task,
        kind="pr",
        count=1,
        prefix="merged",
        meta_json=json.dumps({"state": "MERGED", "gate_attempts": 1}),
        created_at=IN_WINDOW,
    )
    bulk_evidence(
        task_id=task,
        kind="pr",
        count=1,
        prefix="open",
        meta_json=json.dumps({"state": "OPEN"}),
        created_at=IN_WINDOW,
    )
    bulk_evidence(
        task_id=task,
        kind="pr",
        count=1,
        prefix="closed",
        quality_gate="rejected",
        meta_json=json.dumps({"state": "CLOSED"}),
        created_at=IN_WINDOW,
    )

    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)[alice]
    assert measured.merged_prs == 1
    assert measured.first_pass_success == 1.0
    assert measured.first_pass_known == 1


def test_first_pass_excludes_unknown_attempts_from_the_denominator(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """Two measured PRs (one clean, one not) plus one the collector never graded.

    The rate is 1/2, not 1/3 — an unmeasured PR is not a failed one. And a
    person whose PRs are ALL ungraded gets None, not a flattering 100%.
    """
    alice, bob = employees["alice"], employees["bob"]
    alice_task = make_task(owner=alice, size="M", ref="PR-2", title="Alice PRs")
    for prefix, attempts in (("clean", 1), ("retried", 3)):
        bulk_evidence(
            task_id=alice_task,
            kind="pr",
            count=1,
            prefix=prefix,
            meta_json=json.dumps({"state": "MERGED", "gate_attempts": attempts}),
            created_at=IN_WINDOW,
        )
    bulk_evidence(
        task_id=alice_task,
        kind="pr",
        count=1,
        prefix="ungraded",
        meta_json=json.dumps({"state": "MERGED"}),
        created_at=IN_WINDOW,
    )

    bob_task = make_task(owner=bob, size="M", ref="PR-3", title="Bob PRs")
    bulk_evidence(
        task_id=bob_task,
        kind="pr",
        count=2,
        prefix="bob-ungraded",
        meta_json=json.dumps({"state": "MERGED"}),
        created_at=IN_WINDOW,
    )

    measured = compute_diagnostics(team_store, period_start=WINDOW_START, period_end=DAY)
    assert measured[alice].merged_prs == 3
    assert measured[alice].first_pass_known == 2
    assert measured[alice].first_pass_unknown == 1
    assert measured[alice].first_pass_success == 0.5

    assert measured[bob].merged_prs == 2
    assert measured[bob].first_pass_known == 0
    assert measured[bob].first_pass_success is None


def test_outcomes_per_session_uses_the_scorer_s_count(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
) -> None:
    """Diagnostics never counts outcomes itself — it is HANDED them.

    Passing a deliberately wrong count proves the direction of the dependency:
    the number that comes out is the one that went in, so no future edit can
    quietly let this module re-derive (and then re-weight) verified output.
    """
    bob = employees["bob"]
    task = make_task(
        owner=bob,
        size="M",
        ref="OPS-1",
        title="Real",
        evidence=[("test_run", "tr-ops", "pass")],
        verified_at=IN_WINDOW,
    )
    for index in range(4):
        add_session(
            task_id=task,
            started_at=f"2026-08-12T{index:02d}:00:00Z",
            ended_at=f"2026-08-12T{index:02d}:30:00Z",
        )

    measured = compute_diagnostics(
        team_store, period_start=WINDOW_START, period_end=DAY, verified_outcomes={bob: 2}
    )[bob]
    assert measured.session_count == 4
    assert measured.verified_outcomes == 2
    assert measured.outcomes_per_session == 0.5
