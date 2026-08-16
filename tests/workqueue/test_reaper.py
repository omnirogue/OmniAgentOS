"""The reaper: requeue expired leases, consume nothing.

The reaper is the reason a `kill -9` on one Mac costs the pool ≤120 s instead of
a stalled unit. Two symmetric mistakes are available here and both are silent:

* consuming an EXTRA attempt (it must not — nothing new ran), and
* leaving the claim-time increment standing, which is what shipped. `claim`
  increments `attempt` unconditionally and `record_result` refunds it for
  'abandoned'; a reaper that skipped the refund burned one budget unit per dead
  worker until the unit sat at attempt == max_attempts in state 'queued' —
  which `claim`'s own `attempt < max_attempts` filter excludes forever. Idle,
  unclaimable, and indistinguishable from healthy backlog.

So the contract is: the reaper refunds exactly what the abandoned execution
consumed, and nothing else moves.
"""

from __future__ import annotations

from omniagentos.workqueue.schema import LEASE_TTL_S
from tests.workqueue.conftest import at, submit


def test_expired_lease_is_requeued_and_the_attempt_row_is_abandoned(store):
    unit_id, _ = store.enqueue(submit("reap"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    assert claimed is not None
    before = store.get_unit(unit_id)
    assert before["attempt"] == 1

    assert store.reap_expired(now=at(LEASE_TTL_S - 1)) == 0, "a live lease is never reclaimed"
    assert store.reap_expired(now=at(LEASE_TTL_S + 1)) == 1

    unit = store.get_unit(unit_id)
    assert unit["state"] == "queued"
    assert unit["lease_owner"] is None
    assert unit["lease_expires_at"] is None
    # The abandoned execution produced no verdict about the candidate, so its
    # claim-time increment is given back — exactly as record_result('abandoned')
    # does. instrument_retries is NOT touched: abandoned refunds attempt only.
    assert unit["attempt"] == 0
    assert unit["instrument_retries"] == 0
    # The generation is NOT rolled back: the previous holder must stay fenced out.
    assert unit["lease_generation"] == claimed["lease_generation"]

    attempts = store.list_attempts(unit_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "abandoned"
    assert attempts[0]["finished_at"] == at(LEASE_TTL_S + 1)

    assert store.reap_expired(now=at(LEASE_TTL_S + 2)) == 0, "reaping is idempotent"


def test_reaped_unit_is_claimable_by_another_machine(store):
    unit_id, _ = store.enqueue(submit("reap-reclaim"))
    store.claim("mac-studio", "w1", [], now=at(0))
    store.reap_expired(now=at(LEASE_TTL_S + 1))

    second = store.claim("mw0001-owner", "w2", [], now=at(LEASE_TTL_S + 2))
    assert second is not None
    assert second["unit"]["id"] == unit_id
    assert second["attempt"] == 2
    assert store.status(now=at(LEASE_TTL_S + 3))["lease_reclaims_1h"] == 1


def test_reaper_cancels_instead_of_requeueing_a_cancelled_unit(store):
    unit_id, _ = store.enqueue(submit("reap-cancel"))
    store.claim("mac-studio", "w1", [], now=at(0))
    store.cancel(unit_id)

    assert store.reap_expired(now=at(LEASE_TTL_S + 1)) == 1
    unit = store.get_unit(unit_id)
    # Requeueing it would leave a unit that claim() can never pick up: idle
    # backlog that reads as healthy is the worst possible reading (§6).
    assert unit["state"] == "cancelled"
    assert unit["terminal_reason"] == "cancelled"


def test_repeated_reaps_never_strand_a_unit_below_its_attempt_cap(store):
    """N dead workers in a row must leave the unit exactly as claimable as it was.

    This is the leak in its live shape: `max_attempts` reaps used to walk
    `attempt` up to the cap with nothing to show for it, and the unit then read
    as 'queued' forever while `claim` silently refused it. The assertion that
    matters is not "attempt is small" but "the pool can still run this work".
    """
    unit_id, _ = store.enqueue(submit("reap-budget", max_attempts=2))
    max_attempts = store.get_unit(unit_id)["max_attempts"]

    for cycle in range(max_attempts + 3):  # three MORE reaps than the budget
        window = cycle * (LEASE_TTL_S + 10)
        claimed = store.claim("mac-studio", f"w{cycle}", [], now=at(window))
        assert claimed is not None, f"reap {cycle}: the unit stopped being claimable"
        assert store.get_unit(unit_id)["attempt"] == 1, "the claim itself consumes one"
        assert store.reap_expired(now=at(window + LEASE_TTL_S + 1)) == 1
        unit = store.get_unit(unit_id)
        assert unit["state"] == "queued"
        assert unit["attempt"] == 0, f"reap {cycle} leaked a budget unit"
        assert unit["instrument_retries"] == 0

    # ...and a real verdict still lands normally afterwards: the refunds did not
    # make the unit immortal, they only stopped charging it for dead workers.
    for cycle in range(max_attempts):
        window = 100_000 + cycle * 1000
        claimed = store.claim("mac-studio", "wz", [], now=at(window))
        assert claimed is not None
        store.record_result(
            unit_id,
            "mac-studio:wz",
            claimed["lease_generation"],
            "candidate-defect",
            exit_code=1,
            remedy="the test it added fails",
            now=at(window + 10),
        )
    unit = store.get_unit(unit_id)
    assert (unit["state"], unit["terminal_reason"]) == ("parked", "attempts-exhausted")
    assert unit["attempt"] == max_attempts


def test_a_worker_that_beats_the_reaper_to_an_expired_lease_refunds_too(store):
    """`claim` reclaims expired leases directly, so it carries the same refund.

    The reaper runs every 60 s; a claiming worker can reach an expired lease
    first. If only one of the two paths refunded, the leak would simply move to
    whichever path won the race — the same zombie, harder to see.
    """
    unit_id, _ = store.enqueue(submit("reap-race", max_attempts=2))
    store.claim("mac-studio", "w1", [], now=at(0))

    # No reap in between: the second claim IS the reclaim.
    second = store.claim("mw0001-owner", "w2", [], now=at(LEASE_TTL_S + 1))
    assert second is not None
    unit = store.get_unit(unit_id)
    assert unit["attempt"] == 1, "the reclaim refunds the abandoned claim and spends its own"
    assert second["attempt"] == 2, "the EXECUTION ordinal still advances (it is not the budget)"
    assert [row["outcome"] for row in store.list_attempts(unit_id)] == ["abandoned", None]

    third = store.claim("mw0002", "w3", [], now=at(2 * LEASE_TTL_S + 2))
    assert third is not None, "a unit reclaimed twice is still claimable"
    assert store.get_unit(unit_id)["attempt"] == 1
    assert store.status(now=at(2 * LEASE_TTL_S + 3))["double_executions"] == 0


def test_cancel_of_a_queued_unit_is_immediate(store):
    unit_id, _ = store.enqueue(submit("cancel-queued"))
    store.cancel(unit_id)
    unit = store.get_unit(unit_id)
    assert unit["state"] == "cancelled"
    assert unit["cancel_requested"] == 1
    assert store.claim("mac-studio", "w1", []) is None


def test_cancel_of_a_parked_unit_is_immediate_too(store):
    """A parked unit is not running, so a cancel takes effect at once.

    Leaving it at (state='parked', cancel_requested=1) was the first half of a
    zombie: the next unpark forced 'queued' without clearing the flag, and claim
    filters cancel_requested=0.
    """
    unit_id, _ = store.enqueue(submit("cancel-parked"))
    store.park(unit_id, "storm-parked", "land the exemption on main first")

    store.cancel(unit_id)
    unit = store.get_unit(unit_id)
    assert unit["state"] == "cancelled"
    assert unit["terminal_reason"] == "cancelled"
    assert unit["finished_at"] is not None
    assert store.claim("mac-studio", "w1", []) is None


def test_unpark_of_a_cancelled_unit_cancels_instead_of_queueing_it(store):
    """The human amnesty does not resurrect cancelled work.

    'queued' with cancel_requested=1 is not backlog, it is a hole: claim can
    never take it and status counts it as work waiting. Terminating is the only
    honest answer, and the refusal ledger keeps its memory because this unit is
    not going to re-run the input it was refused for.
    """
    unit_id, _ = store.enqueue(submit("unpark-cancelled"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.refusal_record("key-unpark-cancelled", "raw", "candidate-defect", 0, "the test fails")
    # Cancelled IN FLIGHT: the lease is not broken (§3.4), so the unit reaches
    # its soft park still carrying the flag. This is how a parked unit acquires
    # an outstanding cancel in the real loop.
    store.cancel(unit_id)
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "unchanged-retry",
        retryable=0,
        remedy="nothing changed",
        input_key="key-unpark-cancelled",
        now=at(10),
    )
    parked = store.get_unit(unit_id)
    assert (parked["state"], parked["cancel_requested"]) == ("parked", 1)

    store.unpark(unit_id, because="I fixed the fixture on main")

    unit = store.get_unit(unit_id)
    assert unit["state"] == "cancelled", "an unpark must never produce a queued zombie"
    assert unit["terminal_reason"] == "cancelled"
    assert store.claim("mac-studio", "w1", [], now=at(100)) is None
    assert store.refusal_check("key-unpark-cancelled", "raw") is not None, (
        "the amnesty is a statement about an input that is about to be re-run; "
        "this unit will not run, so the storm detector keeps its count"
    )


def test_requeue_of_a_cancelled_soft_park_cancels_instead_of_queueing_it(store):
    unit_id, _ = store.enqueue(submit("requeue-cancelled"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.cancel(unit_id)
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "unchanged-retry",
        retryable=0,
        remedy="nothing changed",
        now=at(10),
    )
    assert store.get_unit(unit_id)["state"] == "parked"

    store.requeue(unit_id)
    unit = store.get_unit(unit_id)
    assert unit["state"] == "cancelled"
    assert unit["terminal_reason"] == "cancelled"
    assert store.claim("mac-studio", "w1", [], now=at(100)) is None


def test_no_path_leaves_a_queued_unit_that_claim_refuses(store):
    """The invariant behind all three: queued ⇒ claimable (modulo not_before).

    Enumerated over the state-changing verbs rather than asserted about one of
    them, because the defect was a PAIR of verbs that were each defensible alone.
    """
    for index, action in enumerate(("unpark", "requeue", "reap", "record-result")):
        unit_id, _ = store.enqueue(submit(f"invariant-{action}"))
        window = index * 100_000
        claimed = store.claim("mac-studio", "w1", [], now=at(window))
        store.cancel(unit_id)  # in flight: the lease is left alone (§3.4)
        if action == "reap":
            store.reap_expired(now=at(window + LEASE_TTL_S + 1))
        else:
            store.record_result(
                unit_id,
                "mac-studio:w1",
                claimed["lease_generation"],
                "unchanged-retry" if action in ("unpark", "requeue") else "lease-lost",
                retryable=0,
                remedy="nothing changed",
                now=at(window + 10),
            )
            if action == "unpark":
                store.unpark(unit_id, because="tried to revive it")
            elif action == "requeue":
                store.requeue(unit_id)

        unit = store.get_unit(unit_id)
        assert unit["state"] != "queued" or unit["cancel_requested"] == 0, (
            f"{action} left {unit_id} queued-but-unclaimable: "
            "idle backlog that reads as healthy is the worst possible reading (§6)"
        )
