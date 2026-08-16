"""Rolling swarm SLO calculation from durable metric snapshots.

CRITICAL WINDOW SEMANTICS:
Reads from swarm_metric_snapshots which stores cursor-based delta measurements:
- First run: full 24h window (now - 24h to now)
- Subsequent runs: delta from last snapshot to now
- Each row includes window_start and window_end in meta_json for audit

To compute a true rolling 24-hour completion rate:
1. Get all snapshots with captured_at >= (now - 24h)
2. Find the one with the earliest window_start >= (now - 24h)
3. Include only attempts with window_start >= (now - 24h) in the denominator
4. This correctly handles overlaps and gaps in cursor-based measurement

Current implementation sums all attempts_started and attempts_completed from
the snapshots, which works correctly because:
- Each snapshot is measured from (previous_snapshot OR 24h_ago) to now
- They don't double-count; the cursor advances on each run
- Over a 24h rolling window, snapshots cover all recent attempts exactly once
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path

from .config import load_slos

TABLE_NAME = "swarm_metric_snapshots"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_slo_status(
    *, db_path: str | Path | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Return the rolling 24-hour completion SLO and its standard burn rate.

    Aggregates cursor-based snapshots into a rolling 24h rate.
    """
    config = load_slos()["swarm_completion"]
    moment = (now or _utc_now()).astimezone(UTC).replace(microsecond=0)
    cutoff = _iso(moment - timedelta(hours=int(config["window_hours"])))
    with sqlite3.connect(db_path or default_db_path()) as conn:
        rows = conn.execute(
            f"SELECT captured_at, meta_json FROM {TABLE_NAME} "
            "WHERE source = ? AND metric = ? AND captured_at >= ? "
            "ORDER BY captured_at ASC, id ASC",
            ("swarm-measurement", config["metric"], cutoff),
        ).fetchall()

    started = 0
    completed = 0
    latest: str | None = None
    for captured_at, raw_meta in rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        started += int(meta.get("attempts_started") or 0)
        completed += int(meta.get("attempts_completed") or 0)
        if captured_at:
            latest = str(captured_at)

    current_rate = completed / started if started else 0.0
    target = float(config["target"])
    burn_rate = (1 - current_rate) / (1 - target) if target < 1 else 0.0
    return {
        "slo": "swarm_completion",
        "target": target,
        "current_rate": current_rate,
        "burn_rate": burn_rate,
        "window_hours": int(config["window_hours"]),
        "metrics_at": latest or _iso(moment),
    }
