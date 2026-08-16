"""Persist swarm-performance measurement snapshots to swarm_metric_snapshots.

This module writes to a dedicated swarm_metric_snapshots table (never the goals
subsystem's metric_snapshots table). Each tick measures attempts started since
the previous successful measurement, using a cursor-based window that never
extends past 24 hours (rolling back to 24h on the first run, then using the
prior snapshot timestamp as a cursor for subsequent runs). This allows the
completion SLO to aggregate snapshots over a rolling 24-hour window without
double-counting attempts.

Window semantics (CRITICAL for SLO reconciliation):
- First run: window_start = (now - 24h), window_end = now
- Subsequent runs: window_start = prior_snapshot.captured_at, window_end = now
- The window_start/window_end are ALWAYS stored in meta_json for audit
- The timestamp column used (started_at or created_at) is stored in meta_json
- SLO aggregation MUST read window_start, window_end, and time_column from each row
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path
from omniagentos.slo import get_slo_status

SOURCE = "swarm-measurement"
COMPLETION_METRIC = "swarm_completion_rate"
TABLE_NAME = "swarm_metric_snapshots"
PROVIDERS = ("claude", "codex", "grok", "gemini", "kimi", "qwen")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attempt_time_column(conn: sqlite3.Connection) -> str:
    """Return the schema's creation timestamp column.

    Production migration 044 calls it ``started_at``.  Supporting
    ``created_at`` keeps this tick compatible with earlier exported databases
    and documents why the SQL does not use the wording from the product brief.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(swarm_attempts)").fetchall()}
    if "started_at" in columns:
        return "started_at"
    if "created_at" in columns:
        return "created_at"
    raise RuntimeError("swarm_attempts has neither started_at nor created_at")


def _window_start(conn: sqlite3.Connection, *, now: datetime) -> datetime:
    """Use the prior completion snapshot as a cursor, never extending past 24h.

    WINDOW SEMANTICS:
    - If a prior snapshot exists within the past 24h, use its captured_at as window_start
    - Otherwise, use exactly 24h ago
    - This creates a cursor-based delta window on subsequent runs, not a pure rolling window
    """
    cutoff = now - timedelta(hours=24)
    row = conn.execute(
        f"SELECT captured_at FROM {TABLE_NAME} "
        "WHERE source = ? AND metric = ? AND captured_at >= ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (SOURCE, COMPLETION_METRIC, _iso(cutoff)),
    ).fetchone()
    if row is None or not row[0]:
        return cutoff
    try:
        previous = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except ValueError:
        return cutoff
    return max(cutoff, previous.astimezone(UTC))


def _measure(
    conn: sqlite3.Connection, *, start: datetime, end: datetime, time_column: str
) -> dict[str, Any]:
    params = (_iso(start), _iso(end))
    rows = conn.execute(
        f"SELECT provider, end_reason FROM swarm_attempts "
        f"WHERE {time_column} > ? AND {time_column} <= ?",
        params,
    ).fetchall()

    provider_counts = {
        provider: {"attempts_started": 0, "attempts_completed": 0} for provider in PROVIDERS
    }
    reasons: dict[str, int] = {}
    attempts_completed = 0
    for provider, end_reason in rows:
        provider_name = str(provider or "")
        if provider_name not in provider_counts:
            provider_counts[provider_name] = {"attempts_started": 0, "attempts_completed": 0}
        provider_counts[provider_name]["attempts_started"] += 1
        if end_reason:
            reason = str(end_reason)
            reasons[reason] = reasons.get(reason, 0) + 1
        if end_reason == "completed":
            attempts_completed += 1
            provider_counts[provider_name]["attempts_completed"] += 1

    attempts_started = len(rows)
    provider_stats = {
        provider: {
            **counts,
            "completion_rate": (
                counts["attempts_completed"] / counts["attempts_started"]
                if counts["attempts_started"]
                else 0.0
            ),
        }
        for provider, counts in provider_counts.items()
    }
    top_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    return {
        "attempts_started": attempts_started,
        "attempts_completed": attempts_completed,
        "completion_rate": attempts_completed / attempts_started if attempts_started else 0.0,
        "provider_stats": provider_stats,
        "top_reasons": top_reasons,
    }


def tick(*, db_path: str | Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Measure one interval and return the rows written to swarm_metric_snapshots.

    Returns list of dicts with metric, value, captured_at, and meta_json keys.
    meta_json includes window_start, window_end, time_column (for reproducibility),
    and detailed stats.
    """
    database = Path(db_path or default_db_path())
    moment = (now or _utc_now()).astimezone(UTC).replace(microsecond=0)
    captured_at = _iso(moment)
    with sqlite3.connect(database) as conn:
        # Ensure table exists
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                metric TEXT,
                value REAL,
                unit TEXT,
                window TEXT,
                captured_at TEXT,
                meta_json TEXT
            )
        """)

        time_column = _attempt_time_column(conn)
        start = _window_start(conn, now=moment)
        measured = _measure(conn, start=start, end=moment, time_column=time_column)
        common_meta = {
            **measured,
            "window_start": _iso(start),
            "window_end": captured_at,
            "window_hours": (moment - start).total_seconds() / 3600,
            "time_column": time_column,
        }
        snapshots: list[tuple[str, float, dict[str, Any]]] = [
            (COMPLETION_METRIC, float(measured["completion_rate"]), common_meta)
        ]
        for provider in PROVIDERS:
            provider_meta = {
                **common_meta,
                "provider": provider,
                "provider_stat": measured["provider_stats"][provider],
            }
            snapshots.append(
                (
                    f"swarm_{provider}_completion_rate",
                    float(measured["provider_stats"][provider]["completion_rate"]),
                    provider_meta,
                )
            )
        for index in range(3):
            reason = (
                measured["top_reasons"][index]
                if index < len(measured["top_reasons"])
                else {"reason": None, "count": 0}
            )
            raw_count = reason.get("count", 0)
            reason_count = float(raw_count) if isinstance(raw_count, (int, float, str)) else 0.0
            snapshots.append(
                (
                    f"swarm_top_failure_{index + 1}",
                    reason_count,
                    {**common_meta, "top_reason": reason},
                )
            )

        conn.executemany(
            f"INSERT INTO {TABLE_NAME} "
            "(source, metric, value, unit, window, captured_at, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    SOURCE,
                    metric,
                    value,
                    "ratio" if "completion_rate" in metric else "count",
                    "cursor-based-delta-24h",
                    captured_at,
                    json.dumps(meta, sort_keys=True),
                )
                for metric, value, meta in snapshots
            ],
        )
        return [
            {"metric": metric, "value": value, "captured_at": captured_at, "meta_json": meta}
            for metric, value, meta in snapshots
        ]


def main() -> int:
    """Launchd entry point: measure the interval, then evaluate the objective.

    Measuring without evaluating is how an SLO becomes decoration. ``tick()``
    has already committed its snapshot by the time :func:`get_slo_status` runs,
    so the rolling status is computed from durable rows and a failure here
    surfaces loudly (non-zero exit into ``var/log/metrics.log``) instead of
    silently costing the measurement.
    """
    rows = tick()
    completion = next(row for row in rows if row["metric"] == COMPLETION_METRIC)
    print(
        json.dumps(
            {"snapshot": completion, "slo": get_slo_status()},
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
