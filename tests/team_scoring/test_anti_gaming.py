"""The anti-gaming battery. This file is the point of the scoring package.

Every test here follows the same shape: build a CONTROL scenario, record the
score, apply the gaming move, record the score again, and assert the DELTA. A
test that only asserted a final number would still pass if the move happened to
land on the same total by accident; asserting the delta says the move itself was
worth nothing, which is the property being claimed.

The moves are the real ones, not hypotheticals — every one of them is something
a person or an agent under pressure will actually do, and several of them are
things the previous 07:45 scoreboard REWARDED (it paid
``real_commits + 3 x merged_PRs``).
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.diagnostics import compute_diagnostics
from omniagentos.team.scoring import compute_scores
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW, WINDOW_START


def _score(team_store: TeamStore, employee_id: str) -> int:
    return compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[employee_id].score


def test_split_farming_a_card_cut_into_twelve_is_worth_the_same(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """One M card = 3 points. The SAME card plus twelve done subtasks = 3 points.

    Splitting work is a legitimate planning move and must stay free. It is only
    a gaming move if it pays, so subtasks carry no points at all and the
    parent's size prices the whole job.
    """
    bob = employees["bob"]
    parent = make_task(
        owner=bob,
        size="M",
        ref="UP-1",
        title="Ship the shared queue",
        evidence=[("test_run", "tr-parent", "pass")],
        verified_at=IN_WINDOW,
    )
    control = _score(team_store, bob)
    assert control == 3

    for index in range(12):
        make_task(
            owner=bob,
            size="L",  # even L-sized subtasks: the size of a child is not a price
            title=f"subtask {index}",
            parent_task_id=parent,
            acceptance="",
            verified_at=IN_WINDOW,
        )

    assert _score(team_store, bob) - control == 0


def test_commit_inflation_five_hundred_commits_move_nothing(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """Evidence is a RECEIPT, never a term. It cannot create or enlarge a point.

    Two halves, because the two failure modes are different: 500 commits on an
    unverified card must not conjure a score out of nothing, and 500 commits on
    a verified S card must not enlarge the one point it already earned.
    """
    bob = employees["bob"]
    unverified = make_task(
        owner=bob,
        size="L",
        ref="UP-2",
        title="Unfinished work",
        evidence=[("commit", "seed", "pass")],
        done=True,
    )
    assert _score(team_store, bob) == 0

    bulk_evidence(task_id=unverified, kind="commit", count=500, prefix="unverified")
    assert _score(team_store, bob) == 0, "commits on an unverified card are still zero"

    make_task(
        owner=bob,
        size="S",
        ref="UP-3",
        title="A small finished thing",
        evidence=[("test_run", "tr-small", "pass")],
        verified_at=IN_WINDOW,
    )
    control = _score(team_store, bob)
    assert control == 1

    bulk_evidence(task_id=unverified, kind="commit", count=500, prefix="more")
    assert _score(team_store, bob) - control == 0


def test_pr_spam_fifty_rejected_prs_move_nothing(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """Fifty rejected PRs are fifty pieces of evidence that the work did not land."""
    alice = employees["alice"]
    task = make_task(
        owner=alice,
        size="M",
        ref="SP-1",
        title="Queue spec",
        evidence=[("test_run", "tr-spec", "pass")],
        verified_at=IN_WINDOW,
    )
    control = _score(team_store, alice)
    assert control == 3

    bulk_evidence(task_id=task, kind="pr", count=50, quality_gate="rejected", prefix="spam")
    assert _score(team_store, alice) - control == 0


def test_session_spam_raises_no_score_and_lowers_the_diagnostic(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
) -> None:
    """Thirty sessions do not move the score, and they visibly WORSEN the ratio.

    This is the one place the two modules are asserted together: spawning
    sessions is free, so it must be free of upside — and because
    ``outcomes_per_session`` is a rate over real outcomes, running thirty
    sessions to produce the same one outcome reads as exactly what it is.
    """
    bob = employees["bob"]
    task = make_task(
        owner=bob,
        size="M",
        ref="UP-4",
        title="One real outcome",
        evidence=[("test_run", "tr-one", "pass")],
        verified_at=IN_WINDOW,
    )
    add_session(task_id=task, started_at="2026-08-12T09:00:00Z", ended_at="2026-08-12T10:00:00Z")
    control_score = _score(team_store, bob)
    control_diagnostics = compute_diagnostics(
        team_store, period_start=WINDOW_START, period_end=DAY, verified_outcomes={bob: 1}
    )[bob]
    assert control_score == 3
    assert control_diagnostics.outcomes_per_session == 1.0

    for index in range(30):
        add_session(
            task_id=task,
            started_at=f"2026-08-13T{index % 24:02d}:00:00Z",
            ended_at=f"2026-08-13T{index % 24:02d}:30:00Z",
        )

    after = compute_diagnostics(
        team_store, period_start=WINDOW_START, period_end=DAY, verified_outcomes={bob: 1}
    )[bob]
    assert _score(team_store, bob) - control_score == 0
    assert after.session_count == 31
    assert after.outcomes_per_session is not None
    assert after.outcomes_per_session < control_diagnostics.outcomes_per_session


def test_status_flapping_fifty_transitions_move_nothing(
    collab_store: CollabStore,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """Moving a card back and forth is free, and worth exactly what it costs."""
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="M",
        ref="SP-2",
        title="Real work",
        evidence=[("test_run", "tr-real", "pass")],
        verified_at=IN_WINDOW,
    )
    flapper = make_task(owner=alice, size="L", ref="SP-3", title="Busy card")
    control = _score(team_store, alice)
    assert control == 3

    for index in range(50):
        status = BoardTaskStatus.IN_PROGRESS.value if index % 2 == 0 else BoardTaskStatus.OPEN.value
        collab_store.update_board_task(flapper, {"status": status}, actor=alice)

    events = team_store.list_events(flapper)
    assert len([event for event in events if event["event"] == "status_change"]) == 50
    assert _score(team_store, alice) - control == 0


def test_done_without_verify_is_worth_zero_and_says_so(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """Done is a claim. Verified is the claim standing up. Only the second pays."""
    bob = employees["bob"]
    task = make_task(
        owner=bob,
        size="L",
        ref="UP-5",
        title="Says it is done",
        evidence=[("commit", "abc123", "pass")],
        done=True,
    )
    breakdown = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[bob]
    assert breakdown.score == 0
    assert breakdown.counted == []
    # ...and the card is NAMED, so "why is my number zero" has an answer.
    assert [entry["reason"] for entry in breakdown.excluded] == ["done_not_verified"]
    assert breakdown.excluded[0]["task_id"] == task


def test_easy_task_farming_the_arithmetic_is_pinned(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """Eight verified S cards == one verified L. Ten S < two L + one M.

    The size ladder is the only lever in the system, so its arithmetic is pinned
    here rather than left to POINTS_BY_SIZE's definition: 8xS==1xL is the exact
    indifference point that makes "cut everything into small cards" a
    strictly-more-work path to the same number, not a shortcut.
    """
    bob, alice = employees["bob"], employees["alice"]
    for index in range(8):
        make_task(
            owner=bob,
            size="S",
            ref=f"S-{index}",
            title=f"small {index}",
            evidence=[("test_run", f"tr-s-{index}", "pass")],
            verified_at=IN_WINDOW,
        )
    make_task(
        owner=alice,
        size="L",
        ref="L-1",
        title="one big thing",
        evidence=[("test_run", "tr-l", "pass")],
        verified_at=IN_WINDOW,
    )
    scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)
    assert scores[bob].score == 8
    assert scores[alice].score == 8
    assert scores[bob].score == scores[alice].score

    # Two more S for Bob (10 total) against one more L + one M for Alice.
    for index in range(8, 10):
        make_task(
            owner=bob,
            size="S",
            ref=f"S-{index}",
            title=f"small {index}",
            evidence=[("test_run", f"tr-s-{index}", "pass")],
            verified_at=IN_WINDOW,
        )
    make_task(
        owner=alice,
        size="L",
        ref="L-2",
        title="another big thing",
        evidence=[("test_run", "tr-l2", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=alice,
        size="M",
        ref="M-1",
        title="a medium thing",
        evidence=[("test_run", "tr-m", "pass")],
        verified_at=IN_WINDOW,
    )
    scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)
    assert scores[bob].score == 10
    assert scores[alice].score == 19
    assert scores[bob].score < scores[alice].score


def test_excessive_attempts_evidence_verifies_nothing_and_flips_no_rate(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """A PR that took three gate attempts is a record of cost, not of output.

    Three separate guarantees, and they are separate on purpose:

    1. ``verify_task`` will not treat it as MECHANICAL evidence, so the card
       cannot self-verify through it (the owner is refused).
    2. A human CAN still verify the card — the work may well be fine — but
    3. scoring re-checks the evidence anyway and refuses the card, because its
       whole evidence trail says the work did not land cleanly.
    """
    bob = employees["bob"]
    task = make_task(
        owner=bob,
        size="L",
        ref="UP-6",
        title="Landed on the third try",
        evidence=[
            (
                "pr",
                "initech/initech#41",
                "excessive_attempts",
                {"state": "MERGED", "gate_attempts": 3},
            )
        ],
        done=True,
    )

    # (1) the owner cannot ride this evidence to a self-verification
    try:
        team_store.verify_task(task, bob)
    except ValueError as exc:
        assert "cannot verify their own task" in str(exc)
    else:  # pragma: no cover - the assertion below is the real failure message
        raise AssertionError("excessive_attempts evidence must not count as mechanical")

    # (2) a second person may still verify it
    verified = team_store.verify_task(task, employees["alice"])
    assert verified is not None and verified["verified_at"] is not None

    # (3) scoring refuses it anyway, and names the reason
    breakdown = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[bob]
    assert breakdown.score == 0
    reasons = {entry["reason"] for entry in breakdown.excluded}
    assert reasons == {"evidence_excessive_attempts", "no_passing_evidence"}

    # ...and it never enters the first-pass rate, in either direction.
    diagnostics = compute_diagnostics(
        team_store, period_start=WINDOW_START, period_end=DAY, verified_outcomes={bob: 0}
    )[bob]
    assert diagnostics.merged_prs == 0
    assert diagnostics.first_pass_success is None
    assert diagnostics.first_pass_known == 0


def test_reverted_evidence_is_excluded_by_name(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """A reverted commit is recorded, refused, and REPORTED as refused.

    The card still counts — it carries a passing test run — so this also pins
    that a single bad artifact does not void a card that otherwise landed. What
    it must never do is disappear silently.
    """
    alice = employees["alice"]
    task = make_task(
        owner=alice,
        size="M",
        ref="SP-4",
        title="Landed, with one revert on the way",
        evidence=[
            ("test_run", "tr-ok", "pass"),
            ("commit", "deadbeef", "reverted"),
        ],
        verified_at=IN_WINDOW,
    )
    breakdown = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)[alice]

    assert breakdown.score == 3
    assert [entry["ref"] for entry in breakdown.counted] == ["SP-4"]
    assert breakdown.counted[0]["evidence_refs"] == ["tr-ok"]
    excluded = [entry for entry in breakdown.excluded if entry["reason"] == "evidence_reverted"]
    assert len(excluded) == 1
    assert excluded[0]["ref"] == "deadbeef"
    assert excluded[0]["task_id"] == task


def test_a_card_verified_outside_the_window_is_not_this_week(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """The window is a window. Last month's L does not pay again this week."""
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="L",
        ref="OLD-1",
        title="Finished in July",
        evidence=[("test_run", "tr-july", "pass")],
        verified_at="2026-07-01T10:00:00Z",
    )
    assert _score(team_store, alice) == 0


def test_an_unowned_agent_card_belongs_to_nobody(
    collab_store: CollabStore,
    team_store: TeamStore,
    employees: dict[str, str],
) -> None:
    """Agent cards keep working exactly as before 123 — and score for no one."""
    from omniagentos.collab.contracts import BoardTask

    card = BoardTask(title="Swarm card", size="L")
    collab_store.create_board_task(card)
    collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
    team_store.verify_task(card.id, "emp_owner")

    scores = compute_scores(team_store, period_start=WINDOW_START, period_end=DAY)
    assert sum(breakdown.score for breakdown in scores.values()) == 0
