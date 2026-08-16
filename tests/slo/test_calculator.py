from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.slo import get_slo_status, load_slos


def test_load_slos_exposes_the_single_swarm_completion_definition() -> None:
    loaded = load_slos()
    assert loaded["swarm_completion"]["target"] == 0.60
    assert loaded["swarm_completion"]["metric"] == "swarm_completion_rate"


def test_status_sums_disjoint_measurement_snapshot_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE swarm_metric_snapshots (id INTEGER PRIMARY KEY, source TEXT, metric TEXT, "
        "captured_at TEXT, meta_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO swarm_metric_snapshots VALUES (?, ?, ?, ?, ?)",
        [
            (1, "swarm-measurement", "swarm_completion_rate", "2026-07-31T09:55:00Z", json.dumps({"attempts_started": 50, "attempts_completed": 2})),
            (2, "swarm-measurement", "swarm_completion_rate", "2026-07-31T10:00:00Z", json.dumps({"attempts_started": 25, "attempts_completed": 1})),
        ],
    )
    conn.commit()
    conn.close()

    status = get_slo_status(db_path=db_path, now=datetime(2026, 7, 31, 10, 1, tzinfo=UTC))

    assert status["current_rate"] == 3 / 75
    assert status["burn_rate"] == (1 - (3 / 75)) / (1 - 0.60)
    assert status["metrics_at"] == "2026-07-31T10:00:00Z"
