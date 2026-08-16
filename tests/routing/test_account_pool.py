"""Tests for omniagentos.routing.account_pool -- round-robin, rate-limit-aware
account routing. Entirely offline (no CLI, no network, no real accounts
touched); the clock is injected so cooldown recovery is asserted by
advancing a fake clock, never by sleeping on a real one."""

from __future__ import annotations

import threading
from collections import Counter

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.routing.account_pool import (
    AccountPool,
    Outcome,
    account_pool_enabled,
    classify_outcome,
    get_default_pool,
    reset_default_pool,
)
from omniagentos.routing.config import Account, AccountPoolConfig, ProviderPool


class FakeClock:
    """Deterministic, manually-advanced clock for cooldown tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _pool_config(**accounts_by_provider: list[Account]) -> AccountPoolConfig:
    return AccountPoolConfig(
        providers={
            provider: ProviderPool(cooldown_seconds=60, accounts=accounts)
            for provider, accounts in accounts_by_provider.items()
        }
    )


def _account(
    id_: str,
    *,
    priority: int = 0,
    enabled: bool = True,
    max_inflight: int | None = None,
    cooldown_seconds: int | None = None,
) -> Account:
    return Account(
        id=id_,
        config_dir=f"/fake/{id_}",
        enabled=enabled,
        priority=priority,
        max_inflight=max_inflight,
        cooldown_seconds=cooldown_seconds,
    )


class TestRoundRobin:
    def test_cycles_a_b_a_b(self) -> None:
        config = _pool_config(claude=[_account("A", priority=0), _account("B", priority=1)])
        pool = AccountPool(config, now=FakeClock())
        picked = []
        for _ in range(5):
            account = pool.pick("claude")
            assert account is not None
            picked.append(account.id)
            pool.report(account.id, Outcome.OK)
        assert picked == ["A", "B", "A", "B", "A"]

    def test_priority_establishes_initial_order(self) -> None:
        config = _pool_config(claude=[_account("B", priority=5), _account("A", priority=0)])
        pool = AccountPool(config, now=FakeClock())
        account = pool.pick("claude")
        assert account is not None
        assert account.id == "A"

    def test_unknown_provider_returns_none(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A")]), now=FakeClock())
        assert pool.pick("nonexistent") is None

    def test_empty_provider_returns_none(self) -> None:
        pool = AccountPool(_pool_config(claude=[]), now=FakeClock())
        assert pool.pick("claude") is None

    def test_disabled_account_is_always_skipped(self) -> None:
        config = _pool_config(claude=[_account("A", enabled=False), _account("B")])
        pool = AccountPool(config, now=FakeClock())
        for _ in range(3):
            account = pool.pick("claude")
            assert account is not None
            assert account.id == "B"
            pool.report("B", Outcome.OK)


class TestCooldown:
    def test_rate_limited_cools_account_until_clock_advances(self) -> None:
        clock = FakeClock()
        config = _pool_config(claude=[_account("A"), _account("B")])
        pool = AccountPool(config, now=clock)

        first = pool.pick("claude")
        assert first is not None
        assert first.id == "A"
        pool.report("A", Outcome.RATE_LIMITED)  # A cools for the provider default (60s)

        # A is cooling: every subsequent pick must land on B only.
        for _ in range(3):
            account = pool.pick("claude")
            assert account is not None
            assert account.id == "B"
            pool.report("B", Outcome.OK)

        clock.advance(61)
        # A has recovered and rejoins the rotation.
        recovered = pool.pick("claude")
        assert recovered is not None
        assert recovered.id == "A"

    def test_cooldown_not_yet_expired_still_skips(self) -> None:
        clock = FakeClock()
        config = _pool_config(claude=[_account("A"), _account("B")])
        pool = AccountPool(config, now=clock)
        pool.pick("claude")
        pool.report("A", Outcome.RATE_LIMITED)
        clock.advance(59)  # one second short of the 60s default cooldown
        account = pool.pick("claude")
        assert account is not None
        assert account.id == "B"

    def test_per_account_cooldown_overrides_provider_default(self) -> None:
        clock = FakeClock()
        config = _pool_config(claude=[_account("A", cooldown_seconds=5), _account("B")])
        pool = AccountPool(config, now=clock)

        first = pool.pick("claude")
        assert first is not None
        assert first.id == "A"
        pool.report("A", Outcome.RATE_LIMITED)  # A cools for its own 5s override

        # Round robin naturally reaches B next regardless of A's cooldown --
        # this isn't yet testing recovery, just establishing the cursor.
        second = pool.pick("claude")
        assert second is not None
        assert second.id == "B"
        pool.report("B", Outcome.OK)

        clock.advance(6)  # past A's 5s override, nowhere near the provider's 60s default
        third = pool.pick("claude")
        assert third is not None
        assert third.id == "A"  # recovered via its OWN override, not the 60s default

    def test_ok_and_error_outcomes_do_not_cool(self) -> None:
        clock = FakeClock()
        config = _pool_config(claude=[_account("A"), _account("B")])
        pool = AccountPool(config, now=clock)
        for outcome in (Outcome.OK, Outcome.ERROR):
            account = pool.pick("claude")
            assert account is not None
            pool.report(account.id, outcome)

        # Neither report started a cooldown -- both accounts are still eligible.
        first = pool.pick("claude")
        second = pool.pick("claude")
        assert first is not None
        assert second is not None
        assert {first.id, second.id} == {"A", "B"}

    def test_report_unknown_account_id_is_a_no_op(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A")]), now=FakeClock())
        pool.report("does-not-exist", Outcome.RATE_LIMITED)  # must not raise
        account = pool.pick("claude")
        assert account is not None
        assert account.id == "A"


class TestAllCooling:
    def test_false_when_any_account_available(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A"), _account("B")]), now=FakeClock())
        assert pool.all_cooling("claude") is False

    def test_true_when_every_account_cooling(self) -> None:
        clock = FakeClock()
        pool = AccountPool(_pool_config(claude=[_account("A"), _account("B")]), now=clock)
        first = pool.pick("claude")
        assert first is not None
        pool.report(first.id, Outcome.RATE_LIMITED)
        second = pool.pick("claude")
        assert second is not None
        pool.report(second.id, Outcome.RATE_LIMITED)
        assert pool.all_cooling("claude") is True

    def test_true_when_no_accounts_configured(self) -> None:
        pool = AccountPool(_pool_config(claude=[]), now=FakeClock())
        assert pool.all_cooling("claude") is True

    def test_true_for_unconfigured_provider(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A")]), now=FakeClock())
        assert pool.all_cooling("unknown-provider") is True

    def test_disabled_only_account_still_counts_as_all_cooling(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A", enabled=False)]), now=FakeClock())
        assert pool.all_cooling("claude") is True

    def test_max_inflight_saturation_does_not_count_as_cooling(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A", max_inflight=1)]), now=FakeClock())
        pool.pick("claude")  # saturate the single slot
        # Busy is not the same as exhausted -- must NOT trigger model fallback.
        assert pool.all_cooling("claude") is False


class TestThreadSafety:
    """Many runner workers call pick()/report() concurrently in production;
    the lock must make check-then-increment atomic so max_inflight is never
    exceeded and no two callers are ever double-assigned past capacity."""

    def test_concurrent_pick_respects_max_inflight_single_account(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A", max_inflight=3)]), now=FakeClock())
        results: list[Account | None] = []
        results_lock = threading.Lock()

        def worker() -> None:
            account = pool.pick("claude")
            with results_lock:
                results.append(account)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        granted = [account for account in results if account is not None]
        assert len(granted) == 3
        assert all(account.id == "A" for account in granted)

    def test_concurrent_pick_across_two_accounts_never_exceeds_combined_capacity(self) -> None:
        pool = AccountPool(
            _pool_config(claude=[_account("A", max_inflight=2), _account("B", max_inflight=2)]),
            now=FakeClock(),
        )
        results: list[Account | None] = []
        results_lock = threading.Lock()

        def worker() -> None:
            account = pool.pick("claude")
            with results_lock:
                results.append(account)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        granted = [account for account in results if account is not None]
        assert len(granted) == 4  # exactly max_inflight(A) + max_inflight(B), never more
        assert Counter(account.id for account in granted) == {"A": 2, "B": 2}


class TestClassifyOutcome:
    @staticmethod
    def _result(status: ResultStatus, error: str | None) -> AgentResult:
        return AgentResult(status=status, error=error, usage=AgentUsage(wall_ms=1, estimated=True))

    def test_ok_status_classifies_as_ok_regardless_of_error_text(self) -> None:
        result = self._result(ResultStatus.OK, None)
        assert classify_outcome(result) is Outcome.OK

    @pytest.mark.parametrize(
        "error_text",
        [
            "rate limit exceeded",
            "Rate Limit hit, please retry",
            "429 Too Many Requests",
            "usage limit reached for this account",
            "the service is overloaded right now",
            "quota exceeded for today",
            "daily quota hit",
        ],
    )
    def test_rate_limit_signatures_classify_as_rate_limited(self, error_text: str) -> None:
        result = self._result(ResultStatus.ERROR, error_text)
        assert classify_outcome(result) is Outcome.RATE_LIMITED

    @pytest.mark.parametrize(
        "error_text",
        [
            "authentication failed",
            "connection refused",
            "error: unknown option '--effort'",
            "response is not valid JSON",
            "CLI invocation timed out",
            "",
        ],
    )
    def test_normal_errors_classify_as_error_not_rate_limited(self, error_text: str) -> None:
        result = self._result(ResultStatus.ERROR, error_text)
        assert classify_outcome(result) is Outcome.ERROR

    def test_missing_error_text_classifies_as_error(self) -> None:
        result = self._result(ResultStatus.ERROR, None)
        assert classify_outcome(result) is Outcome.ERROR

    def test_timeout_status_without_limit_language_is_a_normal_error(self) -> None:
        # A plain timeout is NOT evidence of quota exhaustion -- must not cool
        # the account (build step 5: "do NOT cool the account on a normal error").
        result = self._result(ResultStatus.TIMEOUT, "CLI invocation timed out")
        assert classify_outcome(result) is Outcome.ERROR

    def test_timeout_status_with_overload_language_is_rate_limited(self) -> None:
        result = self._result(ResultStatus.TIMEOUT, "upstream overloaded, timed out waiting")
        assert classify_outcome(result) is Outcome.RATE_LIMITED


class TestHasAccounts:
    def test_true_when_accounts_configured(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A")]), now=FakeClock())
        assert pool.has_accounts("claude") is True

    def test_false_when_provider_has_an_empty_account_list(self) -> None:
        pool = AccountPool(_pool_config(claude=[]), now=FakeClock())
        assert pool.has_accounts("claude") is False

    def test_false_for_a_provider_not_in_the_config_at_all(self) -> None:
        pool = AccountPool(_pool_config(claude=[_account("A")]), now=FakeClock())
        assert pool.has_accounts("codex") is False


class TestEnabledFlag:
    def test_unset_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNIAGENTOS_ACCOUNT_POOL", raising=False)
        assert account_pool_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "YES", "on"])
    def test_truthy_values_enable(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", value)
        assert account_pool_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_falsy_values_disable(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", value)
        assert account_pool_enabled() is False


class TestDefaultPool:
    def teardown_method(self) -> None:
        reset_default_pool(None)

    def test_get_default_pool_is_a_singleton(self) -> None:
        reset_default_pool(None)
        first = get_default_pool()
        second = get_default_pool()
        assert first is second

    def test_reset_default_pool_replaces_the_instance(self) -> None:
        custom = AccountPool(_pool_config(claude=[_account("only")]), now=FakeClock())
        reset_default_pool(custom)
        assert get_default_pool() is custom
