"""Daily snapshot aggregator for the Observatory time-series.

A single :func:`snapshot` call reads live tables and writes one
``pulse_series`` row per metric for today's date. The function is designed
to be called either:

* directly (e.g., from the HTTP layer to seed on first read), or
* from a scheduler/routine on a daily cadence.

The upsert semantics in :class:`PulseStore` mean re-running is safe: today's
row simply gets the latest counts.

Metric namespace is dotted (``skills.total``, ``loops.fires``, …). Query
failures are logged and raised: treating an unavailable table as a real
zero would make an outage look like a quiet, healthy system.

Unreadable or future source timestamps are also raised rather than softened
to ``None``: the production seed path discards the return map and serves
``series()``, so a soft-unknown count becomes an empty series that mixed
tiles render as a concrete zero. Fail-closed keeps that path from claiming
a favourable measurement it did not take. Empty-denominator *rates* still
use ``None`` (omitted from storage) — those already had an explicit unknown
representation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.db.store import SqliteStore

_LOG = logging.getLogger(__name__)

#: Canonical metric names emitted by :func:`snapshot` and consumed by the
#: Observatory front end. Keep this set in sync with the pinned contract in
#: docs/ui-redesign/FINAL-PLAN.md section B.
METRICS: tuple[str, ...] = (
    "skills.total",
    "skills.versions",
    "improvements.applied",
    "loops.fires",
    "loops.acceptance",
    "memory.facts",
    "reliability.score",
)


def _today_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _parse_utc_timestamp(raw: object) -> datetime | None:
    """Parse a real UTC instant. Impossible calendar dates are unreadable.

    SQLite's ``datetime()`` normalizes some invalid dates (e.g. 2026-02-30 →
    2026-03-02). Pulse must not treat those as successful observations.
    Accepts ISO 8601 (with or without timezone) and SQLite CURRENT_TIMESTAMP
    space-separated format (tz-naive, treated as UTC).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Convert SQLite CURRENT_TIMESTAMP format (space-sep, tz-naive) to ISO format
    # e.g. "2026-07-31 15:08:19" -> "2026-07-31T15:08:19"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Treat tz-naive strings (including SQLite CURRENT_TIMESTAMP) as UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _count(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> float:
    """Run a scalar count query without converting failures into real zeroes."""
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:
        _LOG.exception("Pulse metric query failed: %s", sql)
        raise
    if row is None:
        message = f"Pulse metric query returned no scalar row: {sql}"
        _LOG.error(message)
        raise RuntimeError(message)
    try:
        return float(row[0])
    except (TypeError, ValueError, IndexError) as exc:
        message = f"Pulse metric query returned a non-numeric scalar: {sql}"
        _LOG.error(message)
        raise RuntimeError(message) from exc


def _load_created_at(conn: Any, table: str, floor: datetime | None = None) -> tuple[list[datetime] | None, str | None]:
    """Load every ``created_at`` as a real UTC instant, or return error.

    Any unreadable, timezone-naive, impossible-calendar, or future timestamp
    marks the source as untrustworthy. Returns (timestamps, error_message).
    If floor is provided, only loads rows with created_at >= floor.
    """
    try:
        if floor:
            # Bind a DATE-ONLY floor: a full isoformat bound ('...T00:00:00+00:00')
            # string-compares ABOVE SQLite's space-separated CURRENT_TIMESTAMP form
            # (' ' < 'T'), silently dropping same-day space-form rows — an
            # undercount published with error=None. The date prefix sorts below
            # every same-day timestamp in BOTH forms; exact instant filtering
            # happens in Python below, so this is a safe superset.
            floor_iso = floor.strftime("%Y-%m-%d")
            sql = f"SELECT created_at FROM {table} WHERE created_at >= ?"
            rows = conn.execute(sql, (floor_iso,)).fetchall()
        else:
            sql = f"SELECT created_at FROM {table}"
            rows = conn.execute(sql).fetchall()
    except Exception:
        _LOG.exception("Pulse metric query failed: %s", sql)
        return None, f"query_failed_for_{table}"
    now = datetime.now(UTC)
    parsed: list[datetime] = []
    for row in rows:
        instant = _parse_utc_timestamp(row[0])
        if instant is None or instant > now:
            message = f"unreadable_or_future_timestamps_in_{table}"
            _LOG.error("Pulse metric source %s has unreadable or future timestamps", table)
            return None, message
        parsed.append(instant)
    return parsed, None

def _count_between(timestamps: list[datetime], start: datetime, end: datetime) -> float:
    return float(sum(1 for ts in timestamps if start <= ts <= end))


def compute_metrics(store: SqliteStore) -> tuple[dict[str, float | None], dict[str, str | None]]:
    """Compute today's value for every metric.

    Returns ``({metric: value}, {metric: error})``. Uses ``None`` for values when
    a successful query has insufficient observations to calculate a *rate*, and
    ``None`` for errors when a metric succeeds. Failed metrics have
    ``value=None`` and a non-None error string. Per-metric failures do not
    affect other metrics — one bad timestamp in one source table does not blank
    all seven metrics.
    """
    conn = store._connection
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=7)
    out: dict[str, float | None] = {}
    errors: dict[str, str | None] = {}

    # skills.total — count of active skills in the library
    try:
        out["skills.total"] = _count(conn, "SELECT COUNT(*) FROM skills WHERE status = 'active'")
        errors["skills.total"] = None
    except Exception as exc:
        _LOG.exception("skills.total metric failed")
        out["skills.total"] = None
        errors["skills.total"] = str(exc)

    # skills.versions — new skill_versions rows created in the last 7 days.
    try:
        skill_version_times, load_error = _load_created_at(conn, "skill_versions", week_start)
        if load_error or skill_version_times is None:
            out["skills.versions"] = None
            errors["skills.versions"] = load_error or "unreadable_timestamps"
        else:
            out["skills.versions"] = _count_between(skill_version_times, week_start, now)
            errors["skills.versions"] = None
    except Exception as exc:
        _LOG.exception("skills.versions metric failed")
        out["skills.versions"] = None
        errors["skills.versions"] = str(exc)

    # improvements.applied — improvements currently in a terminal-good status
    try:
        out["improvements.applied"] = _count(
            conn,
            "SELECT COUNT(*) FROM improvements WHERE status IN ('applied', 'monitoring', 'confirmed')",
        )
        errors["improvements.applied"] = None
    except Exception as exc:
        _LOG.exception("improvements.applied metric failed")
        out["improvements.applied"] = None
        errors["improvements.applied"] = str(exc)

    # Time-windowed loop metrics require every run timestamp to be a real instant.
    try:
        run_times, run_load_error = _load_created_at(conn, "routine_runs", day_start)
        if run_load_error or run_times is None:
            out["loops.fires"] = None
            errors["loops.fires"] = run_load_error or "unreadable_timestamps"
        else:
            out["loops.fires"] = _count_between(run_times, day_start, now)
            errors["loops.fires"] = None
    except Exception as exc:
        _LOG.exception("loops.fires metric failed")
        out["loops.fires"] = None
        errors["loops.fires"] = str(exc)

    # loops.acceptance — rolling 7-day acceptance rate (0..1).
    try:
        run_rows = conn.execute(
            "SELECT created_at, accepted FROM routine_runs WHERE created_at >= ?",
            # Date-only floor for the same reason as _load_created_at: a 'T'
            # bound excludes same-day space-form timestamps; Python filters
            # exact instants below.
            (week_start.strftime("%Y-%m-%d"),),
        ).fetchall()

        settled_in_window = 0
        accepted_in_window = 0
        for created_at, accepted in run_rows:
            instant = _parse_utc_timestamp(created_at)
            if instant is None or not (week_start <= instant <= now):
                continue
            if accepted is None:
                continue
            if accepted not in (0, 1):
                message = "Pulse metric source routine_runs has invalid accepted outcomes"
                _LOG.error(message)
                raise RuntimeError(message)
            settled_in_window += 1
            if accepted == 1:
                accepted_in_window += 1

        if settled_in_window > 0:
            out["loops.acceptance"] = round(accepted_in_window / settled_in_window, 4)
        else:
            out["loops.acceptance"] = None
        errors["loops.acceptance"] = None
    except Exception as exc:
        _LOG.exception("loops.acceptance metric failed")
        out["loops.acceptance"] = None
        errors["loops.acceptance"] = str(exc)

    # memory.facts — typed memory records currently in a promoted/active state
    try:
        out["memory.facts"] = _count(
            conn,
            "SELECT COUNT(*) FROM metacog_memory_records "
            "WHERE promotion_status IN ('promoted', 'shadow')",
        )
        errors["memory.facts"] = None
    except Exception as exc:
        _LOG.exception("memory.facts metric failed")
        out["memory.facts"] = None
        errors["memory.facts"] = str(exc)

    # reliability.score — fraction of open reliability events that are NOT
    # critical. 1.0 means fully healthy; lower = more critical events open.
    try:
        open_events = _count(conn, "SELECT COUNT(*) FROM reliability_events WHERE status = 'open'")
        if open_events > 0:
            critical = _count(
                conn,
                "SELECT COUNT(*) FROM reliability_events "
                "WHERE status = 'open' AND severity = 'critical'",
            )
            out["reliability.score"] = round(1.0 - (critical / open_events), 4)
        else:
            out["reliability.score"] = None
        errors["reliability.score"] = None
    except Exception as exc:
        _LOG.exception("reliability.score metric failed")
        out["reliability.score"] = None
        errors["reliability.score"] = str(exc)

    return out, errors


def snapshot(
    store: SqliteStore,
    date: str | None = None,
    values: dict[str, float | None] | None = None,
) -> dict[str, float | None]:
    """Compute today's metrics and upsert them into ``pulse_series``.

    Returns the computed ``{metric: value}`` dict, preserving ``None`` for
    rates with an empty denominator. Idempotent — re-running on the same date
    overwrites today's row with the latest *known* values.

    If ``values`` are pre-computed, uses them directly (avoid double compute).
    Otherwise calls compute_metrics internally.

    Persistence note: ``pulse_series.value`` is ``REAL NOT NULL``, so unknown
    rates are omitted from storage rather than coerced to ``0.0`` (which would
    re-claim "everything rejected" / "fully degraded"). Callers that need the
    honest unknown must use this return value (or ``compute_metrics``), not
    ``PulseStore.latest`` alone. Same-day rows for unknown rates are cleared so
    a later re-snapshot cannot leave a superseded numeric claim.

    Unreadable source timestamps raise before any write so seed-on-empty
    cannot persist companion metrics while leaving a broken count series
    empty (empty companion → mixed tiles show zero).
    """
    from omniagentos.pulse.store import PulseStore

    if values is None:
        values, errors = compute_metrics(store)
    else:
        # Pre-computed values provided; no need to re-compute
        pass
    target_date = date or _today_iso()
    pulse = PulseStore(store)
    points: list[tuple[str, str, float]] = []
    unknown: list[str] = []
    for metric, value in values.items():
        if value is None:
            # Non-null column cannot store "unknown". Skip the write and clear
            # any same-day point so null-ness stays recoverable from the return
            # dict / absence of a point — never rewrite None as 0.0.
            unknown.append(metric)
            continue
        points.append((metric, target_date, value))
    for metric in unknown:
        pulse.delete(metric, target_date)
    pulse.upsert_many(points)
    return values
