"""The grading policy of an unattended loop, pinned as a truth table.

:mod:`selfloop.outcome` is pure, so these tests need no adapters and no context —
which is exactly why the module is worth having: the entire question "is this
loop allowed to call itself successful?" is answerable by reading one file and
this one.

Two rules are enforced here and neither is negotiable elsewhere in the package:

* **A gate may LOWER the loop's claim and may never RAISE it.** Every row of
  :func:`test_compose_truth_table` is one cell of that rule.
* **A non-result leaves the sample entirely.** Not a zero in the numerator, not
  a weighting: the observation is removed from the numerator AND the denominator,
  and when nothing is left the floor reports ``meets=None`` rather than guessing.
  Both ways of collapsing that third value have shipped as production incidents,
  and the docstrings in the module under test name them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from selfloop.adapters.memory import failing_receipt, passing_receipt
from selfloop.contracts import GateReceipt, LoopStatus
from selfloop.ledger import OutcomeRecord
from selfloop.outcome import (
    AcceptanceFloor,
    Settlement,
    acceptance_floor,
    artifact_bytes,
    classify_settlement,
    compose,
    counts_toward_acceptance_floor,
    settlement_of,
)

#: A receipt that EXITED ZERO having tested nothing. Constructed here rather than
#: taken from the adapters' helpers, because ``passing_receipt()`` refuses to
#: mint one — which is the correct behaviour for a test helper and would defeat
#: the point of the rows below, whose whole job is to prove that composition
#: refuses a vacuous pass even when something upstream handed it one.
VACUOUS = GateReceipt(passed=True, checks_collected=0, detail="collected 0 items")


# ---------------------------------------------------------------------------
# compose: the may-lower-never-raise truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("self_reported", "gate", "expected_gate_passed", "expected_class"),
    [
        # A favourable claim is the only one a gate can act on, in both directions.
        (LoopStatus.COMPLETED, passing_receipt(), True, "favourable"),
        (LoopStatus.COMPLETED, failing_receipt(), False, "adverse"),
        (LoopStatus.COMPLETED, None, None, "neutral"),
        (LoopStatus.COMPLETED, VACUOUS, None, "neutral"),
        # Neutral stays neutral however green the gate is. An idle tick with a
        # passing gate is still an idle tick: the gate graded a workspace, not a
        # result the loop produced.
        (LoopStatus.IDLE, passing_receipt(), True, "neutral"),
        (LoopStatus.IDLE, failing_receipt(), False, "neutral"),
        (LoopStatus.IDLE, None, None, "neutral"),
        (LoopStatus.PARKED, passing_receipt(), True, "neutral"),
        (LoopStatus.PARKED, None, None, "neutral"),
        # Adverse short-circuits. A tick that crashed is adverse whatever any gate
        # says, and the gate's ruling is still recorded as a fact about the gate.
        (LoopStatus.FAILED, passing_receipt(), True, "adverse"),
        (LoopStatus.FAILED, failing_receipt(), False, "adverse"),
        (LoopStatus.FAILED, None, None, "adverse"),
        (LoopStatus.ABORTED, passing_receipt(), True, "adverse"),
        (LoopStatus.BLOCKED, passing_receipt(), True, "adverse"),
        (LoopStatus.BLOCKED, None, None, "adverse"),
        # A status the package cannot classify is adverse, never neutral. A loop
        # whose vocabulary has drifted must trip the floor, not leave the
        # denominator.
        ("banana", passing_receipt(), True, "adverse"),
        ("", None, None, "adverse"),
    ],
)
def test_compose_truth_table(
    self_reported: LoopStatus | str,
    gate: GateReceipt | None,
    expected_gate_passed: bool | None,
    expected_class: str,
) -> None:
    record = compose(self_reported, gate, run_id="r1", instance_id="t1", template="demo")
    assert record.gate_passed is expected_gate_passed
    assert record.outcome_class == expected_class


def test_acceptance_requires_both_a_claim_and_a_corroboration() -> None:
    """``accepted`` is derived, so no row can say accepted with a NULL gate."""
    accepted = compose(LoopStatus.COMPLETED, passing_receipt(), run_id="r1")
    uncorroborated = compose(LoopStatus.COMPLETED, None, run_id="r2")
    contradicted = compose(LoopStatus.COMPLETED, failing_receipt(), run_id="r3")

    assert accepted.accepted is True
    assert accepted.corroborated is True
    assert uncorroborated.accepted is False
    assert uncorroborated.corroborated is False
    assert contradicted.accepted is False
    assert contradicted.corroborated is True


def test_a_vacuous_gate_is_recorded_as_absence_and_says_so() -> None:
    """Zero checks lands in the ``None`` column beside "no gate configured".

    The reason it is not simply dropped: an operator who suspects a gate has
    quietly stopped testing greps for ``gate_passed IS NULL`` next to
    ``checks_collected = 0``, and both halves have to be on the row for that to
    find anything.
    """
    record = compose(LoopStatus.COMPLETED, VACUOUS, run_id="r1")
    assert record.gate_passed is None
    assert record.gate_unavailable_reason == "vacuous_gate"
    assert record.checks_collected == 0
    assert record.gate_detail == "collected 0 items"


def test_no_gate_is_recorded_as_absence_with_its_own_reason() -> None:
    record = compose(LoopStatus.COMPLETED, None, run_id="r1")
    assert record.gate_unavailable_reason == "no_gate"


def test_a_supplied_unavailability_reason_survives_composition() -> None:
    """A ``GateUnavailable.reason`` is what an operator reads. Do not overwrite it."""
    record = compose(
        LoopStatus.COMPLETED,
        None,
        run_id="r1",
        gate_unavailable_reason="pytest_not_installed",
    )
    assert record.gate_unavailable_reason == "pytest_not_installed"


def test_a_ruling_gate_leaves_no_unavailability_reason() -> None:
    """The column means "why did the gate not rule" and must be empty when it did."""
    record = compose(
        LoopStatus.COMPLETED,
        passing_receipt(),
        run_id="r1",
        gate_unavailable_reason="stale value from a caller",
    )
    assert record.gate_unavailable_reason == ""


def test_compose_carries_the_learning_columns_through_untouched() -> None:
    record = compose(
        LoopStatus.FAILED,
        failing_receipt(detail="1 failed"),
        run_id="r9",
        instance_id="t1",
        template="demo",
        at="2026-01-01T00:00:00+00:00",
        scope="deliver",
        failure_tag="timeout",
        detail="send timed out",
    )
    assert (record.scope, record.failure_tag) == ("deliver", "timeout")
    assert record.self_reported_status == LoopStatus.FAILED.value
    assert record.id == "r9"  # one run, one report card


# ---------------------------------------------------------------------------
# settlement_of: the bridge between the two three-valued vocabularies
# ---------------------------------------------------------------------------


def test_settlement_of_projects_the_composed_outcome() -> None:
    assert settlement_of(compose(LoopStatus.COMPLETED, passing_receipt())) is Settlement.OK
    assert settlement_of(compose(LoopStatus.COMPLETED, failing_receipt())) is Settlement.FAILED
    assert settlement_of(compose(LoopStatus.COMPLETED, None)) is Settlement.UNGATEABLE
    assert settlement_of(compose(LoopStatus.IDLE, passing_receipt())) is Settlement.UNGATEABLE
    assert settlement_of(compose(LoopStatus.PARKED, None)) is Settlement.UNGATEABLE
    assert settlement_of(compose(LoopStatus.FAILED, None)) is Settlement.FAILED


def _row(outcome_class: str, gate_passed: bool | None, run_id: str = "r") -> OutcomeRecord:
    """An OutcomeRecord written by something other than :func:`compose`."""
    return OutcomeRecord(
        id=run_id,
        run_id=run_id,
        instance_id="t1",
        template="demo",
        at="",
        self_reported_status=LoopStatus.COMPLETED.value,
        gate_passed=gate_passed,
        outcome_class=outcome_class,
    )


def test_an_uncorroborated_favourable_row_is_not_a_pass() -> None:
    """``compose`` cannot produce this shape. Something else can, and it still fails.

    A rule that decides whether an unattended loop grades itself honestly should
    not have exactly one enforcement point.
    """
    assert settlement_of(_row("favourable", None)) is Settlement.UNGATEABLE


def test_an_unclassifiable_row_settles_failed_rather_than_leaving_the_sample() -> None:
    """Fails closed, exactly as ``outcome_class`` does on an unknown status."""
    assert settlement_of(_row("splendid", True)) is Settlement.FAILED


# ---------------------------------------------------------------------------
# acceptance_floor
# ---------------------------------------------------------------------------


def _accepted(run_id: str) -> OutcomeRecord:
    return compose(LoopStatus.COMPLETED, passing_receipt(), run_id=run_id)


def _rejected(run_id: str) -> OutcomeRecord:
    return compose(LoopStatus.COMPLETED, failing_receipt(), run_id=run_id)


def _neutral(run_id: str) -> OutcomeRecord:
    return compose(LoopStatus.IDLE, None, run_id=run_id)


def test_neutral_leaves_both_the_numerator_and_the_denominator() -> None:
    """Five idle ticks neither help nor hurt three real successes.

    Counted as failures, this window auto-pauses a loop that had nothing to do —
    which is the incident that paused four production routines in one night.
    Counted as successes, a loop that parks every tick reports 100% acceptance
    while healing nothing, and that number becomes its own training signal.
    """
    window = [_accepted("a1"), _neutral("n1"), _accepted("a2"), _neutral("n2"), _accepted("a3")]
    window += [_neutral(f"n{i}") for i in (3, 4, 5)]

    floor = acceptance_floor(window)

    assert floor.ok == 3
    assert floor.failed == 0
    assert floor.ungateable == 5
    assert floor.gateable == 3
    assert floor.ratio == pytest.approx(1.0)
    assert floor.meets is True
    assert floor.considered == 8


def test_nothing_gateable_reports_meets_none_rather_than_a_verdict() -> None:
    """"I cannot tell" is a first-class answer, and the only honest one here.

    Reported as a pass, a loop that has verified nothing for a week looks
    healthy. Reported as a failure, it pauses itself for having had nothing to
    do. Both are lies about the same absence.
    """
    floor = acceptance_floor([_neutral(f"n{i}") for i in range(5)])

    assert floor.meets is None
    assert floor.ratio is None
    assert floor.gateable == 0
    assert floor.ungateable == 5


def test_an_empty_ledger_is_undecidable_too() -> None:
    floor = acceptance_floor([])
    assert (floor.meets, floor.ratio, floor.considered) == (None, None, 0)


def test_one_failure_trips_the_default_floor() -> None:
    """The default of 1.0 is the strict end: every gradeable run must be accepted."""
    floor = acceptance_floor([_accepted("a1"), _accepted("a2"), _accepted("a3"), _rejected("f1")])

    assert floor.ratio == pytest.approx(0.75)
    assert floor.meets is False
    assert acceptance_floor(
        [_accepted("a1"), _accepted("a2"), _accepted("a3"), _rejected("f1")], floor=0.7
    ).meets is True


def test_the_window_takes_the_NEWEST_records() -> None:
    """*records* are oldest-to-newest; a recovered loop must be able to recover.

    Ordering is the caller's obligation because sorting here by the record stamp
    would silently reintroduce the wall-clock dependency the event cursor exists
    to remove.
    """
    ledger = [_rejected(f"f{i}") for i in range(5)] + [_accepted(f"a{i}") for i in range(20)]

    assert acceptance_floor(ledger, window=20).meets is True
    assert acceptance_floor(ledger, window=0).meets is False  # window <= 0 considers everything
    assert acceptance_floor(ledger, window=0).considered == 25


def test_the_floor_serialises_every_column_it_decided_from() -> None:
    floor = acceptance_floor([_accepted("a1"), _rejected("f1"), _neutral("n1")], floor=0.5)
    assert floor.as_dict() == {
        "floor": 0.5,
        "ok": 1,
        "failed": 1,
        "ungateable": 1,
        "gateable": 2,
        "ratio": pytest.approx(0.5),
        "meets": True,
        "considered": 3,
    }
    assert isinstance(floor, AcceptanceFloor)


@pytest.mark.parametrize(
    ("settlement", "counts"),
    [
        (Settlement.OK, True),
        (Settlement.FAILED, True),
        (Settlement.UNGATEABLE, False),
        ("ok", True),
        ("ungateable", False),
        ("something else", False),
        (None, False),
    ],
)
def test_counts_toward_acceptance_floor(settlement: Settlement | str | None, counts: bool) -> None:
    """An unrecorded settlement is excluded, never assumed."""
    assert counts_toward_acceptance_floor(settlement) is counts


# ---------------------------------------------------------------------------
# classify_settlement and artifact_bytes: absence is never the best outcome
# ---------------------------------------------------------------------------


def test_a_zero_byte_artifact_is_a_failure_not_a_success(tmp_path: Path) -> None:
    """A zero-byte file is evidence a writer ran, never evidence it produced."""
    empty = tmp_path / "out.json"
    empty.write_text("")
    assert artifact_bytes(empty) == 0
    assert classify_settlement(empty) is Settlement.FAILED


def test_a_non_empty_artifact_settles_ok(tmp_path: Path) -> None:
    written = tmp_path / "out.json"
    written.write_text('{"rows": 3}')
    assert artifact_bytes(written) == 11
    assert classify_settlement(written) is Settlement.OK


def test_a_missing_artifact_is_failed_when_required_and_ungateable_when_not(
    tmp_path: Path,
) -> None:
    """``required=True`` is the default, and the change from the source system.

    Naming an artifact in the call is a claim that the stage owed you the file.
    Under the old default a caller who forgot the keyword got their missing
    artifact quietly excluded from the floor rather than counted against it —
    the same shape of hole as a vacuous gate.
    """
    missing = tmp_path / "never-written.json"
    assert artifact_bytes(missing) is None
    assert classify_settlement(missing) is Settlement.FAILED
    assert classify_settlement(missing, required=False) is Settlement.UNGATEABLE


def test_a_directory_and_a_broken_symlink_are_not_artifacts(tmp_path: Path) -> None:
    """``artifact_bytes`` never raises: a failed stat is an absence of information."""
    broken = tmp_path / "dangling"
    broken.symlink_to(tmp_path / "nothing-here")
    assert artifact_bytes(tmp_path) is None
    assert artifact_bytes(broken) is None
    assert artifact_bytes(object()) is None  # type: ignore[arg-type]
    assert artifact_bytes(None) is None


def test_a_raised_error_outranks_a_written_file(tmp_path: Path) -> None:
    """A stage that raised is not judged on a file it wrote before it raised."""
    written = tmp_path / "out.json"
    written.write_text("partial")
    assert classify_settlement(written, error=RuntimeError("boom")) is Settlement.FAILED
    assert classify_settlement(written, error="boom") is Settlement.FAILED
    # A whitespace-only message is not an error; it is a caller passing nothing.
    assert classify_settlement(written, error="   ") is Settlement.OK


def test_unknown_evidence_can_never_settle_ok() -> None:
    """The fail-closed hinge: absence of a verdict is never the best outcome."""
    assert classify_settlement(evidence=None) is Settlement.FAILED
    assert classify_settlement(evidence=None, required=False) is Settlement.UNGATEABLE
    assert classify_settlement(evidence=False) is Settlement.FAILED
    assert classify_settlement(evidence=True) is Settlement.OK


def test_negative_evidence_outranks_mere_existence(tmp_path: Path) -> None:
    """The file is there and something that looked closer says it does not count."""
    written = tmp_path / "out.json"
    written.write_text("something")
    assert classify_settlement(written, evidence=False) is Settlement.UNGATEABLE
    assert classify_settlement(written, evidence=True) is Settlement.OK


def test_min_bytes_is_honoured(tmp_path: Path) -> None:
    written = tmp_path / "out.json"
    written.write_text("xy")
    assert classify_settlement(written, min_bytes=2) is Settlement.OK
    assert classify_settlement(written, min_bytes=3) is Settlement.FAILED
