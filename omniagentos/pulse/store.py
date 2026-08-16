"""Read/write access to the ``pulse_series`` time-series table.

Composed on top of :class:`omniagentos.db.store.SqliteStore` the same way
:class:`omniagentos.scheduler.store.RoutinesStore` is: reuses the shared
SQLite connection, lock, and transaction helpers instead of opening a
second connection.

The table is created lazily by migration 084, so this store never runs DDL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, cast

from omniagentos.db.store import SqliteStore


def _finite_value(value: float) -> float:
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError("Pulse measurements must be finite")
    return numeric


def _validated_date(raw: str) -> str:
    """Accept only real UTC calendar days that are not in the future.

    A successful write must be a successful observation. Unparseable or future
    dates cannot be committed and later hidden by reads — that would make
    corruption look identical to a clean write of genuine data.
    """
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Pulse date must be a real YYYY-MM-DD calendar day, got {raw!r}"
        ) from exc
    if parsed.isoformat() != raw:
        raise ValueError(
            f"Pulse date must be a real YYYY-MM-DD calendar day, got {raw!r}"
        )
    today = datetime.now(UTC).date()
    if parsed > today:
        raise ValueError(f"Pulse date cannot be in the future: {raw!r}")
    return raw

def _validated_point(metric: str, day: str, value: float) -> tuple[str, str, float]:
    return (metric, _validated_date(day), _finite_value(value))


def _serialized(method):  # type: ignore[no-untyped-def]
    from functools import wraps

    @wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        store = cast(PulseStore, args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


class PulseStore:
    """Time-series read/write over the shared ``pulse_series`` table."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._connection

    @_serialized
    def upsert(self, metric: str, date: str, value: float) -> None:
        """Insert or replace a single (metric, date, value) point.

        Rejects non-finite values and unparseable/future dates at the write
        boundary so a successful return always means a real observation.
        """
        metric_name, day, numeric = _validated_point(metric, date, value)
        self._conn().execute(
            "INSERT INTO pulse_series (metric, date, value) VALUES (?, ?, ?) "
            "ON CONFLICT(metric, date) DO UPDATE SET value = excluded.value",
            (metric_name, day, numeric),
        )
        self._store._connection.commit()

    @_serialized
    def upsert_many(self, points: list[tuple[str, str, float]]) -> None:
        """Bulk upsert a list of (metric, date, value) points in one txn.

        Validates every point before any write so a bad date/value cannot
        partially commit its neighbours.
        """
        if not points:
            return
        validated = [_validated_point(m, d, v) for m, d, v in points]
        conn = self._conn()
        conn.executemany(
            "INSERT INTO pulse_series (metric, date, value) VALUES (?, ?, ?) "
            "ON CONFLICT(metric, date) DO UPDATE SET value = excluded.value",
            validated,
        )
        conn.commit()

    @_serialized
    def delete(self, metric: str, date: str) -> None:
        """Remove a single (metric, date) point if present.

        Used when a rate is unknown (empty denominator): the column is
        non-nullable, so absence — not a coerced ``0.0`` — is how storage
        records "no observation".
        """
        self._conn().execute(
            "DELETE FROM pulse_series WHERE metric = ? AND date = ?",
            (metric, date),
        )
        self._store._connection.commit()

    def series(self, metric: str, days: int = 30) -> list[dict[str, Any]]:
        """Return points in the last ``days`` UTC dates, oldest first.

        Shape: ``[{ "date": "YYYY-MM-DD", "value": <float> }, ...]``.
        """
        window_days = max(1, int(days))
        today = datetime.now(UTC).date()
        first_date = (today - timedelta(days=window_days - 1)).isoformat()
        conn = self._conn()
        rows = conn.execute(
            "SELECT date, value FROM pulse_series "
            "WHERE metric = ? AND date(date) = date AND date BETWEEN ? AND ? "
            "ORDER BY date DESC",
            (metric, first_date, today.isoformat()),
        ).fetchall()
        return [{"date": r["date"], "value": float(r["value"])} for r in reversed(rows)]

    def has_any(self, metric: str | None = None) -> bool:
        """Check for valid, non-future rows (optionally for one metric)."""
        conn = self._conn()
        today = datetime.now(UTC).date().isoformat()
        if metric is not None:
            row = conn.execute(
                "SELECT 1 FROM pulse_series "
                "WHERE metric = ? AND date(date) = date AND date <= ? LIMIT 1",
                (metric, today),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM pulse_series WHERE date(date) = date AND date <= ? LIMIT 1",
                (today,),
            ).fetchone()
        return row is not None

    def latest(self, metric: str) -> dict[str, Any] | None:
        """Most recent valid, non-future point for ``metric``, or ``None``."""
        today = datetime.now(UTC).date().isoformat()
        row = self._conn().execute(
            "SELECT date, value FROM pulse_series "
            "WHERE metric = ? AND date(date) = date AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (metric, today),
        ).fetchone()
        if row is None:
            return None
        return {"date": row["date"], "value": float(row["value"])}

    def metrics(self) -> list[str]:
        """Metric names with at least one valid, non-future point."""
        today = datetime.now(UTC).date().isoformat()
        rows = self._conn().execute(
            "SELECT DISTINCT metric FROM pulse_series "
            "WHERE date(date) = date AND date <= ? ORDER BY metric",
            (today,),
        ).fetchall()
        return [r["metric"] for r in rows]
