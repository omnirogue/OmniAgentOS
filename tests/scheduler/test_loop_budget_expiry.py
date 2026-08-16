"""A leaked reservation must not become free budget when its TTL runs out.

Reserve-before-call is the whole spend cap: the money is held before the paid
call and released only when it can be shown the call did not happen. A hard
kill between the reserve and the settle — or a ``settle()`` that itself fails —
leaves the hold open with nobody to close it.

Until this lane the TTL sweep flipped those holds to ``expired`` and ``expired``
counted as NEITHER settled spend NOR outstanding hold: a $5 reservation, 901
seconds later, was $0.00 of spend. Everything else in this subsystem fails
closed; that one failed open, and the failure mode is a loop that crashes
mid-effect every tick getting its full cap back every 15 minutes.

An expiry is not evidence that a call did not happen. It is the absence of
evidence either way, so it is charged at the reserved maximum with
``cost_quality='unknown'`` — and alarmed, because charging silently would hide
the crash loop that caused it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.scheduler.loop_budget import (
    STATE_EXPIRED_UNKNOWN,
    STATE_OPEN,
    BudgetError,
    BudgetRefused,
    LoopBudgetLedger,
)


class _Clock:
    """A hand-wound clock. ``advance`` is the crash window, in seconds."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def alarms() -> list[tuple[str, str, str, float]]:
    return []


@pytest.fixture
def ledger(
    tmp_path: Path, clock: _Clock, alarms: list[tuple[str, str, str, float]]
) -> LoopBudgetLedger:
    return LoopBudgetLedger(
        str(tmp_path / "budget.sqlite3"),
        instance_caps={"crasher": 10.0},
        clock=clock,
        ttl_seconds=900.0,
        notifier=lambda *args: alarms.append(args),  # type: ignore[arg-type]
    )


def _reserve(ledger: LoopBudgetLedger, usd: float, instance: str = "crasher") -> Any:
    return ledger.reserve(
        instance_id=instance, capability_id="model.complete", estimated_max_usd=usd
    )


def test_a_leaked_reservation_does_not_become_free_budget(
    ledger: LoopBudgetLedger, clock: _Clock
) -> None:
    """Kimi's live proof, inverted: reserve $5, wait out the TTL, ask again.

    On main the second reserve was admitted against ``settled=0.0``. The $5
    call that MAY have happened counted as nothing.
    """
    _reserve(ledger, 5.0)
    clock.advance(901.0)

    state = ledger.get_instance_state("crasher")
    assert state.settled_usd == pytest.approx(5.0), (
        "the expired hold was dropped from the books; a call that may have "
        "been made and billed is now free budget"
    )
    assert state.outstanding_usd == 0.0
    assert state.committed_usd == pytest.approx(5.0)


def test_the_cap_still_binds_across_a_crash_loop(ledger: LoopBudgetLedger, clock: _Clock) -> None:
    """The failure this closes: cap-per-TTL instead of cap-per-day.

    Two $5 reservations exhaust a $10 cap. If expiry freed the money, a loop
    that dies mid-effect could take $10 every 15 minutes forever — nearly
    $1,000 a day against a $10 cap.
    """
    _reserve(ledger, 5.0)
    clock.advance(901.0)
    _reserve(ledger, 5.0)
    clock.advance(901.0)

    with pytest.raises(BudgetRefused) as refusal:
        _reserve(ledger, 5.0)
    assert refusal.value.reason == "instance_cap_exceeded"


def test_an_expired_reservation_is_charged_at_its_maximum_and_says_it_is_unknown(
    ledger: LoopBudgetLedger, clock: _Clock
) -> None:
    reservation = _reserve(ledger, 5.0)
    clock.advance(901.0)
    assert ledger.reclaim_expired() == [reservation.id]

    charged = ledger.get_reservation(reservation.id)
    assert charged.state == STATE_EXPIRED_UNKNOWN
    assert charged.actual_usd == pytest.approx(5.0)
    assert charged.cost_quality == "unknown", (
        "the charge is a fail-closed assumption, not a measurement, and the books must say so"
    )


def test_the_charge_is_reported_separately_so_an_operator_can_see_it(
    ledger: LoopBudgetLedger, clock: _Clock
) -> None:
    """Conservative accounting that hides itself is just a wrong number."""
    _reserve(ledger, 5.0)
    clock.advance(901.0)
    settled = ledger.settle(_reserve(ledger, 1.0).id, actual_usd=0.25, cost_quality="exact")
    assert settled.actual_usd == pytest.approx(0.25)

    state = ledger.get_instance_state("crasher")
    assert state.settled_usd == pytest.approx(5.25)
    assert state.unaccounted_usd == pytest.approx(5.0)


def test_expiry_alarms(
    ledger: LoopBudgetLedger, clock: _Clock, alarms: list[tuple[str, str, str, float]]
) -> None:
    """Charging is the safe answer; being quiet about it is not.

    A crash loop that silently ate its cap would look exactly like a busy loop.
    """
    _reserve(ledger, 5.0)
    clock.advance(901.0)
    ledger.reclaim_expired()

    assert alarms == [("crasher", "alarm", "reservation_expired_unaccounted", 5.0)]


def test_a_broken_notifier_cannot_undo_the_charge(tmp_path: Path, clock: _Clock) -> None:
    """Operator-supplied code runs after the commit and may not wedge the ledger."""

    def _explode(*args: Any) -> None:
        raise RuntimeError("pager is down")

    ledger = LoopBudgetLedger(
        str(tmp_path / "budget.sqlite3"),
        instance_caps={"crasher": 10.0},
        clock=clock,
        notifier=_explode,
    )
    reservation = _reserve(ledger, 5.0)
    clock.advance(901.0)
    ledger.reclaim_expired()

    assert ledger.get_reservation(reservation.id).state == STATE_EXPIRED_UNKNOWN
    assert ledger.get_instance_state("crasher").settled_usd == pytest.approx(5.0)


def test_the_sweep_is_idempotent(ledger: LoopBudgetLedger, clock: _Clock) -> None:
    """Running it twice must not charge twice."""
    _reserve(ledger, 5.0)
    clock.advance(901.0)
    assert len(ledger.reclaim_expired()) == 1
    assert ledger.reclaim_expired() == []
    assert ledger.get_instance_state("crasher").settled_usd == pytest.approx(5.0)


def test_an_open_reservation_inside_its_ttl_is_untouched(
    ledger: LoopBudgetLedger, clock: _Clock
) -> None:
    reservation = _reserve(ledger, 5.0)
    clock.advance(899.0)
    assert ledger.reclaim_expired() == []

    state = ledger.get_instance_state("crasher")
    assert state.outstanding_usd == pytest.approx(5.0)
    assert state.settled_usd == 0.0
    assert ledger.settle(reservation.id, actual_usd=0.5, cost_quality="exact").actual_usd == 0.5


def test_a_charged_expiry_is_terminal(ledger: LoopBudgetLedger, clock: _Clock) -> None:
    """A late owner is not new evidence about what the provider did.

    Letting a straggler settle (or release) an expired hold would hand back
    money the ledger charged precisely because it could not tell whether the
    effect happened — the fail-open door, reopened one level down.
    """
    reservation = _reserve(ledger, 5.0)
    clock.advance(901.0)
    ledger.reclaim_expired()

    with pytest.raises(BudgetError, match="expired unaccounted"):
        ledger.settle(reservation.id, actual_usd=0.01, cost_quality="exact")
    with pytest.raises(BudgetError, match="expired unaccounted"):
        ledger.release(reservation.id)
    assert ledger.get_instance_state("crasher").settled_usd == pytest.approx(5.0)


def test_reserve_sweeps_before_it_measures(ledger: LoopBudgetLedger, clock: _Clock) -> None:
    """The admission decision reads the same books ``get_instance_state`` reports.

    The sweep runs inside ``reserve`` too, so a caller that never invokes
    ``reclaim_expired`` still cannot spend an expired hold twice.
    """
    _reserve(ledger, 9.0)
    clock.advance(901.0)

    with pytest.raises(BudgetRefused):
        _reserve(ledger, 2.0)
    assert _reserve(ledger, 1.0).max_usd == pytest.approx(1.0)


def test_the_global_ceiling_counts_expired_charges_too(tmp_path: Path, clock: _Clock) -> None:
    ledger = LoopBudgetLedger(
        str(tmp_path / "budget.sqlite3"),
        instance_caps={"a": 100.0, "b": 100.0},
        global_ceiling_usd=10.0,
        clock=clock,
    )
    _reserve(ledger, 6.0, instance="a")
    clock.advance(901.0)

    with pytest.raises(BudgetRefused) as refusal:
        _reserve(ledger, 6.0, instance="b")
    assert refusal.value.reason == "global_ceiling_exceeded"


# --------------------------------------------------------------------------
# The report is a WRITE, and a caller must be able to opt out of that
# --------------------------------------------------------------------------


def test_the_default_report_still_sweeps_and_alarms(
    ledger: LoopBudgetLedger, clock: _Clock, alarms: list[tuple[str, str, str, float]]
) -> None:
    """Unchanged behaviour for the operator report that exists to show expiries."""
    _reserve(ledger, 4.0)
    clock.advance(901.0)

    state = ledger.get_instance_state("crasher")
    assert state.unaccounted_usd == pytest.approx(4.0)
    assert alarms, "the operator report must still surface the expiry loudly"


def test_a_reader_can_ask_for_a_read_that_does_not_write_or_page(
    ledger: LoopBudgetLedger, clock: _Clock, alarms: list[tuple[str, str, str, float]]
) -> None:
    """``sweep=False`` is for the dashboard poller that must not drive billing state.

    ``get_instance_state`` reads like an accessor and, by default, transitions
    reservations and pages a human. Wired into a display that refreshes on a
    timer, the display becomes the thing that decides when money is charged.
    """
    reservation = _reserve(ledger, 4.0)
    clock.advance(901.0)

    state = ledger.get_instance_state("crasher", sweep=False)

    assert not alarms, "a read must not be able to page anyone"
    assert ledger.get_reservation(reservation.id).state == STATE_OPEN, (
        "a read must not transition a reservation"
    )
    # The dollars are all still there, in the bucket that has not been swept yet.
    assert state.outstanding_usd == pytest.approx(4.0)
    assert state.unaccounted_usd == pytest.approx(0.0)


def test_not_sweeping_never_under_counts_the_money(ledger: LoopBudgetLedger, clock: _Clock) -> None:
    """The bucket moves; the total committed does not. That is the whole safety claim."""
    _reserve(ledger, 4.0)
    clock.advance(901.0)

    unswept = ledger.get_instance_state("crasher", sweep=False)
    swept = ledger.get_instance_state("crasher", sweep=True)

    unswept_total = unswept.settled_usd + unswept.outstanding_usd
    swept_total = swept.settled_usd + swept.outstanding_usd
    assert unswept_total == pytest.approx(swept_total) == pytest.approx(4.0), (
        "an unswept read that under-counted would let a caller admit spend the "
        "cap should have refused"
    )


def test_the_cap_still_binds_for_a_caller_that_never_sweeps(
    ledger: LoopBudgetLedger, clock: _Clock
) -> None:
    """``reserve`` sweeps on its own account, so opting out of the read is safe."""
    _reserve(ledger, 9.0)
    clock.advance(901.0)
    ledger.get_instance_state("crasher", sweep=False)

    with pytest.raises(BudgetRefused):
        _reserve(ledger, 2.0)
