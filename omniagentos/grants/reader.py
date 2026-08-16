"""Read holder capability grants from the store for brief inclusion.

This module provides utilities to query agent_capabilities for a given holder
(canonical lane identity) and format them for inclusion in worker briefs.
The store is the single source of truth for held capabilities; hardcoding
capability lists is forbidden.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore


def _as_datetime(value: str | None) -> datetime | None:
    """Parse ISO-8601 datetime string to UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_expired(expires_at: str | None, now: str | None = None) -> bool:
    """Check if a grant has expired.

    PLAN.md §1 defines three grant durations:
    - Standing (NULL expires_at): valid until revoked — NOT expired
    - Run (6h broker token): non-NULL expires_at checked against now
    - In-run delta (TTL 1800s): non-NULL expires_at checked against now

    Returns True if expired, False if still valid.
    """
    # NULL expires_at = STANDING grant = valid until revoked (not expired)
    if expires_at is None or expires_at == "":
        return False

    # Non-NULL expires_at: check if in the past
    expiry = _as_datetime(expires_at)
    moment = _as_datetime(now or utc_now_iso())

    # Unparseable timestamp = fail closed (treat as expired)
    if expiry is None or moment is None:
        return True

    # Expired if now >= expiry
    return moment >= expiry


def _read_holder_capabilities(
    holder: str,
    *,
    db_path: str | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Read live capabilities for a holder from agent_capabilities table.

    Returns (capabilities, error_reason) where:
    - capabilities: list of (capability_id, mode) tuples, filtered for non-expired
    - error_reason: None if successful, or a string explaining the degradation
                    (e.g., "store_unavailable", "db_read_failed")

    A live capability is one with:
    - expires_at is NULL (STANDING grant, valid until revoked), OR
    - expires_at is non-NULL and not yet reached in time
    """
    if not holder or not isinstance(holder, str):
        return [], "invalid_holder"

    db_path = db_path or default_db_path()
    store = None
    try:
        store = SqliteStore(db_path)
        rows = store._connection.execute(
            """
            SELECT capability_id, mode, expires_at
            FROM agent_capabilities
            WHERE agent_id = ?
            ORDER BY capability_id ASC
            """,
            (holder,),
        ).fetchall()

        now = utc_now_iso()
        live_caps = []
        for row in rows:
            cap_id, mode, expires_at = row
            if not _is_expired(expires_at, now):
                live_caps.append((str(cap_id), str(mode or "read")))

        return live_caps, None
    except Exception:  # noqa: BLE001 - store read failure is non-blocking
        # Grant store is unavailable; brief construction must continue
        # with graceful degradation
        return [], "store_unavailable"
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def format_grant_lines(
    holder: str | Sequence[str],
    *,
    db_path: str | None = None,
) -> list[str]:
    """Format capability grant lines for inclusion in a brief.

    ``holder`` is one canonical identity or an ordered sequence of them. A
    sequence is how a lane that has BOTH a general and a qualified spelling
    (`lane:swarm.worker` and `lane:swarm.worker.<formation>`, PLAN.md §1
    invariant 1) reads what it actually holds: the union across the spellings,
    deduplicated by capability id, with the FIRST spelling that carries a
    capability winning the mode it is reported under. Order the sequence
    general-first so the common floor is the reported mode and a
    formation-scoped row can only add.

    Returns a list of strings suitable for appending to a worker brief:
    - If the holder set has grants: a "## Standing capability grants" section
      listing capability_id + mode
    - If it has none: explicit "You hold no standing grants" line + request path
    - If EVERY read degraded: "grant status unknown" line + request path. One
      readable spelling is enough to answer honestly, so a partial failure is
      not reported as an outage.
    """
    holders = [holder] if isinstance(holder, str) else [str(h) for h in holder]

    merged: list[tuple[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []
    read_any = False
    for identity in holders:
        capabilities, error = _read_holder_capabilities(identity, db_path=db_path)
        if error is not None:
            errors.append(error)
            continue
        read_any = True
        for cap_id, mode in capabilities:
            if cap_id in seen:
                continue
            seen.add(cap_id)
            merged.append((cap_id, mode))

    lines = ["", "## Standing capability grants"]
    if not read_any:
        # Graceful degradation: every spelling's store read failed.
        reason = errors[0] if errors else "invalid_holder"
        lines.append(f"- Grant status unknown (reason: {reason}) — request access at:")
        lines.append("  `/var/requests/capability_request.md`")
        return lines

    if merged:
        # List each held capability with its mode
        for cap_id, mode in sorted(merged):
            lines.append(f"- {cap_id} (mode: {mode})")
        return lines

    # No grants held by this lane
    lines.append("- You currently hold no standing capability grants.")
    lines.append("- To request access, write to: `/var/requests/capability_request.md`")
    return lines
