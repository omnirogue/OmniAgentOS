"""Doctrine: a NON-RESULT is neither a success nor a failure.

Why this file exists
--------------------
The defect it pins shipped BECAUSE the tests asserted the wrong invariant.
``ACCEPTED_STATUSES`` contained ``parked`` and ``idle``, and
``tests/scheduler/test_loop_jobs.py`` asserted, in as many words, that a parked
tick "is recorded as accepted". Every suite was green while
``rtn_1e5567b9f3314a2c9d76`` (w3-health-monitor) reported total_runs=2,
accepted_runs=2, acceptance_rate=1.0, status=active — with both runs parking the
same approval and healing nothing. A dead loop and a working loop were the same
row.

The invariant, stated once
--------------------------
A run that produced no judgeable result is excluded from the acceptance
DENOMINATOR. Not counted favourable (that hides a stalled loop behind a false
100%), not counted unfavourable (that auto-paused this repo's routines four
times on 2026-07-31: record/settle rollup 94e34b23, fire-time floor 1a4226a9,
pulse settled-definition divergence, no-gate settlement a6c0cc7e).

This is the same doctrine the settlement layer already applies one level down —
``produce_gate_evidence`` returns ``status="unavailable"`` and
``routines_settle`` writes NULL/NULL for evidence-absence — applied at the
loop-acceptance layer.

The counterfeit that must stay caught
-------------------------------------
``tests/counterfeits/corpus.d/acceptance-neutral.toml`` mutates
``loop_jobs.FAVOURABLE_STATUSES`` back to ``{"completed", "parked", "idle"}``
and mutates ``routines_tick._fire`` to settle a neutral run as ``False``. Both
must make nodes in this file RED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.loop_jobs import (
    ADVERSE_STATUSES,
    FAVOURABLE_STATUSES,
    NEUTRAL_STATUSES,
    classify_loop_status,
)
from omniagentos.scheduler.routines import (
    AUTO_PAUSE_MIN_RUNS,
    OUTCOME_ADVERSE,
    OUTCOME_FAVOURABLE,
    OUTCOME_NEUTRAL,
    acceptance_rate,
    classify_run_outcome,
    should_auto_pause,
    should_fire,
)
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
FINISHED_AT = "2026-08-01T09:01:00Z"


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "acceptance_neutral.db")


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


def _routine(routines: RoutinesStore, name: str = "neutral-doctrine") -> dict[str, Any]:
    """A routine capped by BUDGET, not iterations.

    Deliberate: these tests are about the acceptance floor, and the default
    payload's ``max_iterations=5`` cap would stop the routine for a completely
    different (and correct) reason long before the floor had anything to say.
    ``test_neutral_runs_still_count_towards_the_iteration_cap`` covers that cap
    on purpose.
    """
    return routines.create_routine(
        valid_routine_payload(
            name=name,
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "doctrine", "harness": "mock"},
            hard_cap_type="budget_usd",
            hard_cap_value=1000.0,
        )
    )


def _record(routines: RoutinesStore, routine_id: str, iteration: int, **row: Any) -> None:
    # ISSUE-8 (Sol review, seam 1): record_run no longer defaults an omitted
    # cost_usd to a known 0.0 — it reads as genuinely unknown, matching the
    # public API route's own contract. These fixture rows ARE a known,
    # provisional zero (the same shape routines_tick.py always writes at
    # fire time, before settlement); say so explicitly so this file's
    # acceptance-floor tests aren't incidentally exercising the (correct,
    # separately-tested) budget_usd-fails-closed-on-unknown-cost behavior.
    row.setdefault("cost_usd", 0.0)
    routines.record_run(
        routine_id,
        {"iteration": iteration, "finished_at": FINISHED_AT, **row},
    )


def _parked(routines: RoutinesStore, routine_id: str, iteration: int) -> None:
    _record(
        routines,
        routine_id,
        iteration,
        gate_passed=None,
        accepted=None,
        stop_reason="loop_parked_awaiting_human",
        outcome_class=OUTCOME_NEUTRAL,
    )


def _idle(routines: RoutinesStore, routine_id: str, iteration: int) -> None:
    _record(
        routines,
        routine_id,
        iteration,
        gate_passed=None,
        accepted=None,
        stop_reason="loop_idle_no_work",
        outcome_class=OUTCOME_NEUTRAL,
    )


def _blocked(routines: RoutinesStore, routine_id: str, iteration: int) -> None:
    _record(
        routines,
        routine_id,
        iteration,
        gate_passed=False,
        accepted=False,
        stop_reason="loop_blocked",
        outcome_class=OUTCOME_ADVERSE,
    )


def _completed(routines: RoutinesStore, routine_id: str, iteration: int) -> None:
    _record(
        routines,
        routine_id,
        iteration,
        gate_passed=True,
        accepted=True,
        stop_reason="",
        outcome_class=OUTCOME_FAVOURABLE,
    )


# ---------------------------------------------------------------------------
# THE DOCTRINE
# ---------------------------------------------------------------------------


def test_doctrine_a_non_result_is_neither_success_nor_failure() -> None:
    """The one sentence this whole lane exists to make mechanical.

    Stated over the pure primitives so it holds regardless of which layer a
    caller enters at: the three status sets partition cleanly, a non-result
    lands in NEUTRAL, and NEUTRAL moves neither the numerator nor the
    denominator.
    """
    # 1. The sets are a partition — no status is both favourable and adverse,
    #    and none is silently missing.
    assert FAVOURABLE_STATUSES & NEUTRAL_STATUSES == frozenset()
    assert FAVOURABLE_STATUSES & ADVERSE_STATUSES == frozenset()
    assert NEUTRAL_STATUSES & ADVERSE_STATUSES == frozenset()

    # 2. Parking for a human and having nothing to do are BOTH non-results.
    assert NEUTRAL_STATUSES == {"parked", "idle"}
    assert "parked" not in FAVOURABLE_STATUSES, (
        "a loop parking every tick forever would report 100% acceptance and "
        "could never trip the auto-pause floor"
    )
    assert "idle" not in FAVOURABLE_STATUSES
    assert "parked" not in ADVERSE_STATUSES, (
        "parking for a human is the system WORKING; scoring it unfavourable is "
        "the defect that auto-paused four routines on 2026-07-31"
    )
    assert "idle" not in ADVERSE_STATUSES

    # 3. A neutral run changes neither side of the ratio.
    assert acceptance_rate(total_runs=4, accepted_runs=2, neutral_runs=0) == 0.5
    assert acceptance_rate(total_runs=8, accepted_runs=2, neutral_runs=4) == 0.5, (
        "four non-results must leave a 2-of-4 record reading 50%, not 25%"
    )


def test_doctrine_the_taxonomy_maps_every_loop_status() -> None:
    """Every status a worker can emit lands in exactly one class, with a code."""
    assert classify_loop_status("completed") == (OUTCOME_FAVOURABLE, "")
    assert classify_loop_status("parked") == (OUTCOME_NEUTRAL, "loop_parked_awaiting_human")
    assert classify_loop_status("idle") == (OUTCOME_NEUTRAL, "loop_idle_no_work")
    assert classify_loop_status("blocked") == (OUTCOME_ADVERSE, "loop_blocked")
    assert classify_loop_status("aborted") == (OUTCOME_ADVERSE, "loop_aborted")
    assert classify_loop_status("failed") == (OUTCOME_ADVERSE, "loop_failed")
    # Fail CLOSED: an unheard-of status must not become invisible to the floor.
    assert classify_loop_status("who_knows") == (OUTCOME_ADVERSE, "loop_status_unrecognized")


def test_doctrine_parked_idle_and_blocked_are_three_things_not_one() -> None:
    """The distinction lives in the DATA, because operators act on it.

    "Waiting on a human" is chased by chasing the human. "Nothing to do" needs
    no action at all. "Cannot proceed" — a dead credential — is the system's own
    problem and must reach an operator through the floor. Collapsing the third
    into the first two is exactly how a loop with revoked credentials idles
    green forever.
    """
    parked = classify_loop_status("parked")
    idle = classify_loop_status("idle")
    blocked = classify_loop_status("blocked")

    assert parked[1] != idle[1], "parked and idle must be told apart in stop_reason"
    assert parked[0] == idle[0] == OUTCOME_NEUTRAL
    assert blocked[0] == OUTCOME_ADVERSE, (
        "a system-caused inability to proceed is actionable, so it counts"
    )
    assert blocked[1] not in {parked[1], idle[1]}


def test_doctrine_a_pending_run_is_not_a_neutral_run() -> None:
    """``None`` (not judged YET) is a fourth thing, and must stay distinct.

    Conflating "no verdict yet" with "no verdict ever" is how the persisted
    counters were poisoned before: a fired-but-unsettled run was counted as a
    rejection and three of them paused the routine.
    """
    assert (
        classify_run_outcome(gate_passed=None, accepted=None, stop_reason="", finished_at=None)
        is None
    )
    assert (
        classify_run_outcome(
            gate_passed=None, accepted=None, stop_reason="", finished_at=FINISHED_AT
        )
        == OUTCOME_NEUTRAL
    )


# ---------------------------------------------------------------------------
# A loop that parks / idles forever
# ---------------------------------------------------------------------------


def test_n_consecutive_parked_ticks_do_not_report_full_acceptance(
    routines: RoutinesStore,
) -> None:
    """The live defect, reproduced: 2 parks must NOT read 1.0.

    This is the exact shape of rtn_1e5567b9f3314a2c9d76 on 2026-08-01.
    """
    routine = _routine(routines)
    for iteration in (1, 2):
        _parked(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["total_runs"] == 2, "firings are still counted (max_iterations reads this)"
    assert updated["accepted_runs"] == 0
    assert updated["neutral_runs"] == 2
    assert updated["acceptance_rate"] is None, (
        "an empty acceptance denominator is UNKNOWN. 1.0 hides a stalled loop; "
        "0.0 is a lie that auto-pauses a loop for behaving correctly"
    )


@pytest.mark.parametrize("record", [_parked, _idle], ids=["parked", "idle"])
def test_a_loop_that_never_produces_a_result_is_never_auto_paused(
    routines: RoutinesStore,
    record: Any,
) -> None:
    """Ten consecutive non-results: still active, still firing, still unknown."""
    routine = _routine(routines)
    for iteration in range(1, 11):
        record(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "active", updated["auto_pause_reason"]
    assert updated["acceptance_rate"] is None
    assert updated["neutral_runs"] == 10

    # ...and the fire-time floor agrees, on BOTH paths: with settled counts
    # supplied (production) and off the persisted counters alone (the fallback).
    fire, reason = should_fire(updated, now=NOW, settled_runs=0, settled_accepted=0)
    assert fire is True, reason
    fire, reason = should_fire(updated, now=NOW)
    assert fire is True, (
        f"the persisted-counter fallback punished a routine for non-results: {reason}"
    )


def test_a_loop_that_parks_forever_remains_auto_pausable(routines: RoutinesStore) -> None:
    """Neutrality must not become IMMUNITY.

    A routine whose whole history is non-results has an empty denominator, so it
    cannot be paused — correct. But the moment it produces real failures it must
    pause on THOSE, with the parks left out of the maths entirely. A permanent
    non-result that also confers permanent auto-pause immunity is the failure
    mode recorded on 2026-08-01 (gate-execution checkout, round 3).
    """
    routine = _routine(routines)
    for iteration in range(1, 21):
        _parked(routines, routine["id"], iteration)
    assert routines.get_routine(routine["id"])["status"] == "active"  # type: ignore[index]

    for offset in range(AUTO_PAUSE_MIN_RUNS):
        _record(
            routines,
            routine["id"],
            21 + offset,
            gate_passed=False,
            accepted=False,
            stop_reason="builtin_failed",
        )

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "auto_paused", (
        "20 parks must not immunise a routine against 3 real failures"
    )
    assert updated["acceptance_rate"] == 0.0, (
        "with 3 judged runs and 0 accepted, 0% is now a FACT, not an artefact "
        "of counting non-results"
    )


def test_neutral_runs_still_count_towards_the_iteration_cap(routines: RoutinesStore) -> None:
    """Neutrality applies to the ACCEPTANCE denominator, and nowhere else.

    ``total_runs`` is a count of FIRINGS and the ``max_iterations`` hard cap
    reads it. If neutral runs were subtracted there too, a loop that parks every
    tick would fire forever without limit — swapping a false-100% blind spot for
    an unbounded one. The hard cap exists precisely so a broken gate cannot spin
    a routine, and a non-result is not an exemption from it.
    """
    routine = routines.create_routine(
        valid_routine_payload(
            name="iteration-capped",
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "doctrine", "harness": "mock"},
            hard_cap_type="max_iterations",
            hard_cap_value=5.0,
        )
    )
    for iteration in range(1, 6):
        _parked(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["total_runs"] == 5
    assert updated["neutral_runs"] == 5
    fire, reason = should_fire(updated, now=NOW, settled_runs=0, settled_accepted=0)
    assert fire is False
    assert "hard stop-condition" in reason


def test_a_blocked_loop_trips_the_floor(routines: RoutinesStore) -> None:
    """A dead credential does no work — and must NOT be scored like idling.

    ``blocked`` is the whole reason the taxonomy is three-valued rather than
    "result / no result": it is a non-result the SYSTEM caused and can act on,
    so it is adverse, and three of them pause the routine and reach a human.
    """
    routine = _routine(routines)
    for iteration in range(1, AUTO_PAUSE_MIN_RUNS + 1):
        _blocked(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "auto_paused", (
        "a persistent authorization failure must reach an operator, not idle green"
    )
    assert updated["neutral_runs"] == 0
    assert updated["acceptance_rate"] == 0.0


def test_a_transient_non_result_does_not_trip_the_floor(routines: RoutinesStore) -> None:
    """W2's requirement, stated as a test: idle is forgiven, blocked is not."""
    routine = _routine(routines)
    for iteration in range(1, 11):
        _idle(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "active", (
        "a transient API error rendered as idle must never pause a poll loop"
    )


# ---------------------------------------------------------------------------
# Mixed history: the denominator has to be RIGHT, not merely non-empty
# ---------------------------------------------------------------------------


def test_a_mixed_history_computes_over_the_judged_runs_only(routines: RoutinesStore) -> None:
    """3 completed, 1 failed, 6 parked → 3/4 = 75%, not 3/10 = 30%."""
    routine = _routine(routines)
    iteration = 0
    for _ in range(3):
        iteration += 1
        _completed(routines, routine["id"], iteration)
    iteration += 1
    _record(
        routines,
        routine["id"],
        iteration,
        gate_passed=False,
        accepted=False,
        stop_reason="builtin_failed",
    )
    for _ in range(6):
        iteration += 1
        _parked(routines, routine["id"], iteration)

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["total_runs"] == 10
    assert updated["accepted_runs"] == 3
    assert updated["neutral_runs"] == 6
    assert updated["acceptance_rate"] == pytest.approx(0.75)
    assert updated["status"] == "active", updated["auto_pause_reason"]


def test_a_mixed_history_still_pauses_when_the_judged_runs_are_bad(
    routines: RoutinesStore,
) -> None:
    """1 completed, 3 failed, 20 parked → 25% over the judged four: pause."""
    routine = _routine(routines)
    iteration = 0
    for _ in range(20):
        iteration += 1
        _parked(routines, routine["id"], iteration)
    iteration += 1
    _completed(routines, routine["id"], iteration)
    for _ in range(3):
        iteration += 1
        _record(
            routines,
            routine["id"],
            iteration,
            gate_passed=False,
            accepted=False,
            stop_reason="builtin_failed",
        )

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["acceptance_rate"] == pytest.approx(0.25)
    assert updated["status"] == "auto_paused"


# ---------------------------------------------------------------------------
# Empty denominator: NULL everywhere, 0% nowhere, ZeroDivisionError nowhere
# ---------------------------------------------------------------------------


def test_an_empty_denominator_is_unknown_not_zero() -> None:
    """The pure contract, so no consumer has to guess."""
    assert acceptance_rate(total_runs=0, accepted_runs=0, neutral_runs=0) is None
    assert acceptance_rate(total_runs=5, accepted_runs=0, neutral_runs=5) is None
    # And nothing divides by it.
    assert should_auto_pause(total_runs=5, accepted_runs=0, neutral_runs=5) is False
    assert should_auto_pause(total_runs=99, accepted_runs=0, neutral_runs=99) is False


def test_cost_per_accepted_change_degrades_to_null_never_crashes(
    routines: RoutinesStore,
) -> None:
    """Path D from the cross-lineage verification: no division by zero.

    A routine that spends money and produces only non-results has no
    cost-per-accepted-change to report. NULL is the honest answer; a crash in
    ``record_run`` would take the whole tick down, and 0 would read as free.
    """
    routine = _routine(routines)
    for iteration in range(1, 6):
        routines.record_run(
            routine["id"],
            {
                "iteration": iteration,
                "gate_passed": None,
                "accepted": None,
                "cost_usd": 2.0,
                "stop_reason": "loop_parked_awaiting_human",
                "outcome_class": OUTCOME_NEUTRAL,
                "finished_at": FINISHED_AT,
            },
        )

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["total_cost_usd"] == pytest.approx(10.0)
    assert updated["cost_per_accepted_change"] is None
    assert updated["acceptance_rate"] is None


def test_the_settlement_path_counts_a_null_settlement_as_neutral(
    routines: RoutinesStore,
) -> None:
    """The gate-workspace lane's NULL/NULL semantics, read but not rewritten.

    ``routines_settle`` writes gate_passed=NULL/accepted=NULL when evidence is
    unavailable. That already kept those runs out of the FLOOR; it must also
    keep them out of the persisted acceptance denominator, or the two numbers
    disagree and the dashboard shows a healthy routine that never runs.
    """
    routine = _routine(routines)
    pending = [
        routines.record_run(routine["id"], {"iteration": i, "run_id": f"run-{i}"})
        for i in range(1, 4)
    ]
    for row in pending:
        routines.settle_run(
            row["id"],
            gate_passed=None,
            accepted=None,
            finished_at=FINISHED_AT,
            stop_reason="gate_evidence_unavailable",
        )

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["neutral_runs"] == 3
    assert updated["acceptance_rate"] is None
    assert updated["status"] == "active", updated["auto_pause_reason"]


def test_settling_the_same_run_twice_cannot_double_count_a_neutral(
    routines: RoutinesStore,
) -> None:
    """Idempotence, because the neutral counter is now a rollup too."""
    from omniagentos.scheduler.store import RoutineRunAlreadySettled

    routine = _routine(routines)
    row = routines.record_run(routine["id"], {"iteration": 1, "run_id": "run-1"})
    routines.settle_run(
        row["id"],
        gate_passed=None,
        accepted=None,
        finished_at=FINISHED_AT,
        stop_reason="gate_evidence_unavailable",
    )
    with pytest.raises(RoutineRunAlreadySettled):
        routines.settle_run(
            row["id"],
            gate_passed=None,
            accepted=None,
            finished_at=FINISHED_AT,
            stop_reason="gate_evidence_unavailable",
        )

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["neutral_runs"] == 1


# ---------------------------------------------------------------------------
# Queryability: the operator's question must be answerable in SQL
# ---------------------------------------------------------------------------


def test_the_outcome_and_its_reason_are_durable_and_queryable_per_run(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """ "Which loops are waiting on ME, and which are stuck?" — one query.

    Before the taxonomy this was unanswerable without parsing free text: every
    non-result was a boolean plus a sentence in ``notes``.
    """
    routine = _routine(routines)
    _parked(routines, routine["id"], 1)
    _idle(routines, routine["id"], 2)
    _blocked(routines, routine["id"], 3)
    _completed(routines, routine["id"], 4)

    rows = database._connection.execute(
        "SELECT outcome_class, stop_reason FROM routine_runs "
        "WHERE routine_id = ? ORDER BY iteration",
        (routine["id"],),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("neutral", "loop_parked_awaiting_human"),
        ("neutral", "loop_idle_no_work"),
        ("adverse", "loop_blocked"),
        ("favourable", ""),
    ]

    awaiting_a_human = database._connection.execute(
        "SELECT COUNT(*) FROM routine_runs WHERE routine_id = ? AND stop_reason = ?",
        (routine["id"], "loop_parked_awaiting_human"),
    ).fetchone()[0]
    assert awaiting_a_human == 1


def test_a_caller_cannot_declare_an_unknown_outcome_class(routines: RoutinesStore) -> None:
    """The enum is enforced at the write path (there is no CHECK constraint)."""
    routine = _routine(routines)
    with pytest.raises(ValueError, match="unknown outcome_class"):
        routines.record_run(
            routine["id"],
            {"iteration": 1, "finished_at": FINISHED_AT, "outcome_class": "mostly_fine"},
        )


def test_a_builtin_result_cannot_be_neutral_and_accepted_at_once() -> None:
    """The collapse, refused in the one object that could re-create it."""
    from omniagentos.scheduler.builtin_jobs import BuiltinResult

    with pytest.raises(ValueError, match="never also be accepted"):
        BuiltinResult(accepted=True, notes="", outcome=OUTCOME_NEUTRAL)
