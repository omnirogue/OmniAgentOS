"""Per-event emission cap + retention sweep for notifications.

Gated by ``OMNIAGENTOS_LIFECYCLE_RECONCILE_MODE`` (shared lifecycle hygiene flag)
with optional overrides:

* ``OMNIAGENTOS_NOTIF_CAP_N`` (default 20)
* ``OMNIAGENTOS_NOTIF_CAP_WINDOW_SECONDS`` (default 3600)
* ``OMNIAGENTOS_NOTIF_RETENTION_DAYS`` (default 30)
* ``OMNIAGENTOS_NOTIF_RETENTION_BATCH`` (default 200)

Shadow mode logs would-be suppressions/deletes without writes.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.intake.run_card_reconcile import lifecycle_reconcile_mode

__all__ = [
    "EmissionCap",
    "RetentionSweepResult",
    "cap_config",
    "check_emission_cap",
    "maybe_run_retention_sweep",
    "retention_config",
    "run_retention_sweep",
    "suppression_counts",
]

LOG = logging.getLogger(__name__)

_LOCK = threading.RLock()
_EVENTS: dict[str, deque[float]] = defaultdict(deque)
_SUPPRESSED: dict[str, int] = defaultdict(int)
# Throttle opportunistic production sweeps so record paths stay cheap.
_LAST_SWEEP_AT: float = 0.0
_DEFAULT_SWEEP_MIN_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class CapConfig:
    n: int = 20
    window_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    retention_days: int = 30
    batch_size: int = 200


@dataclass
class EmissionCap:
    allowed: bool
    suppressed: bool
    mode: str
    event_key: str
    count_in_window: int
    suppression_total: int


@dataclass
class RetentionSweepResult:
    """Outcome of a retention sweep.

    ``status`` distinguishes genuinely empty success from unmeasured failure:

    - ``"ok"`` — measured outcome (including zero eligible rows, or truthful deletes)
    - ``"failed"`` — eligibility or deletion could not be measured / completed
      (no read capability, malformed timestamps, no delete capability,
      execute/commit error, unreported/invalid custom-deleter count, unknown
      rowcount)

    Callers must not treat ``deleted == 0`` alone as success: an unreadable
    source or a failed delete also reports ``deleted == 0``.
    """

    mode: str
    would_delete: int = 0
    deleted: int = 0
    batches: int = 0
    ids: list[str] = field(default_factory=list)
    status: str = "ok"
    error: str | None = None


def cap_config(env: Mapping[str, str] | None = None) -> CapConfig:
    source = env if env is not None else os.environ
    return CapConfig(
        n=max(1, _env_int(source, "OMNIAGENTOS_NOTIF_CAP_N", 20)),
        window_seconds=max(1, _env_int(source, "OMNIAGENTOS_NOTIF_CAP_WINDOW_SECONDS", 3600)),
    )


def retention_config(env: Mapping[str, str] | None = None) -> RetentionConfig:
    source = env if env is not None else os.environ
    return RetentionConfig(
        retention_days=max(1, _env_int(source, "OMNIAGENTOS_NOTIF_RETENTION_DAYS", 30)),
        batch_size=max(1, _env_int(source, "OMNIAGENTOS_NOTIF_RETENTION_BATCH", 200)),
    )


def _env_int(source: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(source.get(name, default)))
    except (TypeError, ValueError):
        return default


def check_emission_cap(
    event_key: str,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    mode: str | None = None,
) -> EmissionCap:
    """Return whether an emission for ``event_key`` is allowed under the cap."""
    resolved = mode if mode is not None else lifecycle_reconcile_mode(env)
    cfg = cap_config(env)
    if resolved == "off":
        return EmissionCap(
            allowed=True,
            suppressed=False,
            mode="off",
            event_key=event_key,
            count_in_window=0,
            suppression_total=int(_SUPPRESSED.get(event_key, 0)),
        )

    moment = (now or datetime.now(UTC)).timestamp()
    with _LOCK:
        bucket = _EVENTS[event_key]
        cutoff = moment - cfg.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        count = len(bucket)
        if count >= cfg.n:
            if resolved == "shadow":
                LOG.info(
                    "notif cap shadow: would suppress event_key=%s count=%s n=%s",
                    event_key,
                    count,
                    cfg.n,
                )
                return EmissionCap(
                    allowed=True,  # shadow does not suppress
                    suppressed=False,
                    mode="shadow",
                    event_key=event_key,
                    count_in_window=count,
                    suppression_total=int(_SUPPRESSED[event_key]),
                )
            _SUPPRESSED[event_key] += 1
            return EmissionCap(
                allowed=False,
                suppressed=True,
                mode="enforce",
                event_key=event_key,
                count_in_window=count,
                suppression_total=int(_SUPPRESSED[event_key]),
            )
        bucket.append(moment)
        return EmissionCap(
            allowed=True,
            suppressed=False,
            mode=resolved,
            event_key=event_key,
            count_in_window=count + 1,
            suppression_total=int(_SUPPRESSED[event_key]),
        )


def suppression_counts() -> dict[str, int]:
    with _LOCK:
        return dict(_SUPPRESSED)


def reset_cap_state() -> None:
    """Test helper: clear in-process cap counters and retention throttle."""
    global _LAST_SWEEP_AT
    with _LOCK:
        _EVENTS.clear()
        _SUPPRESSED.clear()
        _LAST_SWEEP_AT = 0.0


def run_retention_sweep(
    dal: Any,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    mode: str | None = None,
) -> RetentionSweepResult:
    """Delete/archive notifications older than the retention window in batches."""
    resolved = mode if mode is not None else lifecycle_reconcile_mode(env)
    if resolved == "off":
        return RetentionSweepResult(mode="off")
    cfg = retention_config(env)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = moment - timedelta(days=cfg.retention_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Prefer a DAL helper; fall back to raw connection.
    list_outcome = _list_old_ids(dal, cutoff_iso, limit=cfg.batch_size * 5)
    if list_outcome.ids is None:
        # Could not measure eligible rows. A missing/unreadable source must not
        # present as a favourable empty success (status=ok, deleted=0).
        return RetentionSweepResult(
            mode=resolved,
            would_delete=0,
            deleted=0,
            batches=0,
            ids=[],
            status="failed",
            error=list_outcome.reason or "no_read_capability",
        )
    old_ids = list_outcome.ids
    if resolved == "shadow":
        LOG.info(
            "notif retention shadow: would delete %d rows older than %s",
            len(old_ids),
            cutoff_iso,
        )
        return RetentionSweepResult(
            mode="shadow",
            would_delete=len(old_ids),
            deleted=0,
            batches=0,
            ids=list(old_ids),
        )

    deleted = 0
    batches = 0
    confirmed_ids: list[str] = []
    status = "ok"
    error: str | None = None
    for i in range(0, len(old_ids), cfg.batch_size):
        batch = old_ids[i : i + cfg.batch_size]
        if not batch:
            break
        # Count only rows the delete path truthfully confirms. An unmeasured
        # outcome (None) is NOT the same as measured zero — surface status.
        outcome = _delete_ids(dal, batch)
        if outcome.deleted is None:
            # Eligible rows found but deletion could not run / be measured.
            # Do not collapse this into a favourable empty success.
            status = "failed"
            error = outcome.reason or "delete_unmeasured"
            break
        removed = outcome.deleted
        if removed <= 0:
            continue
        deleted += removed
        batches += 1
        # Name only fully confirmed batches. A partial rowcount cannot identify
        # which of the batch ids actually vanished, and a skipped earlier batch
        # must never appear as a prefix of ``ids`` either.
        if removed == len(batch):
            confirmed_ids.extend(batch)
    return RetentionSweepResult(
        mode="enforce",
        would_delete=0,
        deleted=deleted,
        batches=batches,
        ids=confirmed_ids,
        status=status,
        error=error,
    )


def maybe_run_retention_sweep(
    dal: Any,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    mode: str | None = None,
    min_interval_seconds: float | None = None,
) -> RetentionSweepResult | None:
    """Production entry: run retention at most once per interval (or skip).

    Returns ``None`` when throttled or when mode is off and nothing runs.
    Callers (e.g. :func:`omniagentos.notifications.service.record_notification_result`)
    must treat failures as non-fatal — hygiene never breaks emission.
    """
    global _LAST_SWEEP_AT
    resolved = mode if mode is not None else lifecycle_reconcile_mode(env)
    if resolved == "off":
        return RetentionSweepResult(mode="off")
    moment = (now or datetime.now(UTC)).timestamp()
    interval = (
        float(min_interval_seconds)
        if min_interval_seconds is not None
        else _DEFAULT_SWEEP_MIN_INTERVAL_SECONDS
    )
    with _LOCK:
        if moment - _LAST_SWEEP_AT < interval:
            return None
        _LAST_SWEEP_AT = moment
    return run_retention_sweep(dal, now=now, env=env, mode=resolved)


@dataclass(frozen=True, slots=True)
class _ListOutcome:
    """Measured candidate ids, or an explicit unreadable/unparseable failure.

    ``ids is None`` means eligibility could not be measured — callers must check
    ``is None`` explicitly (never bare truthiness / empty-list collapse) so an
    unreadable source does not silently become a favourable empty success.
    """

    ids: list[str] | None
    reason: str | None = None


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; raise ValueError when unparseable."""
    raw = value.strip()
    if not raw:
        raise ValueError("empty timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _list_old_ids(dal: Any, cutoff_iso: str, *, limit: int) -> _ListOutcome:
    """List aged notification ids eligible for retention deletion.

    Unread operator notifications (``read_at IS NULL``) are never eligible —
    only aged *and already-read* rows may be deleted. Mirrors
    :func:`omniagentos.maintenance.cache_gc` keep-unread semantics.

    Returns ``ids=None`` when the source cannot be read or a row's
    ``created_at`` / ``read_at`` cannot be parsed — never collapse those into
    an empty list (empty list is only for a measured source with zero eligible
    rows). Unparseable ``read_at`` is not favourable "already-read" eligibility.
    """
    connection = getattr(dal, "_connection", None)
    if connection is None:
        lister = getattr(dal, "list", None)
        if not callable(lister):
            # Missing read capability is not a genuinely empty feed.
            return _ListOutcome(ids=None, reason="no_read_capability")
        try:
            rows = lister(limit=max(limit, 1000))
        except Exception:  # noqa: BLE001 - list failure is unmeasured, not empty
            return _ListOutcome(ids=None, reason="list_failed")
        if rows is None:
            return _ListOutcome(ids=None, reason="list_failed")
        try:
            cutoff_dt = _parse_iso_timestamp(cutoff_iso)
        except ValueError:
            return _ListOutcome(ids=None, reason="malformed_cutoff")
        out: list[str] = []
        for row in rows:
            created_raw = row.get("created_at")
            if created_raw is None or str(created_raw).strip() == "":
                # Absent/empty timestamp is unmeasured — not a measured skip.
                # Collapsing it into "not eligible" makes the sweep look empty-ok.
                return _ListOutcome(ids=None, reason="malformed_created_at")
            try:
                created_dt = _parse_iso_timestamp(str(created_raw))
            except ValueError:
                # Unparseable source data: do not silently treat as "not old".
                return _ListOutcome(ids=None, reason="malformed_created_at")
            # read_at must be a parseable timestamp to count as "already-read".
            # Non-null garbage (e.g. "not-a-date") is unmeasured, not eligible.
            read_at = row.get("read_at")
            read_ok = False
            if read_at is not None and str(read_at).strip() != "":
                try:
                    _parse_iso_timestamp(str(read_at))
                    read_ok = True
                except ValueError:
                    return _ListOutcome(ids=None, reason="malformed_read_at")
            if created_dt < cutoff_dt and read_ok:
                out.append(str(row["id"]))
            if len(out) >= limit:
                break
        return _ListOutcome(ids=out)
    # Connection path: never rely on lexical SQL age alone. Garbage strings
    # such as created_at="not-a-date" fail ``created_at < cutoff`` and would
    # otherwise present as a measured empty success while the row remains.
    # Likewise, non-null but unparseable read_at must not become eligibility.
    try:
        cutoff_dt = _parse_iso_timestamp(cutoff_iso)
    except ValueError:
        return _ListOutcome(ids=None, reason="malformed_cutoff")
    try:
        rows = connection.execute(
            "SELECT id, created_at, read_at FROM notifications "
            "WHERE read_at IS NOT NULL "
            "ORDER BY created_at ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - SELECT failure is unmeasured, not empty
        return _ListOutcome(ids=None, reason="list_failed")
    eligible: list[str] = []
    for row in rows:
        if isinstance(row, tuple):
            nid, created_raw, read_raw = row[0], row[1], row[2]
        else:
            nid, created_raw, read_raw = row["id"], row["created_at"], row["read_at"]
        if created_raw is None or str(created_raw).strip() == "":
            return _ListOutcome(ids=None, reason="malformed_created_at")
        try:
            created_dt = _parse_iso_timestamp(str(created_raw))
        except ValueError:
            return _ListOutcome(ids=None, reason="malformed_created_at")
        if read_raw is None or str(read_raw).strip() == "":
            return _ListOutcome(ids=None, reason="malformed_read_at")
        try:
            _parse_iso_timestamp(str(read_raw))
        except ValueError:
            return _ListOutcome(ids=None, reason="malformed_read_at")
        if created_dt < cutoff_dt:
            eligible.append(str(nid))
            if len(eligible) >= limit:
                break
    return _ListOutcome(ids=eligible)


@dataclass(frozen=True, slots=True)
class _DeleteOutcome:
    """Measured delete count, or an explicit unmeasured failure.

    ``deleted is None`` means the outcome could not be measured — callers must
    check ``is None`` explicitly (never bare truthiness) so unknown does not
    silently take the zero/false branch.
    """

    deleted: int | None
    reason: str | None = None


def _delete_ids(dal: Any, ids: list[str]) -> _DeleteOutcome:
    """Delete ``ids`` and return a measured count or an unmeasured failure.

    A non-result (no delete path, execute/commit failure, unreported deleter
    count, unknown/negative rowcount, or impossible positive overcount) returns
    ``deleted=None`` with a ``reason``. Callers must never invent a favourable
    deleted total from an unmeasured outcome — that is the "non-result
    presented as a favourable result" defect class.
    """
    if not ids:
        return _DeleteOutcome(deleted=0)
    connection = getattr(dal, "_connection", None)
    if connection is None:
        deleter = getattr(dal, "delete_ids", None)
        if not callable(deleter):
            return _DeleteOutcome(deleted=None, reason="no_delete_capability")
        result = deleter(ids)
        # Only a non-negative int within the request size is a truthful
        # confirmation. None / non-int is unreported; bool is an int subclass
        # but is a success marker, not a count (True→1 / False→0 counterfeit);
        # negative or overcount is impossible — never clamp to zero ok or
        # invent extra deletes.
        if isinstance(result, int) and not isinstance(result, bool):
            if result < 0 or result > len(ids):
                return _DeleteOutcome(deleted=None, reason="invalid_delete_count")
            return _DeleteOutcome(deleted=result)
        return _DeleteOutcome(deleted=None, reason="unreported_delete_count")
    placeholders = ",".join("?" for _ in ids)
    try:
        cursor = connection.execute(
            f"DELETE FROM notifications WHERE id IN ({placeholders})",
            ids,
        )
        try:
            connection.commit()
        except Exception:  # noqa: BLE001 - commit failure is not a successful delete
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001
                pass
            return _DeleteOutcome(deleted=None, reason="commit_failed")
        n = cursor.rowcount
        if n is None or n < 0:
            # SQLite occasionally reports -1; refuse to invent a favourable count.
            return _DeleteOutcome(deleted=None, reason="unknown_rowcount")
        # Positive overcount is impossible for a truthful DELETE of ``ids``.
        # Accepting it invents a favourable deleted total (same class as
        # custom-deleter overcount).
        if int(n) > len(ids):
            return _DeleteOutcome(deleted=None, reason="invalid_delete_count")
        return _DeleteOutcome(deleted=int(n))
    except Exception:  # noqa: BLE001 - execute failure is not a successful delete
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _DeleteOutcome(deleted=None, reason="execute_failed")
