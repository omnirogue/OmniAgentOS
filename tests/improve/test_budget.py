"""Tests for the synchronous prepaid budget ledger."""

from __future__ import annotations

import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from omniagentos.improve.budget import (
    DAILY_CEILING_USD,
    POOL_CAPS,
    BudgetLedger,
    BudgetRefused,
    Reservation,
    UnknownCostRefused,
    paid_call,
)


class FakeClock:
    """A mutable fake clock for testing that avoids real time.sleep calls."""

    def __init__(self, initial_time: float = 1700000000.0) -> None:
        self._now = initial_time

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the fake clock by the specified number of seconds."""
        self._now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    """Fixture to provide a fake clock."""
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fixture to provide a unique database path for each test."""
    return tmp_path / "budget_test.db"


@pytest.fixture
def ledger(db_path: Path, fake_clock: FakeClock) -> Generator[BudgetLedger, None, None]:
    """Fixture to provide a BudgetLedger instance."""
    ledger_instance = BudgetLedger(db_path, clock=fake_clock)
    yield ledger_instance
    ledger_instance.close()


def test_pool_caps_sum_to_daily_ceiling() -> None:
    """Test 10: Verify cheap invariant that pool caps sum to the daily ceiling."""
    assert sum(POOL_CAPS.values()) == DAILY_CEILING_USD


def test_call_that_would_breach_pool_cap_is_refused(
    ledger: BudgetLedger, fake_clock: FakeClock
) -> None:
    """Test 1: Reserve up to near a pool's cap, then attempt one more that would breach it.

    Asserts BudgetRefused, checks its reason, and verifies pool_state is unchanged.
    """
    pool = "cheap_judges"
    cap = POOL_CAPS[pool]  # 10.0

    # Reserve 9.0 out of 10.0 -> OK
    res1 = ledger.reserve(pool=pool, estimated_max_usd=9.0, idempotency_key="key1")
    assert res1.state == "open"
    assert ledger.pool_state(pool).committed_usd == 9.0

    # Attempt to reserve 2.0 -> should breach cap of 10.0
    with pytest.raises(BudgetRefused) as exc_info:
        ledger.reserve(pool=pool, estimated_max_usd=2.0, idempotency_key="key2")

    assert exc_info.value.reason == "pool_cap_exceeded"
    assert exc_info.value.pool == pool
    assert exc_info.value.requested_usd == 2.0
    assert exc_info.value.settled_usd == 0.0
    assert exc_info.value.outstanding_usd == 9.0
    assert exc_info.value.cap_usd == cap

    # Verify that the refused attempt wrote nothing to reservations and pool state committed is unchanged
    assert ledger.pool_state(pool).committed_usd == 9.0


def test_daily_ceiling_refuses_even_when_pool_has_room(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """Test 2: Verify that global daily ceiling refuses further admissions even if individual pool cap has room."""
    # Custom caps summing to 200.0, but global ceiling is 100.0
    custom_caps = {"workers": 150.0, "cheap_judges": 50.0}
    custom_ledger = BudgetLedger(
        db_path, pool_caps=custom_caps, ceiling_usd=100.0, clock=fake_clock
    )
    try:
        # Reserve 90.0 in workers (cap is 150.0) -> should succeed
        res = custom_ledger.reserve(pool="workers", estimated_max_usd=90.0, idempotency_key="w1")
        assert res.state == "open"
        assert custom_ledger.pool_state("workers").committed_usd == 90.0

        # Try to reserve 20.0 in cheap_judges (cap is 50.0, so pool has plenty of room,
        # but 90.0 + 20.0 = 110.0 > 100.0 global ceiling) -> should raise BudgetRefused with reason "daily_ceiling_exceeded"
        with pytest.raises(BudgetRefused) as exc_info:
            custom_ledger.reserve(pool="cheap_judges", estimated_max_usd=20.0, idempotency_key="c1")
        assert exc_info.value.reason == "daily_ceiling_exceeded"
        assert exc_info.value.pool == "cheap_judges"
        assert exc_info.value.requested_usd == 20.0
        assert exc_info.value.settled_usd == 0.0
        assert exc_info.value.outstanding_usd == 90.0
        assert exc_info.value.cap_usd == 100.0
    finally:
        custom_ledger.close()


def test_concurrent_reservations_cannot_collectively_overshoot(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """Test 3: Concurrently reserve more than a pool cap allows.

    This driving N=16 threads with a barrier, ensuring they call reserve simultaneously.
    Asserts exact pool cap compliance and that at least one thread was refused.
    """
    ledger_instance = BudgetLedger(db_path, clock=fake_clock)

    pool = "cheap_judges"
    cap = POOL_CAPS[pool]  # 10.0
    N = 16
    # Each thread requests 2.0. Total requested = 32.0, cap is 10.0. Maximum 5 threads should succeed.
    requested_amount = 2.0

    barrier = threading.Barrier(N)
    results_lock = threading.Lock()
    successful_reservations: list[Reservation] = []
    refused_exceptions: list[BudgetRefused] = []
    other_exceptions: list[Exception] = []

    def worker(worker_id: int) -> None:
        # Wait for all threads to be ready to hit reserve at the same instant
        barrier.wait()
        try:
            res = ledger_instance.reserve(
                pool=pool,
                estimated_max_usd=requested_amount,
                idempotency_key=f"worker_{worker_id}",
            )
            with results_lock:
                successful_reservations.append(res)
        except BudgetRefused as e:
            with results_lock:
                refused_exceptions.append(e)
        except Exception as e:
            with results_lock:
                other_exceptions.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ledger_instance.close()

    # Assertions
    assert not other_exceptions, f"Unexpected exceptions: {other_exceptions}"

    # a) Sum of granted max_usd <= pool cap EXACTLY
    total_granted = sum(res.max_usd for res in successful_reservations)
    assert total_granted <= cap

    # b) At least one thread was refused
    assert len(refused_exceptions) > 0
    assert len(successful_reservations) + len(refused_exceptions) == N

    # Each granted reservation is exactly requested_amount (2.0)
    # So with cap 10.0, exactly 5 threads must succeed, and 11 must be refused
    assert len(successful_reservations) == 5
    assert len(refused_exceptions) == 11

    # c) Verify database committed equals sum of granted amounts
    # Let's open a fresh ledger connection to inspect state
    inspect_ledger = BudgetLedger(db_path, clock=fake_clock)
    try:
        p_state = inspect_ledger.pool_state(pool)
        assert p_state.committed_usd == total_granted
        assert p_state.outstanding_usd == total_granted
        assert p_state.settled_usd == 0.0
    finally:
        inspect_ledger.close()


def test_retry_reuses_its_reservation(ledger: BudgetLedger, fake_clock: FakeClock) -> None:
    """Test 4: Same idempotency key twice -> returns same reservation, doesn't double-charge."""
    pool = "cheap_judges"
    key = "idem-key-123"

    res1 = ledger.reserve(pool=pool, estimated_max_usd=3.0, idempotency_key=key)
    p_state1 = ledger.pool_state(pool)
    assert p_state1.committed_usd == 3.0

    # Call again with same key
    res2 = ledger.reserve(pool=pool, estimated_max_usd=3.0, idempotency_key=key)
    assert res1.id == res2.id
    assert res2.attempts == 0  # Still 0 because record_attempt wasn't called on it

    p_state2 = ledger.pool_state(pool)
    assert p_state2.committed_usd == 3.0  # NOT double-counted!

    # Record attempts
    res_with_attempt = ledger.record_attempt(res1.id)
    assert res_with_attempt.attempts == 1

    res_with_attempt_2 = ledger.record_attempt(res1.id)
    assert res_with_attempt_2.attempts == 2

    # Check committed is still 3.0
    assert ledger.pool_state(pool).committed_usd == 3.0

    # Settle the reservation
    settled_res = ledger.settle(res1.id, actual_usd=1.5)
    assert settled_res.state == "settled"
    assert settled_res.actual_usd == 1.5

    # Check committed is now 1.5
    assert ledger.pool_state(pool).committed_usd == 1.5

    # Retry should return the settled reservation
    res3 = ledger.reserve(pool=pool, estimated_max_usd=3.0, idempotency_key=key)
    assert res3.id == res1.id
    assert res3.state == "settled"
    assert res3.actual_usd == 1.5


def test_expired_reservation_is_reclaimed(ledger: BudgetLedger, fake_clock: FakeClock) -> None:
    """Test 5: Reserve, advance fake clock past TTL, assert reclaimed and hold released."""
    pool = "cheap_judges"

    # Reserve 6.0
    res1 = ledger.reserve(pool=pool, estimated_max_usd=6.0, idempotency_key="key1")
    assert res1.state == "open"
    assert ledger.pool_state(pool).committed_usd == 6.0

    # Now try to reserve 5.0 -> would exceed pool cap (6.0 + 5.0 = 11.0 > 10.0)
    with pytest.raises(BudgetRefused):
        ledger.reserve(pool=pool, estimated_max_usd=5.0, idempotency_key="key2")

    # Advance clock past TTL (900 seconds default)
    fake_clock.advance(1000.0)

    # Reclaim expired explicitly or via subsequent reserve
    # If we call reserve, it should automatically reclaim expired reservations inside the transaction
    res2 = ledger.reserve(pool=pool, estimated_max_usd=5.0, idempotency_key="key2")
    assert res2.state == "open"

    # Verify res1 state is now expired
    res1_updated = ledger.get_reservation(res1.id)
    assert res1_updated.state == "expired"

    # Pool state should only reflect res2 (5.0 committed)
    p_state = ledger.pool_state(pool)
    assert p_state.committed_usd == 5.0
    assert p_state.outstanding_usd == 5.0
    assert p_state.settled_usd == 0.0


def test_unknown_cost_fails_closed(ledger: BudgetLedger, fake_clock: FakeClock) -> None:
    """Test 6: Invalid estimated_max_usd (None, NaN, <= 0) raises UnknownCostRefused.

    Also settle with None or usage_available=False charges full reserved max.
    """
    pool = "cheap_judges"

    # None
    with pytest.raises(UnknownCostRefused):
        ledger.reserve(pool=pool, estimated_max_usd=None, idempotency_key="k1")

    # NaN
    with pytest.raises(UnknownCostRefused):
        ledger.reserve(pool=pool, estimated_max_usd=float("nan"), idempotency_key="k2")

    # <= 0
    with pytest.raises(UnknownCostRefused):
        ledger.reserve(pool=pool, estimated_max_usd=0.0, idempotency_key="k3")

    with pytest.raises(UnknownCostRefused):
        ledger.reserve(pool=pool, estimated_max_usd=-1.5, idempotency_key="k4")

    # Check that a valid reservation settles at full max if usage telemetry is missing
    res = ledger.reserve(pool=pool, estimated_max_usd=4.5, idempotency_key="k5")

    # Settle with actual_usd=None
    settled1 = ledger.settle(res.id, actual_usd=None)
    assert settled1.state == "settled"
    assert settled1.actual_usd == 4.5

    # Check with another reservation settled with usage_available=False
    res2 = ledger.reserve(pool=pool, estimated_max_usd=3.0, idempotency_key="k6")
    settled2 = ledger.settle(res2.id, actual_usd=1.0, usage_available=False)
    assert settled2.state == "settled"
    assert settled2.actual_usd == 3.0


def test_negative_actual_usd_fails_closed_to_reserved_max(
    ledger: BudgetLedger, fake_clock: FakeClock
) -> None:
    """Negative usage is invalid telemetry, not free work.

    Counterfeit that would fake a green without fixing the defect class:
    - clamp negative to 0.0 (current bug; under-counts spend vs a $cap)
    - reject only exact -1.0 while still zeroing -0.01 / -6.33
    - raise and leave the reservation open (avoids recording, not fail-closed settle)
    - treat actual_usd=0.0 as invalid (would break legitimate free/zero-cost settles)

    Required: negative → charge reserved max_usd; 0.0 still settles as 0.0.
    """
    pool = "workers"
    reserved = 6.33

    res_neg = ledger.reserve(pool=pool, estimated_max_usd=reserved, idempotency_key="neg-actual")
    settled_neg = ledger.settle(res_neg.id, actual_usd=-1.0)
    assert settled_neg.state == "settled"
    assert settled_neg.actual_usd == reserved, (
        f"negative actual must fail closed to max_usd={reserved}, "
        f"not be recorded as free work; got {settled_neg.actual_usd!r}"
    )
    # Pool accounting must also see the full hold, not 0.0 "free" spend
    assert ledger.pool_state(pool).settled_usd == reserved

    res_small_neg = ledger.reserve(pool=pool, estimated_max_usd=4.0, idempotency_key="neg-small")
    settled_small = ledger.settle(res_small_neg.id, actual_usd=-0.01)
    assert settled_small.actual_usd == 4.0

    res_zero = ledger.reserve(pool=pool, estimated_max_usd=2.0, idempotency_key="zero-ok")
    settled_zero = ledger.settle(res_zero.id, actual_usd=0.0)
    assert settled_zero.actual_usd == 0.0


def test_eighty_percent_notification_fires_once(db_path: Path, fake_clock: FakeClock) -> None:
    """Test 7: Verification that the 80% notification fires exactly once, atomically."""
    notifications: list[tuple[str, str, float]] = []

    def test_notifier(pool: str, kind: str, fraction: float) -> None:
        notifications.append((pool, kind, fraction))

    # Construct ledger with custom caps and ceiling 100.0. 80% threshold is 80.0
    custom_caps = {"workers": 150.0}
    ledger = BudgetLedger(
        db_path, pool_caps=custom_caps, ceiling_usd=100.0, clock=fake_clock, notifier=test_notifier
    )
    try:
        # Reserve 75.0 (75% of global 100.0) -> global is 75%, below 80%. No notification.
        ledger.reserve(pool="workers", estimated_max_usd=75.0, idempotency_key="w1")
        assert len(notifications) == 0

        # Reserve another 10.0 (total 85.0, 85%) -> crosses 80%. Should fire notification once.
        ledger.reserve(pool="workers", estimated_max_usd=10.0, idempotency_key="w2")
        assert len(notifications) == 1
        assert notifications[0][1] == "warning"
        assert notifications[0][2] == 0.85

        # Reserve another 5.0 (total 90.0, 90%) -> already notified, should NOT fire again.
        ledger.reserve(pool="workers", estimated_max_usd=5.0, idempotency_key="w3")
        assert len(notifications) == 1
    finally:
        ledger.close()


def test_auto_pause_at_ceiling(db_path: Path, fake_clock: FakeClock) -> None:
    """Test 8: Once committed hits or exceeds global ceiling, paused is True and further reserve calls raise BudgetRefused."""
    notifications: list[tuple[str, str, float]] = []

    def test_notifier(pool: str, kind: str, fraction: float) -> None:
        notifications.append((pool, kind, fraction))

    custom_caps = {"workers": 150.0, "cheap_judges": 50.0}
    ledger = BudgetLedger(
        db_path, pool_caps=custom_caps, ceiling_usd=100.0, clock=fake_clock, notifier=test_notifier
    )
    try:
        # Fill up to reach 100.0
        ledger.reserve(pool="workers", estimated_max_usd=90.0, idempotency_key="g1")
        assert ledger.paused is False

        # Hit the global ceiling of 100.0
        ledger.reserve(pool="cheap_judges", estimated_max_usd=10.0, idempotency_key="g2")
        assert ledger.paused is True

        # Ensure pause notification was fired
        pause_notifs = [n for n in notifications if n[1] == "pause"]
        assert len(pause_notifs) == 1

        # Attempt further reservation -> raises paused
        with pytest.raises(BudgetRefused) as exc_info:
            ledger.reserve(pool="workers", estimated_max_usd=1.0, idempotency_key="g3")
        assert exc_info.value.reason == "paused"

        # Resume clears the pause state
        ledger.resume()
        assert ledger.paused is False

        # Try to reserve 1.0 in workers again. Since we are at ceiling (100.0/100.0),
        # but workers cap has plenty of room (150.0), this will fail with daily_ceiling_exceeded!
        with pytest.raises(BudgetRefused) as exc_info2:
            ledger.reserve(pool="workers", estimated_max_usd=1.0, idempotency_key="g4")
        assert exc_info2.value.reason == "daily_ceiling_exceeded"

    finally:
        ledger.close()


def test_response_dedup_replays_without_new_reservation(
    ledger: BudgetLedger, fake_clock: FakeClock
) -> None:
    """Test 9: Verify response dedup remembers and replays stored responses."""
    key = "idem-key-dedup"
    response_body = "System response body content"

    assert ledger.replay(key) is None

    ledger.remember_response(key, response_body)
    assert ledger.replay(key) == response_body


def test_context_manager_behavior(ledger: BudgetLedger, fake_clock: FakeClock) -> None:
    """Test helper: Verify paid_call context manager behavior under normal and error flows."""
    pool = "cheap_judges"
    key1 = "ctx-key-1"

    # Case A: Normal flow with clean exit, no explicit settle -> settles at max_usd
    with paid_call(ledger, pool=pool, estimated_max_usd=3.0, idempotency_key=key1) as res:
        assert res.state == "open"
        assert ledger.pool_state(pool).committed_usd == 3.0

    # Clean exit should auto-settle at max_usd
    assert ledger.pool_state(pool).committed_usd == 3.0
    res_final = ledger.get_reservation(res.id)
    assert res_final.state == "settled"
    assert res_final.actual_usd == 3.0

    # Case B: Exception flow, attempts == 0 -> should release the reservation
    key2 = "ctx-key-2"
    res_id_captured = ""
    try:
        with paid_call(ledger, pool=pool, estimated_max_usd=4.0, idempotency_key=key2) as res:
            res_id_captured = res.id
            assert ledger.get_reservation(res.id).attempts == 0
            raise ValueError("Pre-flight failure before any model attempt")
    except ValueError:
        pass

    res_post_err = ledger.get_reservation(res_id_captured)
    assert res_post_err.state == "released"
    assert res_post_err.actual_usd == 0.0

    # Case C: Exception flow, attempts > 0 -> should settle at max_usd
    key3 = "ctx-key-3"
    try:
        with paid_call(ledger, pool=pool, estimated_max_usd=5.0, idempotency_key=key3) as res:
            res_id_captured = res.id
            ledger.record_attempt(res.id)
            assert ledger.get_reservation(res.id).attempts == 1
            raise ValueError("Timeout after network transmission initiated")
    except ValueError:
        pass

    res_post_timeout = ledger.get_reservation(res_id_captured)
    assert res_post_timeout.state == "settled"
    assert res_post_timeout.actual_usd == 5.0


def test_mutable_global_aliasing_prevented(db_path: Path, fake_clock: FakeClock) -> None:
    """Verify that mutating one ledger's pool_caps doesn't affect POOL_CAPS or other ledgers."""
    ledger1 = BudgetLedger(db_path, clock=fake_clock)
    original_val = POOL_CAPS["cheap_judges"]

    # Mutate ledger1's pool_caps
    ledger1.pool_caps["cheap_judges"] = 999.0

    # Assert module-level POOL_CAPS is unaffected
    assert POOL_CAPS["cheap_judges"] == original_val

    # Assert a new ledger instance is unaffected
    ledger2 = BudgetLedger(db_path, clock=fake_clock)
    assert ledger2.pool_caps["cheap_judges"] == original_val

    ledger1.close()
    ledger2.close()


def test_concurrent_reservations_across_separate_connections_cannot_overshoot(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """Test 3b: Concurrently reserve more than a pool cap allows, with each thread using its own connection."""
    # Pre-initialize schema in the main thread to avoid any DDL race conditions
    init_ledger = BudgetLedger(db_path, clock=fake_clock)
    init_ledger.close()

    pool = "cheap_judges"
    cap = POOL_CAPS[pool]  # 10.0
    N = 16
    requested_amount = 2.0

    barrier = threading.Barrier(N)
    results_lock = threading.Lock()
    successful_reservations: list[Reservation] = []
    refused_exceptions: list[BudgetRefused] = []
    other_exceptions: list[Exception] = []

    def worker(worker_id: int) -> None:
        thread_ledger = BudgetLedger(db_path, clock=fake_clock)
        try:
            # Synchronize threads on the barrier
            barrier.wait()
            res = thread_ledger.reserve(
                pool=pool,
                estimated_max_usd=requested_amount,
                idempotency_key=f"worker_{worker_id}",
            )
            with results_lock:
                successful_reservations.append(res)
        except BudgetRefused as e:
            with results_lock:
                refused_exceptions.append(e)
        except Exception as e:
            with results_lock:
                other_exceptions.append(e)
        finally:
            thread_ledger.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assertions
    assert not other_exceptions, f"Unexpected exceptions: {other_exceptions}"

    # a) Sum of granted max_usd <= pool cap EXACTLY
    total_granted = sum(res.max_usd for res in successful_reservations)
    assert total_granted <= cap

    # b) At least one thread was refused
    assert len(refused_exceptions) > 0
    assert len(successful_reservations) + len(refused_exceptions) == N

    # Each granted reservation is exactly requested_amount (2.0)
    # So with cap 10.0, exactly 5 threads must succeed, and 11 must be refused
    assert len(successful_reservations) == 5
    assert len(refused_exceptions) == 11

    # c) Verify database committed equals sum of granted amounts
    inspect_ledger = BudgetLedger(db_path, clock=fake_clock)
    try:
        p_state = inspect_ledger.pool_state(pool)
        assert p_state.committed_usd == total_granted
        assert p_state.outstanding_usd == total_granted
        assert p_state.settled_usd == 0.0
    finally:
        inspect_ledger.close()


# 2023-11-14T00:00:00Z — an exact UTC midnight, used to control day boundaries
# in the UTC day-bucketing tests below. All timestamps are injected via
# FakeClock; nothing depends on the wall clock.
UTC_DAY_START = 1699920000.0
DAY_SECONDS = 86400.0


def test_previous_utc_day_spend_does_not_count_toward_todays_cap(
    db_path: Path,
) -> None:
    """UTC day bucketing: spend settled yesterday is excluded from today's caps."""
    clock = FakeClock(UTC_DAY_START + 3600.0)  # 01:00 UTC on day D
    ledger = BudgetLedger(db_path, pool_caps={"workers": 200.0}, ceiling_usd=200.0, clock=clock)
    try:
        res_day_d = ledger.reserve(pool="workers", estimated_max_usd=150.0, idempotency_key="day-d")
        ledger.settle(res_day_d.id, actual_usd=150.0)
        assert ledger.pool_state("workers").settled_usd == 150.0
        assert ledger.snapshot()["global_committed_usd"] == 150.0

        # Jump to exactly UTC midnight of day D+1: yesterday's 150.0 must
        # vanish from cap/pool/snapshot aggregation while staying in the DB.
        clock.advance(DAY_SECONDS - 3600.0)
        assert clock() == UTC_DAY_START + DAY_SECONDS
        assert ledger.pool_state("workers").committed_usd == 0.0
        assert ledger.snapshot()["global_committed_usd"] == 0.0

        # 150.0 on top of yesterday's 150.0 would breach both the pool cap and
        # the ceiling if yesterday still counted; it must be admitted.
        res_day_d1 = ledger.reserve(
            pool="workers", estimated_max_usd=150.0, idempotency_key="day-d1"
        )
        assert res_day_d1.state == "open"
        # A reservation created exactly at UTC midnight belongs to the new day.
        assert ledger.pool_state("workers").committed_usd == 150.0
        assert ledger.snapshot()["global_committed_usd"] == 150.0

        # Yesterday's reservation is still retrievable, just not counted.
        assert ledger.get_reservation(res_day_d.id).state == "settled"
    finally:
        ledger.close()


def test_daily_ceiling_still_trips_within_one_utc_day(db_path: Path) -> None:
    """Within a single UTC day the default $200 ceiling still pauses the ledger."""
    clock = FakeClock(UTC_DAY_START + 3600.0)
    ledger = BudgetLedger(db_path, clock=clock)
    try:
        # Fill every pool exactly to its cap: total committed == 200.0 ==
        # DAILY_CEILING_USD, which trips the pause.
        for pool, cap in POOL_CAPS.items():
            ledger.reserve(pool=pool, estimated_max_usd=cap, idempotency_key=f"fill-{pool}")
        assert ledger.paused is True

        # Hours later, still the same UTC day: still paused, still refused.
        clock.advance(4.0 * 3600.0)
        assert ledger.paused is True
        # snapshot() must report the pause through the day-scoped property, not
        # a raw settings compare (the stored flag is a day stamp, never "1").
        assert ledger.snapshot()["paused"] is True
        with pytest.raises(BudgetRefused) as exc_info:
            ledger.reserve(pool="workers", estimated_max_usd=1.0, idempotency_key="late")
        assert exc_info.value.reason == "paused"
    finally:
        ledger.close()


def test_paused_ledger_unblocks_after_utc_midnight(db_path: Path) -> None:
    """A pause tripped by yesterday's spend expires at UTC midnight.

    Documented semantics (paused-flag preserved but day-scoped): the flag
    stores the UTC day it tripped on and only that day reads as paused, so a
    new UTC day un-blocks reserve() with no resume() call and no state
    mutation on read. Hitting today's fresh ceiling re-pauses and re-notifies.
    """
    notifications: list[tuple[str, str, float]] = []

    def notifier(pool: str, kind: str, fraction: float) -> None:
        notifications.append((pool, kind, fraction))

    clock = FakeClock(UTC_DAY_START + 3600.0)
    ledger = BudgetLedger(
        db_path,
        pool_caps={"workers": 100.0},
        ceiling_usd=100.0,
        clock=clock,
        notifier=notifier,
    )
    try:
        res = ledger.reserve(pool="workers", estimated_max_usd=100.0, idempotency_key="d0")
        ledger.settle(res.id, actual_usd=100.0)
        assert ledger.paused is True
        with pytest.raises(BudgetRefused) as exc_info:
            ledger.reserve(pool="workers", estimated_max_usd=1.0, idempotency_key="d0-late")
        assert exc_info.value.reason == "paused"
        assert [n[1] for n in notifications].count("pause") == 1

        # Cross UTC midnight: the pause no longer applies, without resume().
        clock.advance(DAY_SECONDS)
        assert ledger.paused is False
        assert ledger.snapshot()["paused"] is False

        res_new = ledger.reserve(pool="workers", estimated_max_usd=50.0, idempotency_key="d1")
        assert res_new.state == "open"
        assert ledger.paused is False

        # Today's ceiling is fresh: hitting it again re-pauses and re-notifies
        # (the pause-notification flag is day-scoped too).
        ledger.reserve(pool="workers", estimated_max_usd=50.0, idempotency_key="d1-fill")
        assert ledger.paused is True
        assert [n[1] for n in notifications].count("pause") == 2
    finally:
        ledger.close()


def test_settle_breaches_ceiling_triggers_pause(db_path: Path, fake_clock: FakeClock) -> None:
    """Test 8b: Settle a reservation with an actual cost well above max_usd, breaching ceiling, and check pause & notification."""
    notifications: list[tuple[str, str, float]] = []

    def test_notifier(pool: str, kind: str, fraction: float) -> None:
        notifications.append((pool, kind, fraction))

    # Global ceiling is 100.0, cap for workers is 150.0
    custom_caps = {"workers": 150.0}
    ledger = BudgetLedger(
        db_path, pool_caps=custom_caps, ceiling_usd=100.0, clock=fake_clock, notifier=test_notifier
    )
    try:
        # Reserve 10.0 in workers (global committed: 10.0, 10%)
        res = ledger.reserve(pool="workers", estimated_max_usd=10.0, idempotency_key="w1")
        assert ledger.paused is False
        assert len(notifications) == 0

        # Now, settle the reservation with 110.0 (reported actual is well above the reserved max)
        # This takes global committed to 110.0, which is >= 100.0 (the ceiling)
        ledger.settle(res.id, actual_usd=110.0)

        # Verify paused is True
        assert ledger.paused is True

        # Verify that a "pause" notification fired exactly once
        pause_notifs = [n for n in notifications if n[1] == "pause"]
        assert len(pause_notifs) == 1
        assert pause_notifs[0][0] == "workers"
        assert pause_notifs[0][2] == 1.10  # fraction_used = 110.0 / 100.0 = 1.1

    finally:
        ledger.close()
