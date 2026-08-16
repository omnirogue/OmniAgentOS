"""PKG-INSESSION-FANOUT: coordinator-issued grants for in-session fan-out.

A grant authorizes ONE running swarm attempt (claude bridge session) to run up
to ``max_children`` of its validated subtasks as live subagents via the Task
tool, instead of paying a fresh CLI process per child. It is the DURABLE,
first-class record of pre-authorized in-session parallelism:

* issued only by the swarm coordinator's await loop, after the SAME six guards
  the settle-time split runs (``scheduler._validate_subtasks_payload``) plus
  the per-account AGENT capacity check below;
* consumed one child at a time by the PreToolUse hook
  (``api.routes.sessions._hook_eval_decide``) — the consume is a single atomic
  UPDATE bounded by the budget, the TTL, the void marker, AND the attempt
  still being open, so a dead or settled attempt can never spawn;
* voided at attempt close (``SwarmDal.close_attempt``) — belt; the TTL and the
  attempt-liveness join are the braces.

Accounting: a live grant's ``max_children`` counts as COMMITTED agent capacity
on its account (sessions + live grant budgets ≤ ``max_agents_per_account``).
Counting the committed budget rather than ``children_spawned`` is deliberate —
admission must hold what it authorized, not what happens to have started yet.

The parent attempt stays the single verify/review/merge unit (attempt ==
branch == review == merge is load-bearing in the settle pipeline), so a
CONSUMED grant makes settle skip the card-split — the children's work already
happened inside this attempt — while an UNCONSUMED grant is voided and today's
process-per-child flow runs byte-identically.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.contracts import default_db_path, new_id
from omniagentos.db.store import _connect
from omniagentos.routing.limit_state import (
    inflight_by_account,
    load_swarm_config,
    max_agents_per_account,
)

LOG = logging.getLogger(__name__)

# Hard bounds for the per-grant child budget: the request guard admits 2-4
# children, so a grant can never usefully hold more than 4 slots.
_MIN_CHILDREN = 1
_MAX_CHILDREN = 4
_DEFAULT_MAX_CHILDREN = 4
_DEFAULT_GRANT_TTL_SECONDS = 1800.0
_DEFAULT_GRANT_WAIT_SECONDS = 90

# The grant file the coordinator writes beside the worker's request file; the
# worker polls for it after writing subtasks_request.<attempt>.json.
GRANT_FILE_PREFIX = "subtasks_grant."
REQUEST_FILE_PREFIX = "subtasks_request."


def _insession_section() -> dict[str, Any]:
    config = load_swarm_config()
    section = config.get("insession")
    return section if isinstance(section, dict) else {}


def insession_enabled() -> bool:
    """Master switch (configs/swarm.yaml ``insession.enabled``, DARK default).

    ``OMNIAGENTOS_INSESSION_FANOUT`` overrides in BOTH directions for drills
    (the ``OMNIAGENTOS_SWARM_WORKTREES`` idiom)."""
    override = os.environ.get("OMNIAGENTOS_INSESSION_FANOUT")
    if override is not None and override.strip() != "":
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_insession_section().get("enabled", False))


def insession_max_children() -> int:
    """Per-grant child budget, clamped to the request guard's own bounds."""
    raw = _insession_section().get("max_children", _DEFAULT_MAX_CHILDREN)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_CHILDREN
    return max(_MIN_CHILDREN, min(_MAX_CHILDREN, value))


def grant_ttl_seconds() -> float:
    raw = _insession_section().get("grant_ttl_seconds", _DEFAULT_GRANT_TTL_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_GRANT_TTL_SECONDS
    return value if value > 0 else _DEFAULT_GRANT_TTL_SECONDS


def grant_wait_seconds() -> int:
    """How long the worker protocol tells it to wait for a grant."""
    raw = _insession_section().get("grant_wait_seconds", _DEFAULT_GRANT_WAIT_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_GRANT_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_GRANT_WAIT_SECONDS


def grant_path_for_request(request_path: str) -> str:
    """``subtasks_request.<attempt>.json`` → sibling ``subtasks_grant.<attempt>.json``.

    Pure string mapping so every consumer (protocol text, coordinator writer,
    spawn relay) derives the SAME path from the one value already threaded."""
    head, sep, tail = request_path.rpartition(REQUEST_FILE_PREFIX)
    if not sep:  # not an attempt-bound request path; fail loud at the caller
        raise ValueError(f"not a subtasks request path: {request_path!r}")
    return f"{head}{GRANT_FILE_PREFIX}{tail}"


def _now_utc(now: datetime | None = None) -> datetime:
    return now.astimezone(UTC) if now is not None else datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


# A grant is LIVE while unvoided and unexpired; every predicate below inlines
# these two clauses so a reader and the consume UPDATE can never disagree.
_LIVE_GRANT_WHERE = "voided_at IS NULL AND expires_at > :now"


@dataclass(frozen=True)
class InsessionGrant:
    """One coordinator-issued in-session fan-out authorization."""

    id: str
    swarm_run_id: str
    board_task_id: str
    attempt_id: str
    session_id: str
    provider: str
    account_id: str | None
    max_children: int
    children_spawned: int
    children: tuple[Mapping[str, Any], ...]
    created_at: str
    expires_at: str
    voided_at: str | None
    void_reason: str | None


def _grant_from_row(row: sqlite3.Row | Mapping[str, Any]) -> InsessionGrant:
    try:
        children_raw = json.loads(str(row["children_json"]) or "[]")
    except (json.JSONDecodeError, TypeError):
        children_raw = []
    children = tuple(item for item in children_raw if isinstance(item, Mapping))
    return InsessionGrant(
        id=str(row["id"]),
        swarm_run_id=str(row["swarm_run_id"]),
        board_task_id=str(row["board_task_id"]),
        attempt_id=str(row["attempt_id"]),
        session_id=str(row["session_id"]),
        provider=str(row["provider"]),
        account_id=(str(row["account_id"]) if row["account_id"] is not None else None),
        max_children=int(row["max_children"]),
        children_spawned=int(row["children_spawned"]),
        children=children,
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        voided_at=(str(row["voided_at"]) if row["voided_at"] is not None else None),
        void_reason=(str(row["void_reason"]) if row["void_reason"] is not None else None),
    )


def _live_budget_by_account(
    conn: sqlite3.Connection, account_ids: Sequence[str], *, now_iso: str
) -> dict[str, int]:
    """Σ max_children of LIVE grants per account (committed agent capacity)."""
    ids = list(dict.fromkeys(str(a) for a in account_ids))
    totals: dict[str, int] = dict.fromkeys(ids, 0)
    if not ids or not _table_exists(conn, "insession_grants"):
        return totals
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT account_id, SUM(max_children) AS n FROM insession_grants "
        f"WHERE account_id IN ({placeholders}) AND {_LIVE_GRANT_WHERE.replace(':now', '?')} "
        "GROUP BY account_id",
        (*ids, now_iso),
    ).fetchall()
    for row in rows:
        key = str(row["account_id"])
        if key in totals:
            totals[key] = int(row["n"] or 0)
    return totals


def create_grant(
    *,
    swarm_run_id: str,
    board_task_id: str,
    attempt_id: str,
    session_id: str,
    provider: str,
    account_id: str | None,
    children: Sequence[Mapping[str, Any]],
    db_path: str | None = None,
    now: datetime | None = None,
) -> tuple[InsessionGrant | None, str | None]:
    """Atomically admit + record one grant; returns ``(grant, None)`` or
    ``(None, deny_reason)``.

    One ``BEGIN IMMEDIATE`` transaction covers the capacity read AND the
    insert (the ``reserve_account`` idiom): two coordinators granting against
    the same account serialize, so the agent ceiling cannot be over-committed
    by a read/insert race. Capacity = live session inflight (the SAME
    definition ``reserve_account`` uses) + Σ live grant budgets; the grant is
    admitted only when that plus ``len(children)`` fits under
    ``max_agents_per_account``. ``UNIQUE(attempt_id)`` makes a re-grant of the
    same attempt structurally impossible.
    """
    count = len(children)
    budget = min(count, insession_max_children())
    if count < 2 or budget < count:
        # The guards admit 2-4 children; a config clamp below the validated
        # count means the operator narrowed the budget — deny rather than
        # silently granting fewer children than the request was validated for.
        return None, "child_budget"
    if not account_id:
        # Agent capacity is an ACCOUNT ledger; a session with no attribution
        # has nothing to charge, and an uncharged grant would be the exact
        # "fleet_available lies" failure this package exists to close.
        return None, "no_account"
    reference = _now_utc(now)
    now_iso = _iso(reference)
    expires_at = _iso(reference + timedelta(seconds=grant_ttl_seconds()))
    ceiling = max_agents_per_account(provider)
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return None, "migration_missing"
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Lazy reap (the reservation idiom): expired grants are dead
            # accounting weight — void them so they stop holding capacity.
            conn.execute(
                "UPDATE insession_grants SET voided_at = ?, void_reason = 'expired' "
                "WHERE voided_at IS NULL AND expires_at <= ?",
                (now_iso, now_iso),
            )
            sessions_inflight = inflight_by_account(conn, (account_id,), now=reference)[
                str(account_id)
            ]
            granted = _live_budget_by_account(conn, (account_id,), now_iso=now_iso)[str(account_id)]
            if sessions_inflight + granted + budget > ceiling:
                conn.commit()  # keep the lazy reap
                return None, "agent_capacity"
            grant_id = new_id("isg")
            try:
                conn.execute(
                    "INSERT INTO insession_grants "
                    "(id, swarm_run_id, board_task_id, attempt_id, session_id, "
                    "provider, account_id, max_children, children_spawned, "
                    "children_json, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        grant_id,
                        swarm_run_id,
                        board_task_id,
                        attempt_id,
                        session_id,
                        provider,
                        account_id,
                        budget,
                        json.dumps(list(children), separators=(",", ":"), sort_keys=True),
                        now_iso,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return None, "already_granted"
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        row = conn.execute("SELECT * FROM insession_grants WHERE id = ?", (grant_id,)).fetchone()
        return (_grant_from_row(row) if row is not None else None), None
    finally:
        conn.close()


def consume_child_slot(
    session_id: str,
    *,
    db_path: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """One atomic child-slot consume for the PreToolUse Task gate.

    Allowed iff the session's grant is live (unvoided, unexpired), has budget
    remaining, AND its attempt is still open — all four conditions inside the
    single UPDATE, so no interleaving of settle/void/expiry with the hook can
    over-spawn. Returns ``(allowed, reason)``; the reason names the FIRST
    failing condition for the deny event (best-effort diagnosis, read after
    the fact)."""
    now_iso = _iso(_now_utc(now))
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return False, "no_grant"
        cursor = conn.execute(
            "UPDATE insession_grants "
            "SET children_spawned = children_spawned + 1 "
            "WHERE session_id = :session_id "
            f"AND {_LIVE_GRANT_WHERE} "
            "AND children_spawned < max_children "
            "AND EXISTS (SELECT 1 FROM swarm_attempts a "
            "            WHERE a.id = insession_grants.attempt_id "
            "            AND a.ended_at IS NULL)",
            {"session_id": session_id, "now": now_iso},
        )
        conn.commit()
        if cursor.rowcount == 1:
            return True, "granted"
        row = conn.execute(
            "SELECT * FROM insession_grants WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return False, "no_grant"
        if row["voided_at"] is not None:
            return False, "grant_voided"
        if str(row["expires_at"]) <= now_iso:
            return False, "grant_expired"
        if int(row["children_spawned"]) >= int(row["max_children"]):
            return False, "budget_exhausted"
        return False, "attempt_ended"
    finally:
        conn.close()


def grant_for_attempt(attempt_id: str, *, db_path: str | None = None) -> InsessionGrant | None:
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return None
        row = conn.execute(
            "SELECT * FROM insession_grants WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _grant_from_row(row) if row is not None else None
    finally:
        conn.close()


def void_grant_for_attempt(
    attempt_id: str,
    reason: str,
    *,
    db_path: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Idempotent CAS void (the ``close_attempt`` idiom): True iff THIS call
    voided a previously-live grant."""
    now_iso = _iso(_now_utc(now))
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return False
        cursor = conn.execute(
            "UPDATE insession_grants SET voided_at = ?, void_reason = ? "
            "WHERE attempt_id = ? AND voided_at IS NULL",
            (now_iso, reason[:200], attempt_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def live_grant_budget_total(
    provider: str = "claude",
    *,
    db_path: str | None = None,
    now: datetime | None = None,
) -> int:
    """Σ committed budgets of live grants for ``provider`` (preflight usage)."""
    now_iso = _iso(_now_utc(now))
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return 0
        row = conn.execute(
            "SELECT COALESCE(SUM(max_children), 0) AS n FROM insession_grants "
            f"WHERE provider = :provider AND {_LIVE_GRANT_WHERE}",
            {"provider": provider, "now": now_iso},
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def live_children_total(*, db_path: str | None = None, now: datetime | None = None) -> int:
    """Children actually spawned under live grants (``fleet_available``'s
    additive truth field)."""
    now_iso = _iso(_now_utc(now))
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "insession_grants"):
            return 0
        row = conn.execute(
            "SELECT COALESCE(SUM(children_spawned), 0) AS n FROM insession_grants "
            f"WHERE {_LIVE_GRANT_WHERE}",
            {"now": now_iso},
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


__all__ = [
    "GRANT_FILE_PREFIX",
    "REQUEST_FILE_PREFIX",
    "InsessionGrant",
    "consume_child_slot",
    "create_grant",
    "grant_for_attempt",
    "grant_path_for_request",
    "grant_ttl_seconds",
    "grant_wait_seconds",
    "insession_enabled",
    "insession_max_children",
    "live_children_total",
    "live_grant_budget_total",
    "void_grant_for_attempt",
]
