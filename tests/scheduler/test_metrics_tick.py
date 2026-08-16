from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.metrics_tick import main, tick


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE swarm_attempts (provider TEXT NOT NULL, started_at TEXT NOT NULL, end_reason TEXT)"
    )
    # swarm_metric_snapshots is created by tick() automatically, no pre-creation needed
    return conn


def test_tick_writes_rates_reasons_and_zero_provider_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    conn = _db(db_path)
    conn.executemany(
        "INSERT INTO swarm_attempts VALUES (?, ?, ?)",
        [
            ("codex", "2026-07-31T10:05:00Z", "completed"),
            ("codex", "2026-07-31T10:06:00Z", "blocked"),
            ("gemini", "2026-07-31T10:07:00Z", "blocked"),
        ],
    )
    conn.commit()
    conn.close()

    rows = tick(db_path=db_path, now=datetime(2026, 7, 31, 10, 10, tzinfo=UTC))

    assert len(rows) == 10
    completion = next(row for row in rows if row["metric"] == "swarm_completion_rate")
    assert completion["value"] == 1 / 3
    assert completion["meta_json"]["attempts_started"] == 3
    assert completion["meta_json"]["attempts_completed"] == 1
    assert completion["meta_json"]["provider_stats"]["kimi"]["completion_rate"] == 0.0
    blocked = next(row for row in rows if row["metric"] == "swarm_top_failure_1")
    assert blocked["value"] == 2
    assert blocked["meta_json"]["top_reason"] == {"reason": "blocked", "count": 2}
    # Verify window_start and window_end are stored (required for SLO reconciliation)
    assert "window_start" in completion["meta_json"]
    assert "window_end" in completion["meta_json"]


def test_tick_uses_previous_snapshot_as_cursor_and_writes_zeros(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO swarm_attempts VALUES (?, ?, ?)",
        ("claude", "2026-07-31T10:05:00Z", "completed"),
    )
    conn.commit()
    conn.close()

    first = tick(db_path=db_path, now=datetime(2026, 7, 31, 10, 10, tzinfo=UTC))
    second = tick(db_path=db_path, now=datetime(2026, 7, 31, 10, 20, tzinfo=UTC))

    assert next(row for row in first if row["metric"] == "swarm_completion_rate")["value"] == 1.0
    completion = next(row for row in second if row["metric"] == "swarm_completion_rate")
    assert completion["value"] == 0.0
    assert completion["meta_json"]["attempts_started"] == 0
    assert json.loads(json.dumps(completion["meta_json"]))["top_reasons"] == []


def test_launchd_entry_point_evaluates_the_slo_it_just_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scheduled job is ``get_slo_status``'s production caller.

    Measuring a completion rate every ten minutes and never comparing it to the
    objective is the 'built, tested, never wired' shape this repo keeps finding.
    The operator's ``var/log/metrics.log`` line carries both halves or neither.
    """
    db_path = tmp_path / "state.sqlite3"
    conn = _db(db_path)
    # main() takes no clock argument -- it is the launchd entry point -- so the
    # attempts must sit inside the real rolling window rather than a fixed date.
    recent = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany(
        "INSERT INTO swarm_attempts VALUES (?, ?, ?)",
        [("codex", recent, "completed"), ("codex", recent, "blocked")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))

    assert main() == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["snapshot"]["metric"] == "swarm_completion_rate"
    assert emitted["snapshot"]["value"] == 0.5
    # The snapshot tick() just committed is the one the status reads back.
    assert emitted["slo"]["slo"] == "swarm_completion"
    assert emitted["slo"]["current_rate"] == 0.5
