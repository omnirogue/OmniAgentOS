"""Round-robin, rate-limit-aware account pool.

Rotates calls across multiple subscription accounts for a provider (today:
multiple real ``~/.claude*`` config dirs on the same machine -- see
``configs/accounts.yaml``). A ``rate_limited`` report cools that ONE account
down for its ``cooldown_seconds``; once every account for a provider is
cooling or disabled, ``all_cooling()`` tells the caller to stop retrying
accounts and fall back to the next MODEL lineage instead (composes with
``omniagentos.intake.fallback.run_with_fallback`` -- see
``omniagentos/adapters/claude.py`` for the actual composition point).

Thread-safe: many runner workers call ``pick()``/``report()`` concurrently.
All state mutation happens under one lock.

WP2: the pool is a SHORT-TTL WRITE-THROUGH CACHE over the durable limit state
(``omniagentos.routing.limit_state`` -- the cross-process source of truth).
When constructed with ``durable_db_path``: ``report()`` also writes the outcome
through to ``report_outcome`` (RATE_LIMITED -> transient_rate_limit; OK -> ok,
the structured-first clear), and ``pick()`` consults the durable cooldown for a
candidate whose cached durable view is older than the TTL -- so a cooldown
written by ANOTHER process (longhaul engine, sessions daemon) is honored here
within one TTL. Without ``durable_db_path`` the pool behaves exactly as before
(pure in-process state); every durable call is fail-safe.

The clock is injectable (``now``) precisely so tests can advance past a
cooldown deterministically instead of sleeping on a real clock. Defaults to
``time.monotonic`` (immune to wall-clock adjustments, matches the rest of the
codebase's timing use, e.g. ``omniagentos/comms/ratelimit.py``).

NEVER log or print account credentials -- ``Account.config_dir`` is the only
per-account value this module (or anything built on it) ever surfaces, and
it is a filesystem PATH, not a secret.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from omniagentos.contracts import AgentResult, ResultStatus, default_db_path
from omniagentos.routing.config import (
    DEFAULT_COOLDOWN_SECONDS,
    Account,
    AccountPoolConfig,
    load_accounts_config,
)

LOG = logging.getLogger(__name__)

DEFAULT_PROVIDER = "claude"

# How long a durable-cooldown read stays trusted before pick() re-checks the
# database for that account (seconds). Short by design: the durable state is
# the source of truth across processes; the cache only absorbs hot-path reads.
DEFAULT_DURABLE_TTL_SECONDS = 5.0

# Rate/usage-limit signatures in a CLI's error envelope/stderr (build step 5:
# narrower than omniagentos.intake.fallback._is_limit_or_unavailable_error on
# purpose -- that function decides "give up on this MODEL", this one decides
# "cool this ACCOUNT", and a plain timeout or auth error is not evidence the
# account itself is out of quota, so it must NOT cool the account).
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "429",
    "usage limit",
    "overloaded",
    "quota",
)


class Outcome(StrEnum):
    """Result classification a caller reports back to the pool after a call."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


def classify_outcome(result: AgentResult) -> Outcome:
    """Classify a completed adapter call for pool bookkeeping.

    ``OK`` on success. ``RATE_LIMITED`` only when the error text matches a
    rate/usage-limit signature (see ``_RATE_LIMIT_PATTERNS``). Everything else
    -- a plain error, a validation failure, a timeout with no limit language,
    or a classification-related failure text -- classifies as ``ERROR`` and
    must NOT cool the account or be written through as account success
    (M-14: no dead Outcome path; classification failure ≠ account OK).
    """
    if result.status is ResultStatus.OK:
        return Outcome.OK

    error_text = (result.error or "").lower()
    if any(pattern in error_text for pattern in _RATE_LIMIT_PATTERNS):
        return Outcome.RATE_LIMITED
    return Outcome.ERROR


@dataclass
class _AccountState:
    account: Account
    provider: str
    in_flight: int = 0
    cooldown_until: float = 0.0
    # WP2 write-through cache bookkeeping: when this account's durable
    # cooldown was last consulted (pool clock), and its resolved
    # claude_accounts.id (lazily looked up by config dir; None = not yet).
    durable_checked_at: float | None = None
    durable_account_id: str | None = None


class AccountPool:
    """Round-robin account router. One instance normally lives for the whole
    process (see ``get_default_pool``); tests construct their own with a fake
    clock and/or an in-memory config."""

    def __init__(
        self,
        config: AccountPoolConfig | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        durable_db_path: str | None = None,
        durable_ttl_seconds: float = DEFAULT_DURABLE_TTL_SECONDS,
    ) -> None:
        self._config = config if config is not None else load_accounts_config()
        self._now = now
        # WP2: when set, this pool is a write-through cache over the durable
        # limit state in this database (see module docstring). None (the
        # default, and what every pre-existing caller passes) keeps the pool
        # purely in-process -- no behavior change.
        self._durable_db_path = durable_db_path
        self._durable_ttl = durable_ttl_seconds
        self._lock = threading.Lock()
        self._states: dict[str, _AccountState] = {}
        self._order: dict[str, list[str]] = {}
        self._cursor: dict[str, int] = {}
        for provider, pool in self._config.providers.items():
            ordered = sorted(pool.accounts, key=lambda a: (a.priority, a.id))
            self._order[provider] = [account.id for account in ordered]
            self._cursor[provider] = 0
            for account in ordered:
                self._states[account.id] = _AccountState(account=account, provider=provider)

    def has_accounts(self, provider: str) -> bool:
        """True when at least one account is configured for ``provider``
        (regardless of enabled/cooling state) -- callers use this to decide
        whether to engage pooled routing at all, vs. run unpooled."""
        return bool(self._order.get(provider))

    def _cooldown_seconds(self, state: _AccountState) -> int:
        if state.account.cooldown_seconds is not None:
            return state.account.cooldown_seconds
        pool = self._config.providers.get(state.provider)
        return pool.cooldown_seconds if pool is not None else DEFAULT_COOLDOWN_SECONDS

    # -- WP2 durable write-through cache helpers (all fail-safe: a database
    # -- problem degrades to pure in-process behavior, never an exception).

    def _durable_account_id(self, state: _AccountState) -> str | None:
        """Resolve (and cache) the durable claude_accounts.id for a pool account."""
        if state.durable_account_id is not None:
            return state.durable_account_id
        try:
            from omniagentos.routing.limit_state import account_id_for_config_dir

            resolved = account_id_for_config_dir(
                state.account.resolved_config_dir(), db_path=self._durable_db_path
            )
        except Exception:  # noqa: BLE001
            LOG.debug("durable account resolution failed for %s", state.account.id, exc_info=True)
            return None
        if resolved is not None:
            state.durable_account_id = resolved
        return resolved

    def _consult_durable_cooldown(self, state: _AccountState, now: float) -> None:
        """Refresh ``state.cooldown_until`` from the durable cooldown when the
        cached view is older than the TTL. A durable cooldown written by
        another process becomes a local cooldown here (pool clock)."""
        if (
            state.durable_checked_at is not None
            and now - state.durable_checked_at < self._durable_ttl
        ):
            return
        state.durable_checked_at = now
        try:
            from omniagentos.routing.limit_state import durable_cooldown_remaining

            remaining = durable_cooldown_remaining(
                self._durable_account_id(state),
                config_dir=state.account.resolved_config_dir(),
                db_path=self._durable_db_path,
            )
        except Exception:  # noqa: BLE001
            LOG.debug("durable cooldown read failed for %s", state.account.id, exc_info=True)
            return
        if remaining > 0:
            state.cooldown_until = max(state.cooldown_until, now + remaining)

    def _write_through(self, state: _AccountState, outcome: Outcome) -> None:
        """Report the outcome to the durable limit state (RATE_LIMITED cools
        durably; OK is the structured-first clear; plain ERROR carries no
        durable evidence and is not written).

        Only ``Outcome.OK`` may be recorded as account success. Unknown or
        non-success outcomes never clear durable cooldowns (M-14).
        """
        if outcome is Outcome.ERROR:
            return
        if outcome is not Outcome.OK and outcome is not Outcome.RATE_LIMITED:
            # Fail closed: never invent account success for unclassified outcomes.
            return
        account_id = self._durable_account_id(state)
        if account_id is None:
            return
        try:
            from omniagentos.routing.limit_state import report_outcome

            report_outcome(
                state.provider,
                account_id,
                "transient_rate_limit" if outcome is Outcome.RATE_LIMITED else "ok",
                detail=f"account pool report: {outcome.value}",
                db_path=self._durable_db_path,
            )
        except Exception:  # noqa: BLE001
            LOG.debug("durable write-through failed for %s", state.account.id, exc_info=True)

    def pick(self, provider: str = DEFAULT_PROVIDER, *, interactive: bool = False) -> Account | None:
        """Round-robin the next eligible account for ``provider``.

        Eligible means enabled, not currently cooling, within its weekly quota,
        and (if ``max_inflight`` is set) not already at that concurrency cap.
        Automated callers cannot use an account's interactive reserve; pass
        ``interactive=True`` only for an explicit human session. Skips
        ineligible accounts and continues the rotation past them without
        disturbing their place in line. Increments the chosen account's
        in-flight count. Returns ``None`` when no account is eligible right
        now (every account is cooling/disabled/saturated/at quota).
        """
        with self._lock:
            order = self._order.get(provider) or []
            count = len(order)
            if count == 0:
                return None
            now = self._now()
            cursor = self._cursor.get(provider, 0) % count
            for step in range(count):
                index = (cursor + step) % count
                state = self._states[order[index]]
                if not state.account.enabled:
                    continue
                if state.cooldown_until > now:
                    continue
                if (
                    state.account.max_inflight is not None
                    and state.in_flight >= state.account.max_inflight
                ):
                    continue
                # WP2: consult the durable cooldown on a cache miss (TTL
                # expiry) so a cooldown written by another process is honored.
                if self._durable_db_path is not None:
                    self._consult_durable_cooldown(state, now)
                    if state.cooldown_until > now:
                        continue
                    # M5: the reserve is checked on every pick (never cached).
                    # A missing durable id or a quota read failure is unsafe for
                    # automation, so it receives no account.
                    durable_account_id = self._durable_account_id(state)
                    if durable_account_id is None:
                        continue
                    try:
                        from omniagentos.routing.limit_state import (
                            increment_weekly_attempts,
                            weekly_attempts_quota_remaining,
                        )

                        remaining, reserve_floor, can_use_reserve = (
                            weekly_attempts_quota_remaining(
                                durable_account_id,
                                state.provider,
                                self._durable_db_path,
                                interactive=interactive,
                            )
                        )
                        if remaining <= 0 or (
                            remaining <= reserve_floor and not can_use_reserve
                        ):
                            continue
                        # Repeat the decision while atomically incrementing so
                        # another process cannot spend through the reserve.
                        if not increment_weekly_attempts(
                            durable_account_id,
                            db_path=self._durable_db_path,
                            provider=state.provider,
                            interactive=interactive,
                        ):
                            continue
                    except Exception:  # noqa: BLE001
                        LOG.debug("weekly quota check failed for %s", state.account.id, exc_info=True)
                        continue
                state.in_flight += 1
                self._cursor[provider] = (index + 1) % count
                return state.account
            return None

    def report(self, account_id: str, outcome: Outcome) -> None:
        """Record the result of a call made against ``account_id``.

        Always decrements in-flight (paired with the increment ``pick()``
        made when it returned this account). A ``rate_limited`` outcome
        starts (or restarts) the account's cooldown; ``ok``/``error`` never
        touch the cooldown. Unknown ids are ignored (defensive -- a caller
        reporting against a stale/removed account should never raise).
        """
        with self._lock:
            state = self._states.get(account_id)
            if state is None:
                return
            state.in_flight = max(0, state.in_flight - 1)
            if outcome is Outcome.RATE_LIMITED:
                state.cooldown_until = self._now() + self._cooldown_seconds(state)
        # WP2 write-through (outside the lock: durable I/O must not serialize
        # every concurrent pick/report against a database write).
        if self._durable_db_path is not None:
            self._write_through(state, outcome)

    def all_cooling(self, provider: str = DEFAULT_PROVIDER) -> bool:
        """True when every enabled account for ``provider`` is currently
        cooling, or when there are no (enabled) accounts at all -- either way
        the signal is the same: stop retrying accounts, fall back to the next
        model lineage. ``max_inflight`` saturation does NOT count as cooling
        here -- a busy-but-healthy account will free up shortly, it isn't
        evidence the provider is exhausted.
        """
        with self._lock:
            order = self._order.get(provider) or []
            if not order:
                return True
            now = self._now()
            for account_id in order:
                state = self._states[account_id]
                if state.account.enabled and state.cooldown_until <= now:
                    return False
            return True


_default_pool: AccountPool | None = None
_default_pool_lock = threading.Lock()


def get_default_pool() -> AccountPool:
    """Process-wide singleton pool, built from ``configs/accounts.yaml`` on
    first use. Thread-safe, built at most once."""
    global _default_pool
    with _default_pool_lock:
        if _default_pool is None:
            _default_pool = AccountPool(durable_db_path=default_db_path())
        return _default_pool


def reset_default_pool(pool: AccountPool | None = None) -> None:
    """Test hook: replace (or clear, forcing a rebuild on next use) the
    process-wide singleton. Production code never calls this."""
    global _default_pool
    with _default_pool_lock:
        _default_pool = pool


def account_pool_enabled() -> bool:
    """``OMNIAGENTOS_ACCOUNT_POOL=1`` (or any other truthy value) opts in;
    unset (or any falsy value) keeps the pre-existing single-account
    behavior -- no regression when the flag is off."""
    value = os.environ.get("OMNIAGENTOS_ACCOUNT_POOL", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


__all__ = [
    "DEFAULT_DURABLE_TTL_SECONDS",
    "DEFAULT_PROVIDER",
    "AccountPool",
    "Outcome",
    "account_pool_enabled",
    "classify_outcome",
    "get_default_pool",
    "reset_default_pool",
]
