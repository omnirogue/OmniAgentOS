"""Durable rate-limit / cooldown / inflight authority for provider accounts.

THE single brain for account limit state (plan WP2 / longhaul unification #1):

- every cooldown or disable write for ``claude_accounts`` flows through
  :func:`report_outcome` (which delegates to the single writer in
  ``omniagentos.accounts.service``);
- every spawn-side account choice flows through :func:`pick_account` or
  :func:`reserve_account`;
- pressure / all-cooling / inflight reads come from here.

``AccountPool`` (``omniagentos.routing.account_pool``) is a short-TTL
in-process write-through cache over this module; longhaul's engine and store
delegate their cooldown reads/writes here. Durable state in SQLite is the
source of truth across the API process and the sessions daemon.

Four-class outcome table (CLIs swallow rate-limit headers, so classification
is output-pattern based -- see ``omniagentos.longhaul.limits``):

  outcome               durable effect
  --------------------  ------------------------------------------------------
  transient_rate_limit  cooldown for base * uniform(0.8, 1.2) -- base from
                        configs/accounts.yaml (provider cooldown_seconds);
                        status -> 'rate_limited'
  quota_exhausted       cooldown until the parsed window reset (``reset_at``);
                        fallback configs/swarm.yaml quota_fallback_seconds;
                        status -> 'rate_limited' (COOL, never disable)
  overloaded            30-120s backoff cooldown (configs/swarm.yaml bounds);
                        status NOT changed -- overload is the provider's
                        problem, not evidence about this account
  auth_error            STOP THE LINE: disable the account (enabled=0,
                        status='error') + operator notification; NEVER
                        auto-retried -- ten agents retrying auth gets accounts
                        locked
  ok                    STRUCTURED-FIRST: a clean completion clears the
                        cooldown and restores status='ok' -- recovered
                        intermediate 401/429 hints must never cool a healthy
                        account. Never re-enables a disabled account.

Durable inflight for an account = COUNT of live sessions attributed to it
(``sessions.account_id``, persisted by the supervisor at launch) that are in
starting/running, NOT kill-requested, and activity-fresh (<30 min -- zombies
must not permanently inflate provider pressure; the A2 reaper is the cleanup,
this is the safety margin), UNION'd with open longhaul attempts
(``task_sessions.account_id``, ``ended_at IS NULL``) whose session row does
not already carry the attribution -- longhaul historically never wrote
``sessions.account_id`` (unification item 3), so without the union every
longhaul claude session would be invisible and shared accounts would
over-subscribe. Active (unexpired) reservations count too.

:func:`reserve_account` makes count+reserve one atomic step (BEGIN IMMEDIATE):
two slot workers each observing N < limit concurrently must not both spawn.
The reservation converts to real inflight when the supervisor persists
``sessions.account_id`` at launch (:func:`convert_reservation` deletes the
row); the short TTL (~120s) reclaims it when the spawn never happened.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from omniagentos.accounts.service import (
    AVAILABLE_PREDICATE,
    LRU_ORDER,
    SpawnAccount,
    next_account_for_spawn,
    predictive_out_ids,
    set_account_cooldown,
    spawn_account_from_row,
)
from omniagentos.contracts import default_db_path, new_id, utc_now_iso
from omniagentos.db.store import _connect
from omniagentos.path_containment import inode_paths_equal

LOG = logging.getLogger(__name__)

DEFAULT_PROVIDER = "claude"

# The four failure classes + the structured-first success signal.
OUTCOME_OK = "ok"
OUTCOME_TRANSIENT_RATE_LIMIT = "transient_rate_limit"
OUTCOME_QUOTA_EXHAUSTED = "quota_exhausted"
OUTCOME_OVERLOADED = "overloaded"
OUTCOME_AUTH_ERROR = "auth_error"
OUTCOMES = frozenset(
    {
        OUTCOME_OK,
        OUTCOME_TRANSIENT_RATE_LIMIT,
        OUTCOME_QUOTA_EXHAUSTED,
        OUTCOME_OVERLOADED,
        OUTCOME_AUTH_ERROR,
    }
)

# Session states that consume account capacity.
_LIVE_SESSION_STATES = ("starting", "running")

# Hard-coded fallbacks when configs/swarm.yaml is absent or partial. Values
# mirror the checked-in config; the config is the tunable, these are the floor.
_DEFAULT_INFLIGHT_FRESH_MINUTES = 30.0
_DEFAULT_RESERVATION_TTL_SECONDS = 120.0
_DEFAULT_TRANSIENT_COOLDOWN_SECONDS = 60.0
_DEFAULT_OVERLOADED_BACKOFF_MIN = 30.0
_DEFAULT_OVERLOADED_BACKOFF_MAX = 120.0
_DEFAULT_QUOTA_FALLBACK_SECONDS = 3600.0
_DEFAULT_INTERACTIVE_RESERVE_ATTEMPTS = 10
_DEFAULT_WEEKLY_ATTEMPT_QUOTA = 250
# fleet-scale-200: mirror configs/swarm.yaml (tests/routing/test_fleet_scale.py
# pins the equality — a fallback that lags the config silently caps a fleet whose
# config file failed to load at the OLD ceiling, which is the hardest failure of
# this kind to diagnose).
_DEFAULT_MAX_SESSIONS_GLOBAL = 260
_DEFAULT_RESERVED_SMALL_TASK_SLOTS = 40
# Same invariant, extended to the per-account ceiling at integration time.
# fleet-scale-200 deliberately left this at 3 because feat/provider-resilience
# owned the value; that branch then raised the CONFIG to 20 without moving the
# fallback, which is exactly the drift the comment above warns about — a missing
# or partial swarm.yaml would have handed out 260 global sessions while capping
# each account at 3, a silent ~7x capacity cliff with no log line.
_DEFAULT_MAX_INFLIGHT_PER_ACCOUNT = 20
# PKG-INSESSION-FANOUT: AGENTS per account = live sessions + committed live
# in-session grant budgets. Same mirror invariant as the ceilings above.
_DEFAULT_MAX_AGENTS_PER_ACCOUNT = 60

_TRANSIENT_JITTER_LOW = 0.8
_TRANSIENT_JITTER_HIGH = 1.2


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def swarm_config_path() -> Path:
    """Resolve configs/swarm.yaml cwd-independently.

    ``OMNIAGENTOS_SWARM_CONFIG`` overrides (same convention as
    ``OMNIAGENTOS_ACCOUNTS_CONFIG``)."""
    override = os.environ.get("OMNIAGENTOS_SWARM_CONFIG")
    if override:
        return Path(override)
    return _repo_root() / "configs" / "swarm.yaml"


def load_swarm_config(path: Path | None = None) -> dict[str, Any]:
    """configs/swarm.yaml as a plain dict; missing/broken file -> ``{}``.

    Best-effort by design: the durable limit-state must keep functioning on its
    hard-coded defaults when the config is absent (fresh checkout, tests)."""
    try:
        raw = yaml.safe_load((path or swarm_config_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _limits_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else load_swarm_config()
    limits = config.get("limits")
    return limits if isinstance(limits, dict) else {}


def _provider_limits(provider: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    limits = _limits_section(config)
    providers = limits.get("providers")
    if isinstance(providers, dict):
        entry = providers.get(provider)
        if isinstance(entry, dict):
            return entry
    return {}


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def inflight_fresh_minutes() -> float:
    """Activity-freshness window for durable inflight (zombie safety margin)."""
    return _as_float(
        _limits_section().get("inflight_fresh_minutes"), _DEFAULT_INFLIGHT_FRESH_MINUTES
    )


def reservation_ttl_seconds() -> float:
    return _as_float(
        _limits_section().get("reservation_ttl_seconds"), _DEFAULT_RESERVATION_TTL_SECONDS
    )


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def interactive_reserve_attempts() -> int:
    """Attempts held back for a human interactive session per account."""
    return _positive_int(
        _limits_section().get("interactive_reserve_attempts"),
        _DEFAULT_INTERACTIVE_RESERVE_ATTEMPTS,
    )


def weekly_attempt_quota_default() -> int:
    """Fallback weekly ceiling for account rows without an explicit quota."""
    return _positive_int(
        _limits_section().get("weekly_attempt_quota_default"),
        _DEFAULT_WEEKLY_ATTEMPT_QUOTA,
    )


def _transient_base_seconds(provider: str) -> float:
    """Transient-cooldown base: configs/accounts.yaml is authoritative,
    configs/swarm.yaml is the fallback for providers not declared there."""
    try:
        from omniagentos.routing.config import load_accounts_config

        pool = load_accounts_config().providers.get(provider)
        if pool is not None:
            return float(pool.cooldown_seconds)
    except Exception:  # noqa: BLE001 - config trouble must not break reporting
        LOG.debug("accounts.yaml unavailable; using swarm.yaml transient base", exc_info=True)
    return _as_float(
        _provider_limits(provider).get("transient_cooldown_seconds"),
        _DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    )


def _overloaded_bounds(provider: str) -> tuple[float, float]:
    entry = _provider_limits(provider)
    low = _as_float(entry.get("overloaded_backoff_seconds_min"), _DEFAULT_OVERLOADED_BACKOFF_MIN)
    high = _as_float(entry.get("overloaded_backoff_seconds_max"), _DEFAULT_OVERLOADED_BACKOFF_MAX)
    if high < low:
        low, high = high, low
    return low, high


def _quota_fallback_seconds(provider: str) -> float:
    return _as_float(
        _provider_limits(provider).get("quota_fallback_seconds"),
        _DEFAULT_QUOTA_FALLBACK_SECONDS,
    )


def max_inflight_per_account(provider: str) -> int:
    """Static per-account concurrency ceiling (configs/swarm.yaml). The plan's
    empirically derived ceilings (from swarm_attempts history) override this
    later -- the goal is to never see a 429, not to handle it gracefully."""
    raw = _provider_limits(provider).get("max_inflight_per_account")
    if raw is None:
        return _DEFAULT_MAX_INFLIGHT_PER_ACCOUNT
    try:
        ceiling = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_INFLIGHT_PER_ACCOUNT
    return ceiling if ceiling > 0 else _DEFAULT_MAX_INFLIGHT_PER_ACCOUNT


def max_agents_per_account(provider: str) -> int:
    """Per-account AGENT ceiling: live sessions PLUS committed in-session
    fan-out budgets (PKG-INSESSION-FANOUT grant admission). Distinct from
    :func:`max_inflight_per_account`, which caps CLI processes only — a
    session hosting granted subagents consumes ONE process slot but up to
    1 + max_children agent slots."""
    raw = _provider_limits(provider).get("max_agents_per_account")
    if raw is None:
        return _DEFAULT_MAX_AGENTS_PER_ACCOUNT
    try:
        ceiling = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGENTS_PER_ACCOUNT
    return ceiling if ceiling > 0 else _DEFAULT_MAX_AGENTS_PER_ACCOUNT


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc(now: datetime | None = None) -> datetime:
    return now.astimezone(UTC) if now is not None else datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _next_weekly_reset(now: datetime) -> datetime:
    """Return the next UTC Monday boundary for the account quota window."""
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_today + timedelta(days=(7 - now.weekday()) % 7 or 7)


def _reset_weekly_quota_if_needed(
    conn: sqlite3.Connection,
    account_id: str,
    provider: str,
    now: datetime,
) -> tuple[int, int] | None:
    """Return ``(quota, used)`` after lazily rolling an expired window.

    A NULL reset value is expected for accounts that predate migration 117 and
    starts a fresh UTC Monday--Sunday window. A malformed non-NULL value is
    deliberately rejected: guessing about a corrupted quota window could hand
    automated work the interactive reserve.
    """
    row = conn.execute(
        "SELECT weekly_attempt_quota, weekly_attempts_used, quota_reset_at "
        "FROM claude_accounts WHERE id = ? AND provider = ?",
        (account_id, provider),
    ).fetchone()
    if row is None:
        return None

    raw_reset_at = row["quota_reset_at"]
    reset_at = _parse_iso(raw_reset_at)
    if raw_reset_at is not None and reset_at is None:
        raise ValueError("malformed quota_reset_at")
    if reset_at is None or reset_at <= now:
        conn.execute(
            "UPDATE claude_accounts SET weekly_attempts_used = 0, quota_reset_at = ?, "
            "updated_at = ? WHERE id = ?",
            (_iso(_next_weekly_reset(now)), _iso(now), account_id),
        )
        used = 0
    else:
        try:
            used = int(row["weekly_attempts_used"])
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed weekly_attempts_used") from exc
        if used < 0:
            raise ValueError("negative weekly_attempts_used")

    raw_quota = row["weekly_attempt_quota"]
    if raw_quota is None:
        quota = weekly_attempt_quota_default()
    else:
        try:
            quota = int(raw_quota)
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed weekly_attempt_quota") from exc
    if quota < 0:
        raise ValueError("negative weekly_attempt_quota")
    return quota, used


def weekly_attempts_quota_remaining(
    account_id: str,
    provider: str,
    db_path: str | None = None,
    *,
    interactive: bool = True,
    now: datetime | None = None,
) -> tuple[int, int, bool]:
    """Return remaining weekly attempts, the interactive reserve, and access.

    This is intentionally fail-closed. If durable quota state cannot be read,
    automated callers receive neither normal capacity nor the interactive
    reserve. ``interactive`` defaults to true for direct legacy consumers;
    :meth:`AccountPool.pick` explicitly marks background work non-interactive.
    """
    reserve_floor = interactive_reserve_attempts()
    conn: sqlite3.Connection | None = None
    try:
        reference = _now_utc(now)
        conn = _connect(db_path or default_db_path())
        conn.execute("BEGIN IMMEDIATE")
        state = _reset_weekly_quota_if_needed(conn, account_id, provider, reference)
        conn.commit()
        if state is None:
            return 0, reserve_floor, False
        quota, used = state
        remaining = max(0, quota - used)
        return remaining, reserve_floor, bool(interactive and remaining > 0)
    except Exception:  # noqa: BLE001 - quota uncertainty must protect the reserve
        if conn is not None:
            conn.rollback()
        LOG.debug("weekly quota read failed for %s", account_id, exc_info=True)
        return 0, reserve_floor, False
    finally:
        if conn is not None:
            conn.close()


def increment_weekly_attempts(
    account_id: str,
    count: int = 1,
    db_path: str | None = None,
    *,
    provider: str = DEFAULT_PROVIDER,
    interactive: bool = True,
    now: datetime | None = None,
) -> bool:
    """Atomically record allocated attempts, refusing exhausted/reserved capacity.

    The optional caller context lets ``AccountPool`` repeat its quota decision
    under the write transaction, closing the cross-process race between its
    required read-before-pick check and the accounting write.
    """
    if count <= 0:
        return False
    conn: sqlite3.Connection | None = None
    try:
        reference = _now_utc(now)
        conn = _connect(db_path or default_db_path())
        conn.execute("BEGIN IMMEDIATE")
        state = _reset_weekly_quota_if_needed(conn, account_id, provider, reference)
        if state is None:
            conn.rollback()
            return False
        quota, used = state
        remaining = max(0, quota - used)
        reserve_floor = interactive_reserve_attempts()
        if remaining < count or (not interactive and remaining - count < reserve_floor):
            conn.rollback()
            return False
        conn.execute(
            "UPDATE claude_accounts SET weekly_attempts_used = weekly_attempts_used + ?, "
            "updated_at = ? WHERE id = ? AND provider = ?",
            (count, _iso(reference), account_id, provider),
        )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 - a failed accounting write must deny allocation
        if conn is not None:
            conn.rollback()
        LOG.debug("weekly quota increment failed for %s", account_id, exc_info=True)
        return False
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def pick_account(
    provider: str = DEFAULT_PROVIDER, *, db_path: str | None = None
) -> SpawnAccount | None:
    """The next enabled, non-cooling account for ``provider`` (LRU round-robin).

    Thin provider-aware wrapper over ``accounts.service.next_account_for_spawn``
    (the accounts table is generalized by migration 045's ``provider`` column).
    Returns ``None`` when every account for the provider is disabled or cooling
    -- callers then fall back exactly as before this module existed."""
    return next_account_for_spawn(db_path, provider=provider)


def list_available_accounts(
    provider: str = DEFAULT_PROVIDER, *, db_path: str | None = None
) -> list[dict[str, Any]]:
    """Enabled, non-cooling accounts for ``provider``, LRU-first.

    Same ordering contract as ``next_account_for_spawn`` but read-only (no
    rotation-cursor advance) -- longhaul's ``_available_accounts`` routes here,
    and consumes element [0], so this list carries the same predictive-balance
    stable partition as the write-side selectors (review RTE-01 R2)."""
    conn = _connect(db_path or default_db_path())
    try:
        now = utc_now_iso()
        out_ids = predictive_out_ids(conn, provider, now)
        rows = conn.execute(
            f"SELECT * FROM claude_accounts WHERE {AVAILABLE_PREDICATE} {LRU_ORDER}",
            {"provider": provider, "now": now},
        ).fetchall()
        ordered = [r for r in rows if str(r["id"]) not in out_ids] + [
            r for r in rows if str(r["id"]) in out_ids
        ]
        return [dict(row) for row in ordered]
    finally:
        conn.close()


def account_id_for_config_dir(config_dir: str, *, db_path: str | None = None) -> str | None:
    """Resolve a CLI config dir to its ``claude_accounts.id`` (or ``None``).

    The in-process ``AccountPool`` keys accounts by configs/accounts.yaml ids;
    the durable table keys them by ``acct_*`` ids registered per config dir.
    This is the bridge the pool's write-through uses."""
    resolved = os.path.realpath(os.path.expanduser(config_dir))
    conn = _connect(db_path or default_db_path())
    try:
        for row in conn.execute(
            "SELECT id, config_dir FROM claude_accounts WHERE config_dir IS NOT NULL"
        ).fetchall():
            stored = os.path.realpath(os.path.expanduser(str(row["config_dir"])))
            # Safety (`is True`): return an account only for positively equal identity.
            if inode_paths_equal(stored, resolved) is True:
                return str(row["id"])
        return None
    finally:
        conn.close()


def enabled_providers(*, db_path: str | None = None) -> list[str]:
    """Distinct providers with at least one ENABLED account, sorted.

    The auto solo-vs-swarm headroom decision (``swarm.planner.swarm_headroom``)
    averages :func:`provider_pressure` over exactly this set — a provider with
    no enabled account contributes no fleet capacity, so its permanent 1.0
    pressure must not drown out the providers that can actually spawn."""
    conn = _connect(db_path or default_db_path())
    try:
        rows = conn.execute(
            "SELECT DISTINCT provider FROM claude_accounts WHERE enabled = 1 ORDER BY provider"
        ).fetchall()
        return [str(row["provider"]) for row in rows]
    finally:
        conn.close()


def durable_cooldown_remaining(
    account_id: str | None = None,
    *,
    config_dir: str | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> float:
    """Seconds of durable cooldown remaining for an account (0.0 when none).

    Looked up by ``account_id`` or, for cache layers that only know the config
    dir, by ``config_dir``. Unknown accounts report 0.0 (no durable opinion)."""
    if account_id is None and config_dir is not None:
        account_id = account_id_for_config_dir(config_dir, db_path=db_path)
    if account_id is None:
        return 0.0
    conn = _connect(db_path or default_db_path())
    try:
        row = conn.execute(
            "SELECT cooldown_until FROM claude_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    until = _parse_iso(row["cooldown_until"])
    if until is None:
        return 0.0
    return max(0.0, (until - _now_utc(now)).total_seconds())


# ---------------------------------------------------------------------------
# Outcome reporting (the four-class table)
# ---------------------------------------------------------------------------


def report_outcome(
    provider: str,
    account_id: str,
    outcome: str,
    detail: str = "",
    *,
    reset_at: str | None = None,
    db_path: str | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one outcome from the four-class table to durable account state.

    Returns ``{"outcome", "cooldown_until", "disabled"}`` for callers/tests.
    Raises ``ValueError`` on an unknown outcome -- a typo'd class silently
    no-opping would defeat the whole authority.

    STRUCTURED-FIRST invariant (docs/architecture/longhaul.md): callers report
    ``ok`` on any clean completion, and ``ok`` clears/overrides whatever an
    intermediate 401/429 hint may have written -- a recovered blip must never
    leave a healthy account cooling. ``ok`` never re-enables a disabled
    account: auth disables are operator-only reversals."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown limit outcome: {outcome!r}")
    # The module-level ``random`` quacks like a Random instance (its functions
    # ARE a shared instance's bound methods); tests inject a seeded one.
    rng = rng if rng is not None else cast(random.Random, random)
    reference = _now_utc(now)
    resolved_db = db_path or default_db_path()

    if outcome == OUTCOME_OK:
        conn = _connect(resolved_db)
        try:
            conn.execute(
                "UPDATE claude_accounts SET status = 'ok', status_detail = NULL, "
                "cooldown_until = NULL, updated_at = ? WHERE id = ? AND enabled = 1",
                (utc_now_iso(), account_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"outcome": outcome, "cooldown_until": None, "disabled": False}

    if outcome == OUTCOME_TRANSIENT_RATE_LIMIT:
        base = _transient_base_seconds(provider)
        seconds = base * rng.uniform(_TRANSIENT_JITTER_LOW, _TRANSIENT_JITTER_HIGH)
        until = _iso(reference + timedelta(seconds=seconds))
        set_account_cooldown(account_id, until, detail or "transient rate limit", resolved_db)
        return {"outcome": outcome, "cooldown_until": until, "disabled": False}

    if outcome == OUTCOME_QUOTA_EXHAUSTED:
        until = reset_at or _iso(reference + timedelta(seconds=_quota_fallback_seconds(provider)))
        set_account_cooldown(account_id, until, detail or "quota exhausted", resolved_db)
        return {"outcome": outcome, "cooldown_until": until, "disabled": False}

    if outcome == OUTCOME_OVERLOADED:
        low, high = _overloaded_bounds(provider)
        until = _iso(reference + timedelta(seconds=rng.uniform(low, high)))
        # A backoff, NOT a status change: status stays whatever it was.
        set_account_cooldown(
            account_id, until, detail or "provider overloaded", resolved_db, status=None
        )
        return {"outcome": outcome, "cooldown_until": until, "disabled": False}

    # OUTCOME_AUTH_ERROR -- stop the line: disable + notify, never auto-retry.
    from omniagentos.accounts.service import mark_status, set_enabled

    label: str | None = None
    conn = _connect(resolved_db)
    try:
        row = conn.execute(
            "SELECT label FROM claude_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        label = str(row["label"]) if row is not None else None
    finally:
        conn.close()
    set_enabled(account_id, False, db_path=resolved_db)
    mark_status(account_id, "error", detail or "authentication failed", db_path=resolved_db)
    _notify_auth_failure(provider, account_id, label, detail, resolved_db)
    return {"outcome": outcome, "cooldown_until": None, "disabled": True}


def _notify_auth_failure(
    provider: str, account_id: str, label: str | None, detail: str, db_path: str
) -> None:
    """Operator notification for a stop-the-line auth failure. Best-effort:
    the disable itself already happened; a notification failure is logged."""
    try:
        from omniagentos.notifications.service import record_notification

        name = label or account_id
        # kind must satisfy the notifications CHECK constraint
        # ('approval','alert','escalation','done','blocked','info'); a disabled
        # account needing a human re-login is an escalation.
        record_notification(
            kind="escalation",
            title=f"{provider} account disabled: authentication failure",
            body=(
                f"Account {name!r} was DISABLED after a hard authentication failure "
                f"and will never be auto-retried. Re-login and re-enable it manually. "
                f"Detail: {detail[:300] or 'authentication failed'}"
            ),
            severity="critical",
            ref_type="claude_account",
            ref_id=account_id,
            payload={
                "reason": "account_auth_failure",
                "provider": provider,
                "account_id": account_id,
                "detail": detail[:500],
            },
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001 - notification must never own the disable.
        LOG.debug("auth-failure notification failed for %s", account_id, exc_info=True)


def clear_expired_cooldowns(now_iso: str | None = None, *, db_path: str | None = None) -> list[str]:
    """Clear expired cooldowns, restoring status='ok'. Single implementation
    lives in ``accounts.service``; exposed here so limit-state callers need no
    second import."""
    from omniagentos.accounts.service import clear_expired_cooldowns as _clear

    return _clear(now_iso or utc_now_iso(), db_path)


# ---------------------------------------------------------------------------
# Durable inflight + reservations
# ---------------------------------------------------------------------------


def _fresh_cutoff_iso(now: datetime | None = None) -> str:
    return _iso(_now_utc(now) - timedelta(minutes=inflight_fresh_minutes()))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def inflight_by_account(
    conn: sqlite3.Connection,
    account_ids: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Durable inflight for MANY accounts in a fixed number of queries.

    Same three components as the single-account read (which delegates here, so
    there is exactly one definition of "inflight"):

    * sessions attributed to the account (live, fresh, not kill-requested);
    * open longhaul attempts whose session row does not already carry the
      attribution (avoids double-counting once the supervisor persists
      ``sessions.account_id`` for longhaul spawns too);
    * unexpired reservations.

    Batched ON PURPOSE (fleet-scale-200). The per-account version issued 3
    counting queries plus 2 ``sqlite_master`` probes for EVERY account, so
    :func:`provider_pressure` cost 5N queries per call and :func:`reserve_account`
    paid that N times *while holding a ``BEGIN IMMEDIATE`` write lock* — i.e. the
    fleet's spawn path serialized behind a transaction whose length grew with the
    account pool. This form is 3 queries + 2 probes regardless of N, so the write
    lock is held for a bounded time no matter how many accounts exist.

    Every requested id is present in the result (0 when it has no inflight).
    """
    ids = list(dict.fromkeys(str(account_id) for account_id in account_ids))
    counts: dict[str, int] = dict.fromkeys(ids, 0)
    if not ids:
        return counts
    reference = _now_utc(now)
    cutoff = _fresh_cutoff_iso(reference)
    states = ",".join("?" * len(_LIVE_SESSION_STATES))
    accounts = ",".join("?" * len(ids))

    def _add(sql: str, params: Sequence[Any]) -> None:
        for row in conn.execute(sql, tuple(params)).fetchall():
            key = str(row["account_id"])
            if key in counts:
                counts[key] += int(row["n"])

    _add(
        "SELECT account_id, COUNT(*) AS n FROM sessions "
        f"WHERE account_id IN ({accounts}) AND state IN ({states}) "
        "AND kill_requested = 0 "
        "AND COALESCE(last_activity_at, updated_at, created_at) >= ? "
        "GROUP BY account_id",
        (*ids, *_LIVE_SESSION_STATES, cutoff),
    )
    if _table_exists(conn, "task_sessions"):
        _add(
            "SELECT ts.account_id AS account_id, COUNT(*) AS n FROM task_sessions ts "
            f"WHERE ts.account_id IN ({accounts}) AND ts.ended_at IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM sessions s WHERE s.id = ts.session_id "
            "  AND (s.account_id IS NOT NULL OR s.kill_requested = 1 "
            f"       OR s.state NOT IN ({states}))"
            ") GROUP BY ts.account_id",
            (*ids, *_LIVE_SESSION_STATES),
        )
    if _table_exists(conn, "account_reservations"):
        _add(
            "SELECT account_id, COUNT(*) AS n FROM account_reservations "
            f"WHERE account_id IN ({accounts}) AND expires_at > ? "
            "GROUP BY account_id",
            (*ids, _iso(reference)),
        )
    return counts


def _account_inflight(
    conn: sqlite3.Connection, account_id: str, *, now: datetime | None = None
) -> int:
    """Durable inflight for one account, on an existing connection.

    Thin wrapper over :func:`inflight_by_account` so the batched and single
    reads can never disagree about what counts."""
    return inflight_by_account(conn, (account_id,), now=now)[str(account_id)]


def inflight_count(
    account_id: str, *, db_path: str | None = None, now: datetime | None = None
) -> int:
    """Durable inflight session count for ``account_id`` (see module docstring)."""
    conn = _connect(db_path or default_db_path())
    try:
        return _account_inflight(conn, account_id, now=now)
    finally:
        conn.close()


@dataclass(frozen=True)
class Reservation:
    """A short-TTL claim on one concurrent-session slot for an account."""

    id: str
    provider: str
    account: SpawnAccount
    expires_at: str


def reserve_account(
    provider: str = DEFAULT_PROVIDER,
    *,
    max_inflight: int | None = None,
    ttl_seconds: float | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
    _connection: sqlite3.Connection | None = None,
) -> Reservation | None:
    """Atomically pick an account AND claim one inflight slot on it.

    Count + reserve happen inside one ``BEGIN IMMEDIATE`` transaction, so two
    slot workers checking N < limit concurrently serialize and exactly one
    wins the last slot. Returns ``None`` when no enabled, non-cooling account
    for ``provider`` has spare capacity.

    ``max_inflight`` defaults to configs/swarm.yaml's per-provider
    ``max_inflight_per_account``. The reservation expires after
    ``ttl_seconds`` (default configs/swarm.yaml ``reservation_ttl_seconds``,
    ~120s): the supervisor persisting ``sessions.account_id`` at launch is the
    hand-off point where :func:`convert_reservation` must be called; a crash
    before that simply lets the TTL reclaim the slot."""
    ceiling = max_inflight if max_inflight is not None else max_inflight_per_account(provider)
    ttl = ttl_seconds if ttl_seconds is not None else reservation_ttl_seconds()
    reference = _now_utc(now)
    now_iso = _iso(reference)
    owns_connection = _connection is None
    conn = _connection if _connection is not None else _connect(db_path or default_db_path())
    try:
        # Predictive-balance probe BEFORE the write transaction: filesystem I/O
        # must not extend the write-lock hold. Preference only — reordering,
        # never filtering, so every pre-feature pick remains reachable.
        predictive_out = predictive_out_ids(conn, provider, now_iso)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Lazy TTL reap: expired claims are dead weight, drop them first.
            conn.execute("DELETE FROM account_reservations WHERE expires_at <= ?", (now_iso,))
            rows = conn.execute(
                f"SELECT * FROM claude_accounts WHERE {AVAILABLE_PREDICATE} {LRU_ORDER}",
                {"provider": provider, "now": now_iso},
            ).fetchall()
            # Deprioritize predictively-out accounts (cached weekly >= 90% used):
            # stable partition preserves LRU order within each half, and when
            # every account is out the order degrades to exactly plain LRU.
            rows = [r for r in rows if str(r["id"]) not in predictive_out] + [
                r for r in rows if str(r["id"]) in predictive_out
            ]
            # ONE batched inflight read for the whole candidate set (after the
            # TTL reap above, so reaped claims are already excluded). The write
            # lock is held for the length of this transaction, so its cost must
            # not scale with the account pool — see inflight_by_account.
            inflight = inflight_by_account(conn, [str(row["id"]) for row in rows], now=reference)
            for row in rows:
                account = dict(row)
                account_id = str(account["id"])
                if ceiling > 0 and inflight.get(account_id, 0) >= ceiling:
                    continue
                # Credential must load BEFORE any durable reservation/LRU write.
                # spawn_account_from_row is non-raising (hollow env on missing
                # oauth/api_key secret). Doing that after INSERT+commit leaves a
                # successful-looking pick with a hollow credential — the
                # governing non-result-as-favourable class. Same stop-the-line
                # as next_account_for_spawn.
                spawn = spawn_account_from_row(account)
                auth_type = account.get("auth_type")
                if auth_type == "oauth_token" and not spawn.env.get("CLAUDE_CODE_OAUTH_TOKEN"):
                    conn.execute(
                        "UPDATE claude_accounts SET enabled = 0, status = ?, "
                        "status_detail = ?, updated_at = ? WHERE id = ?",
                        (
                            "error",
                            f"account {account_id}: oauth secret missing or unreadable",
                            now_iso,
                            account_id,
                        ),
                    )
                    continue
                if auth_type == "api_key" and not spawn.env.get("ANTHROPIC_API_KEY"):
                    conn.execute(
                        "UPDATE claude_accounts SET enabled = 0, status = ?, "
                        "status_detail = ?, updated_at = ? WHERE id = ?",
                        (
                            "error",
                            f"account {account_id}: API key secret missing or unreadable",
                            now_iso,
                            account_id,
                        ),
                    )
                    continue
                reservation_id = new_id("rsv")
                expires_at = _iso(reference + timedelta(seconds=ttl))
                conn.execute(
                    "INSERT INTO account_reservations "
                    "(id, account_id, provider, session_id, created_at, expires_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (reservation_id, account_id, provider, now_iso, expires_at),
                )
                # Advance the rotation cursor exactly like next_account_for_spawn:
                # a reservation IS a pick (credential already verified above).
                conn.execute(
                    "UPDATE claude_accounts SET "
                    "last_used_seq = (SELECT COALESCE(MAX(last_used_seq), 0) + 1 "
                    "                 FROM claude_accounts), "
                    "last_used_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso, now_iso, account_id),
                )
                conn.commit()
                return Reservation(
                    id=reservation_id,
                    provider=provider,
                    account=spawn,
                    expires_at=expires_at,
                )
            conn.commit()
            return None
        except BaseException:
            conn.rollback()
            raise
    finally:
        if owns_connection:
            conn.close()


# D10 UltraCode: the hard cap on concurrent fable-tier sessions per ultra run.
# Distinct claude accounts are PREFERRED (one deep session per subscription);
# fewer available accounts degrade to reuse — never block. Non-claude MAX-tier
# workers do not count against this cap.
ULTRA_DISTINCT_ACCOUNT_CAP = 3


def reserve_distinct_accounts(
    provider: str = DEFAULT_PROVIDER,
    n: int = ULTRA_DISTINCT_ACCOUNT_CAP,
    *,
    exclude_account_ids: Sequence[str] | frozenset[str] | set[str] = (),
    max_inflight: int | None = None,
    ttl_seconds: float | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> list[Reservation]:
    """Atomically reserve up to ``n`` slots, PREFERRING distinct accounts.

    The D10 UltraCode primitive: an ultra run spawns at most
    :data:`ULTRA_DISTINCT_ACCOUNT_CAP` concurrent fable-tier sessions
    (``n`` is clamped there — the cap is per-ultra-run), each landing on a
    DIFFERENT enabled, non-cooling account when possible so no single
    subscription absorbs the whole burst.

    One ``BEGIN IMMEDIATE`` transaction covers the entire loop (the same
    count+reserve serialization as :func:`reserve_account` — two parallel
    reservers cannot over-book an account past its ceiling). Each iteration
    first tries an account not yet claimed by THIS call and not in
    ``exclude_account_ids`` (pass the account ids already carrying a
    fable-tier session for the run, so later top-up calls keep preferring
    untouched accounts); when no distinct account has capacity it DEGRADES to
    reuse — any available account with spare inflight capacity, LRU-first,
    non-excluded accounts before excluded ones. Fewer distinct accounts than
    requested must reduce distinctness, never concurrency, and NEVER block:
    when no account has capacity at all the loop stops early and returns the
    reservations it did get (possibly ``[]``).

    ``max_inflight`` defaults to configs/swarm.yaml's per-provider
    ``max_inflight_per_account``; the account snapshot (availability + LRU
    order) is taken once at entry, so a degrade pass reuses in the snapshot's
    LRU order. Every reservation advances the rotation cursor exactly like
    :func:`reserve_account` and expires after ``ttl_seconds`` unless converted
    (:func:`convert_reservation`) or released (:func:`release_reservation`).
    """
    ceiling = max_inflight if max_inflight is not None else max_inflight_per_account(provider)
    ttl = ttl_seconds if ttl_seconds is not None else reservation_ttl_seconds()
    count = max(0, min(int(n), ULTRA_DISTINCT_ACCOUNT_CAP))
    if count == 0:
        return []
    excluded = {str(account_id) for account_id in exclude_account_ids}
    reference = _now_utc(now)
    now_iso = _iso(reference)
    conn = _connect(db_path or default_db_path())
    try:
        # Predictive-balance probe BEFORE the write transaction (see
        # reserve_account) — reorder, never filter.
        predictive_out = predictive_out_ids(conn, provider, now_iso)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Lazy TTL reap: expired claims are dead weight, drop them first.
            conn.execute("DELETE FROM account_reservations WHERE expires_at <= ?", (now_iso,))
            rows = conn.execute(
                f"SELECT * FROM claude_accounts WHERE {AVAILABLE_PREDICATE} {LRU_ORDER}",
                {"provider": provider, "now": now_iso},
            ).fetchall()
            rows = [r for r in rows if str(r["id"]) not in predictive_out] + [
                r for r in rows if str(r["id"]) in predictive_out
            ]
            accounts = [dict(row) for row in rows]
            # One batched inflight read for the whole snapshot (see
            # reserve_account: the write lock must not be held for a per-account
            # query fan-out). Claims made BY THIS CALL are added back below so
            # they still count against the ceiling exactly like a committed
            # reservation would — the property the previous per-claim re-read
            # got from in-transaction visibility.
            inflight = inflight_by_account(
                conn, [str(account["id"]) for account in accounts], now=reference
            )

            def _claim(account: dict[str, Any]) -> Reservation | None:
                account_id = str(account["id"])
                if ceiling > 0 and inflight.get(account_id, 0) >= ceiling:
                    return None
                # Credential before INSERT/LRU — same contract as reserve_account.
                # On missing secret: quarantine and skip; never consume an
                # inflight slot or advance the successful-pick cursor.
                spawn = spawn_account_from_row(account)
                auth_type = account.get("auth_type")
                if auth_type == "oauth_token" and not spawn.env.get("CLAUDE_CODE_OAUTH_TOKEN"):
                    conn.execute(
                        "UPDATE claude_accounts SET enabled = 0, status = ?, "
                        "status_detail = ?, updated_at = ? WHERE id = ?",
                        (
                            "error",
                            f"account {account_id}: oauth secret missing or unreadable",
                            now_iso,
                            account_id,
                        ),
                    )
                    return None
                if auth_type == "api_key" and not spawn.env.get("ANTHROPIC_API_KEY"):
                    conn.execute(
                        "UPDATE claude_accounts SET enabled = 0, status = ?, "
                        "status_detail = ?, updated_at = ? WHERE id = ?",
                        (
                            "error",
                            f"account {account_id}: API key secret missing or unreadable",
                            now_iso,
                            account_id,
                        ),
                    )
                    return None
                inflight[account_id] = inflight.get(account_id, 0) + 1
                reservation_id = new_id("rsv")
                expires_at = _iso(reference + timedelta(seconds=ttl))
                conn.execute(
                    "INSERT INTO account_reservations "
                    "(id, account_id, provider, session_id, created_at, expires_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (reservation_id, account_id, provider, now_iso, expires_at),
                )
                # Advance the rotation cursor exactly like next_account_for_spawn:
                # a reservation IS a pick (credential already verified above).
                conn.execute(
                    "UPDATE claude_accounts SET "
                    "last_used_seq = (SELECT COALESCE(MAX(last_used_seq), 0) + 1 "
                    "                 FROM claude_accounts), "
                    "last_used_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso, now_iso, account_id),
                )
                return Reservation(
                    id=reservation_id,
                    provider=provider,
                    account=spawn,
                    expires_at=expires_at,
                )

            reservations: list[Reservation] = []
            used_ids: set[str] = set()
            for _ in range(count):
                claimed: Reservation | None = None
                # Distinct-first: untouched by this call AND by the caller.
                for account in accounts:
                    account_id = str(account["id"])
                    if account_id in used_ids or account_id in excluded:
                        continue
                    claimed = _claim(account)
                    if claimed is not None:
                        used_ids.add(account_id)
                        break
                if claimed is None:
                    # DEGRADE (never block): reuse any account with spare
                    # capacity — non-excluded before excluded, LRU-first.
                    ordered = [a for a in accounts if str(a["id"]) not in excluded] + [
                        a for a in accounts if str(a["id"]) in excluded
                    ]
                    for account in ordered:
                        claimed = _claim(account)
                        if claimed is not None:
                            used_ids.add(str(account["id"]))
                            break
                if claimed is None:
                    break  # no capacity anywhere: return what we have
                reservations.append(claimed)
            conn.commit()
            return reservations
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def convert_reservation(
    reservation_id: str, session_id: str, *, db_path: str | None = None
) -> bool:
    """Convert a reservation into real inflight: the supervisor persisted
    ``sessions.account_id`` for ``session_id``, so the sessions row now carries
    the slot and the reservation is deleted. Returns False for an unknown or
    already-reclaimed reservation."""
    conn = _connect(db_path or default_db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "DELETE FROM account_reservations WHERE id = ?", (reservation_id,)
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if cursor.rowcount != 1:
            LOG.debug(
                "reservation %s already reclaimed before conversion to %s",
                reservation_id,
                session_id,
            )
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_reservation(
    reservation_id: str,
    *,
    db_path: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> bool:
    """Free a reserved slot after a spawn failure (the TTL is only the backstop
    for crashes -- deliberate failure paths release immediately)."""
    owns_connection = _connection is None
    conn = _connection if _connection is not None else _connect(db_path or default_db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "DELETE FROM account_reservations WHERE id = ?", (reservation_id,)
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return cursor.rowcount == 1
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Pressure
# ---------------------------------------------------------------------------


def all_cooling(
    provider: str = DEFAULT_PROVIDER,
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    _connection: sqlite3.Connection | None = None,
) -> bool:
    """True when every ENABLED account for ``provider`` is cooling, or when no
    account is enabled at all -- either way: stop retrying accounts, fall back
    to the next model lineage / park for capacity."""
    now_iso = _iso(_now_utc(now))
    owns_connection = _connection is None
    conn = _connection if _connection is not None else _connect(db_path or default_db_path())
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM claude_accounts WHERE {AVAILABLE_PREDICATE}",
            {"provider": provider, "now": now_iso},
        ).fetchone()
        return int(row["n"]) == 0
    finally:
        if owns_connection:
            conn.close()


def provider_pressure(
    provider: str = DEFAULT_PROVIDER,
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    _connection: sqlite3.Connection | None = None,
) -> float:
    """Backpressure metric in [0.0, 1.0] the router biases AWAY from BEFORE
    the 429 arrives.

        pressure = min(1.0, cooling_fraction + saturation_fraction)

    ``cooling_fraction``    = unavailable enabled accounts / enabled accounts
                              (unavailable = provider cooldown OR operator pause)
    ``saturation_fraction`` = total durable inflight (incl. reservations)
                              / (enabled accounts x max_inflight_per_account)

    No enabled accounts -> 1.0 (a provider with nothing to give is fully
    pressured, matching ``all_cooling``'s fallback signal).

    Called on the routing hot path (once per provider per ``swarm_headroom``
    evaluation), so the inflight side is ONE batched read rather than a query
    fan-out over the account pool."""
    reference = _now_utc(now)
    now_iso = _iso(reference)
    owns_connection = _connection is None
    conn = _connection if _connection is not None else _connect(db_path or default_db_path())
    try:
        rows = conn.execute(
            "SELECT id, cooldown_until, paused_until FROM claude_accounts "
            "WHERE enabled = 1 AND provider = ?",
            (provider,),
        ).fetchall()
        if not rows:
            return 1.0
        enabled = len(rows)
        cooling = 0
        for row in rows:
            # An operator pause removes capacity exactly as a cooldown does, so
            # the router must feel it as backpressure -- otherwise it keeps
            # biasing traffic toward a provider whose fleet the operator just
            # shrank, and only discovers the shortfall at spawn time.
            windows = (_parse_iso(row["cooldown_until"]), _parse_iso(row["paused_until"]))
            if any(until is not None and _iso(until) > now_iso for until in windows):
                cooling += 1
        inflight_total = sum(
            inflight_by_account(conn, [str(row["id"]) for row in rows], now=reference).values()
        )
        cooling_fraction = cooling / enabled
        capacity = enabled * max_inflight_per_account(provider)
        saturation_fraction = (inflight_total / capacity) if capacity > 0 else 0.0
        return min(1.0, cooling_fraction + saturation_fraction)
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Fleet session budget (WP5a seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetAvailability:
    """Live fleet-budget snapshot the WP5a scheduler consumes.

    ``available_global``    slots any lane may take right now.
    ``available_for_swarm`` slots swarm runs may take: swarm sessions may only
                            ever grow the fleet to (cap - reserved), keeping
                            ``reserved_small_task_slots`` of guaranteed headroom
                            for small/fast/longhaul tasks at all times.
    """

    total_live: int
    swarm_live: int
    max_sessions_global: int
    reserved_small_task_slots: int
    available_global: int
    available_for_swarm: int
    # PKG-INSESSION-FANOUT (additive): children actually spawned under live
    # grants. They consume no session slot (they live inside one process) but
    # they ARE real concurrent agents — surfaced here so no consumer of this
    # snapshot can mistake process count for agent count.
    insession_children_live: int = 0


def count_live_sessions(*, db_path: str | None = None, now: datetime | None = None) -> int:
    """Fleet-wide live session count (starting/running, not kill-requested,
    activity-fresh) -- same zombie exclusions as per-account inflight."""
    cutoff = _fresh_cutoff_iso(_now_utc(now))
    placeholders = ",".join("?" * len(_LIVE_SESSION_STATES))
    conn = _connect(db_path or default_db_path())
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM sessions WHERE state IN ({placeholders}) "
            "AND kill_requested = 0 "
            "AND COALESCE(last_activity_at, updated_at, created_at) >= ?",
            (*_LIVE_SESSION_STATES, cutoff),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def count_live_swarm_sessions(*, db_path: str | None = None) -> int:
    """Open swarm attempts (``swarm_attempts.ended_at IS NULL``) -- the swarm
    share of the live fleet. Zero when the swarm tables are absent."""
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "swarm_attempts"):
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM swarm_attempts WHERE ended_at IS NULL"
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def fleet_available(
    max_sessions_global: int | None = None,
    reserved_small_task_slots: int | None = None,
    *,
    total_live: int | None = None,
    swarm_live: int | None = None,
    db_path: str | None = None,
) -> FleetAvailability:
    """The global session-budget ledger (fleet capacity model).

    Pure math over the two live counts; each count is read from the DB only
    when not supplied, so the WP5a scheduler can unit-test the arithmetic and
    production can pass nothing. Config defaults come from configs/swarm.yaml
    (``max_sessions_global``, ``reserved_small_task_slots``)."""
    config = load_swarm_config()
    if max_sessions_global is None:
        max_sessions_global = int(
            _as_float(config.get("max_sessions_global"), _DEFAULT_MAX_SESSIONS_GLOBAL)
        )
    if reserved_small_task_slots is None:
        reserved_small_task_slots = int(
            _as_float(config.get("reserved_small_task_slots"), _DEFAULT_RESERVED_SMALL_TASK_SLOTS)
        )
    if total_live is None:
        total_live = count_live_sessions(db_path=db_path)
    if swarm_live is None:
        swarm_live = count_live_swarm_sessions(db_path=db_path)
    # PKG-INSESSION-FANOUT truth field (lazy import: swarm.insession imports
    # this module at load). Best-effort — the budget arithmetic below is
    # process-based on purpose (children take no session slot); this count
    # only keeps the snapshot honest about total live agents.
    insession_children_live = 0
    try:
        from omniagentos.swarm.insession import live_children_total

        insession_children_live = live_children_total(db_path=db_path)
    except Exception:  # noqa: BLE001 -- the session ledger must not depend on the feature module.
        insession_children_live = 0
    available_global = max(0, max_sessions_global - total_live)
    swarm_ceiling = max(0, max_sessions_global - reserved_small_task_slots)
    available_for_swarm = max(0, min(available_global, swarm_ceiling - swarm_live))
    return FleetAvailability(
        total_live=total_live,
        swarm_live=swarm_live,
        max_sessions_global=max_sessions_global,
        reserved_small_task_slots=reserved_small_task_slots,
        available_global=available_global,
        available_for_swarm=available_for_swarm,
        insession_children_live=insession_children_live,
    )


__all__ = [
    "DEFAULT_PROVIDER",
    "OUTCOMES",
    "OUTCOME_AUTH_ERROR",
    "OUTCOME_OK",
    "OUTCOME_OVERLOADED",
    "OUTCOME_QUOTA_EXHAUSTED",
    "OUTCOME_TRANSIENT_RATE_LIMIT",
    "ULTRA_DISTINCT_ACCOUNT_CAP",
    "FleetAvailability",
    "Reservation",
    "account_id_for_config_dir",
    "all_cooling",
    "clear_expired_cooldowns",
    "convert_reservation",
    "count_live_sessions",
    "count_live_swarm_sessions",
    "durable_cooldown_remaining",
    "enabled_providers",
    "fleet_available",
    "inflight_by_account",
    "inflight_count",
    "increment_weekly_attempts",
    "interactive_reserve_attempts",
    "list_available_accounts",
    "load_swarm_config",
    "max_agents_per_account",
    "max_inflight_per_account",
    "pick_account",
    "provider_pressure",
    "release_reservation",
    "report_outcome",
    "reserve_account",
    "reserve_distinct_accounts",
    "reservation_ttl_seconds",
    "swarm_config_path",
    "weekly_attempt_quota_default",
    "weekly_attempts_quota_remaining",
]
