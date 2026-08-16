from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.db.migrate import migrate
from omniagentos.db.store import _connect
from omniagentos.maintenance.cache_gc import _gc_notifications, collect_garbage

# Deterministic clock so retention cutoffs never depend on wall time.
NOW = datetime(2026, 1, 15, tzinfo=UTC)
OLD_TS = "2025-01-01T00:00:00Z"  # older than every window
RECENT_TS = "2026-01-14T00:00:00Z"  # inside every window
OLD_DAY = "2025-01-01"
RECENT_DAY = "2026-01-14"


def _fresh_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "gc.db")
    migrate(db_path)
    return db_path


def _rows(db_path: str, sql: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _insert_event(conn: sqlite3.Connection, ts: str, type_: str) -> None:
    conn.execute(
        "INSERT INTO events (ts, type, actor, action) VALUES (?, ?, 'runner', 'x')",
        (ts, type_),
    )


def test_events_keep_audit_replay_and_recent(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        _insert_event(conn, OLD_TS, "audit.event")  # id 1 — audit trail, kept
        for _ in range(5):  # ids 2..6 — old transient churn
            _insert_event(conn, OLD_TS, "run.updated")
        _insert_event(conn, RECENT_TS, "run.updated")  # id 7 — recent, kept
    finally:
        conn.close()

    report = collect_garbage(db_path, days=7, replay_keep=2, now=NOW)

    # latest id = 7, keep_above = 5 → only old transient ids 2,3,4 are eligible.
    assert report["events"] == 3
    assert _rows(db_path, "SELECT COUNT(*) FROM events WHERE type = 'audit.event'") == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM events WHERE type = 'run.updated'") == 3


def test_metric_snapshots_respect_facts_window_and_goal_links(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO metric_snapshots (id, metric, value, captured_at) "
            "VALUES (1, 'roas', 1.0, ?)",
            (OLD_TS,),
        )  # old + unlinked → deleted
        conn.execute(
            "INSERT INTO metric_snapshots (id, metric, value, captured_at) "
            "VALUES (2, 'roas', 1.0, ?)",
            (OLD_TS,),
        )  # old but linked to a goal fact → kept
        conn.execute(
            "INSERT INTO metric_snapshots (id, metric, value, captured_at) "
            "VALUES (3, 'roas', 1.0, ?)",
            (RECENT_TS,),
        )  # recent → kept
        conn.execute(
            "INSERT INTO goal_facts (goal_id, fact_id, linked_by, linked_at) "
            "VALUES ('g1', 2, 'test', ?)",
            (OLD_TS,),
        )
    finally:
        conn.close()

    report = collect_garbage(db_path, facts_days=365, now=NOW)

    assert report["metric_snapshots"] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM metric_snapshots") == 2
    assert _rows(db_path, "SELECT COUNT(*) FROM metric_snapshots WHERE id = 2") == 1


def test_only_resolved_aged_approvals_deleted(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO approvals (id, action_class, proposed_action, state, "
            "decided_at, created_at) VALUES ('a1', 'read_only', 'x', 'approved', ?, ?)",
            (OLD_TS, OLD_TS),
        )  # resolved + old → deleted
        conn.execute(
            "INSERT INTO approvals (id, action_class, proposed_action, state, "
            "created_at) VALUES ('a2', 'read_only', 'x', 'pending', ?)",
            (OLD_TS,),
        )  # pending → kept regardless of age
        conn.execute(
            "INSERT INTO approvals (id, action_class, proposed_action, state, "
            "decided_at, created_at) VALUES ('a3', 'read_only', 'x', 'rejected', ?, ?)",
            (RECENT_TS, RECENT_TS),
        )  # resolved but recent → kept
    finally:
        conn.close()

    report = collect_garbage(db_path, days=7, now=NOW)

    assert report["approvals"] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM approvals WHERE id = 'a1'") == 0
    assert _rows(db_path, "SELECT COUNT(*) FROM approvals WHERE state = 'pending'") == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM approvals WHERE id = 'a3'") == 1


def test_financial_facts_pruned_by_day(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO revenue_facts (day, vertical, source) VALUES (?, 'AcmeUni', 'stripe')",
            (OLD_DAY,),
        )
        conn.execute(
            "INSERT INTO revenue_facts (day, vertical, source) VALUES (?, 'AcmeUni', 'stripe')",
            (RECENT_DAY,),
        )
        conn.execute(
            "INSERT INTO bank_facts (day, account_id, provider) VALUES (?, 'acc', 'mercury')",
            (OLD_DAY,),
        )
    finally:
        conn.close()

    report = collect_garbage(db_path, facts_days=365, now=NOW)

    assert report["revenue_facts"] == 1
    assert report["bank_facts"] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM revenue_facts") == 1
    assert _rows(db_path, f"SELECT COUNT(*) FROM revenue_facts WHERE day = '{RECENT_DAY}'") == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM bank_facts") == 0


def _seed_run(conn: sqlite3.Connection, run_id: str, state: str, finished_at: str | None) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, 't', ?, ?)",
        (f"tsk_{run_id}", OLD_TS, OLD_TS),
    )
    conn.execute(
        "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
        "finished_at, created_at, updated_at) VALUES (?, ?, 'mock', 'tr', ?, ?, ?, ?, ?)",
        (run_id, f"tsk_{run_id}", state, OLD_TS, finished_at, OLD_TS, OLD_TS),
    )
    conn.execute(
        "INSERT INTO steps (run_id, seq, name) VALUES (?, 1, 'plan')",
        (run_id,),
    )


def test_runs_gc_terminal_only_with_children(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        _seed_run(conn, "run_old", "completed", OLD_TS)  # aged + terminal → deleted
        _seed_run(conn, "run_live", "running", None)  # active → kept
        _seed_run(conn, "run_recent", "completed", RECENT_TS)  # recent → kept
        conn.execute(
            "INSERT INTO artifacts (id, run_id, uri, created_at) "
            "VALUES ('art1', 'run_old', 'file://x', ?)",
            (OLD_TS,),
        )
        conn.execute(
            "INSERT INTO approvals (id, run_id, action_class, proposed_action, state, "
            "created_at) VALUES ('ap_old', 'run_old', 'read_only', 'x', 'approved', ?)",
            (OLD_TS,),
        )
    finally:
        conn.close()

    report = collect_garbage(db_path, runs_days=30, now=NOW)

    assert report["runs"] == 1
    assert report["steps"] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM runs") == 2
    assert _rows(db_path, "SELECT COUNT(*) FROM runs WHERE id = 'run_old'") == 0
    assert _rows(db_path, "SELECT COUNT(*) FROM steps WHERE run_id = 'run_old'") == 0
    assert _rows(db_path, "SELECT COUNT(*) FROM artifacts WHERE run_id = 'run_old'") == 0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        for _ in range(3):
            _insert_event(conn, OLD_TS, "run.updated")
    finally:
        conn.close()

    report = collect_garbage(db_path, days=7, replay_keep=0, dry_run=True, now=NOW)

    assert report["events"] >= 1
    # Nothing removed.
    assert _rows(db_path, "SELECT COUNT(*) FROM events") == 3


def test_module_is_runnable(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "omniagentos.maintenance.cache_gc", "--db", db_path, "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "rows total" in result.stdout


def _insert_notification(
    conn: sqlite3.Connection,
    notification_id: str,
    *,
    created_at: str,
    read_at: str | None,
) -> None:
    conn.execute(
        "INSERT INTO notifications "
        "(id, kind, title, body, severity, created_at, read_at, payload_json) "
        "VALUES (?, 'info', 't', '', 'info', ?, ?, '{}')",
        (notification_id, created_at, read_at),
    )


def test_notifications_gc_preserves_unread_uses_real_schema(tmp_path: Path) -> None:
    """H-01: real schema uses read_at; aged unread rows must survive GC."""
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        _insert_notification(conn, "ntf_unread_old", created_at=OLD_TS, read_at=None)
        _insert_notification(conn, "ntf_read_old", created_at=OLD_TS, read_at=OLD_TS)
        _insert_notification(conn, "ntf_unread_recent", created_at=RECENT_TS, read_at=None)
        _insert_notification(conn, "ntf_read_recent", created_at=RECENT_TS, read_at=RECENT_TS)
    finally:
        conn.close()

    report = collect_garbage(db_path, days=7, now=NOW)

    assert report["notifications"] == 1
    conn = sqlite3.connect(db_path)
    try:
        remaining = {
            row[0] for row in conn.execute("SELECT id FROM notifications ORDER BY id").fetchall()
        }
    finally:
        conn.close()
    # Only the aged + already-read row is eligible; every unread row is kept.
    assert remaining == {
        "ntf_unread_old",
        "ntf_unread_recent",
        "ntf_read_recent",
    }
    assert _rows(db_path, "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL") == 2


def test_notifications_gc_dry_run_counts_without_deleting_unread(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    conn = _connect(db_path)
    try:
        _insert_notification(conn, "ntf_unread_old", created_at=OLD_TS, read_at=None)
        _insert_notification(conn, "ntf_read_old", created_at=OLD_TS, read_at=OLD_TS)
    finally:
        conn.close()

    report = collect_garbage(db_path, days=7, dry_run=True, now=NOW)

    assert report["notifications"] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM notifications") == 2
    assert _rows(db_path, "SELECT COUNT(*) FROM notifications WHERE id = 'ntf_unread_old'") == 1


def test_notifications_gc_fail_closed_without_read_at_column(tmp_path: Path) -> None:
    """H-01: missing read_at means the keep-unread predicate is unprovable → no deletes.

    Cutoff is deliberately *after* the row's created_at so an age-only fallback
    would have wiped it; fail-closed must leave the row alone instead.
    """
    db_path = str(tmp_path / "partial.db")
    # RECENT_TS is after OLD_TS; any age-only sweep past this cutoff would hit ntf_1.
    cutoff = RECENT_TS
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO notifications (id, created_at) VALUES ('ntf_1', ?)",
            (OLD_TS,),
        )
        conn.commit()
        deleted = _gc_notifications(conn, cutoff, apply=True)
    finally:
        conn.close()

    assert deleted == {"notifications": 0}
    assert _rows(db_path, "SELECT COUNT(*) FROM notifications") == 1


def test_notifications_gc_fail_closed_when_table_missing(tmp_path: Path) -> None:
    """H-01: absent notifications table is schema uncertainty, not a free wipe."""
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    try:
        # Intentionally no notifications table.
        deleted = _gc_notifications(conn, RECENT_TS, apply=True)
        dry = _gc_notifications(conn, RECENT_TS, apply=False)
    finally:
        conn.close()

    assert deleted == {"notifications": 0}
    assert dry == {"notifications": 0}
