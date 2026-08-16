"""M-33: bounded SQLite busy/locked retry — contention behaviour + metrics."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from omniagentos.db.busy import (
    BusyRetryExhausted,
    BusyRetryMetrics,
    BusyRetryPolicy,
    compute_backoff_ms,
    execute_write_transaction,
    is_busy_or_locked,
    reset_busy_retry_metrics,
    run_with_busy_retry,
)
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


def _file_conn(path: Path, *, timeout: float = 0.05) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path), isolation_level=None, timeout=timeout, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def test_is_busy_or_locked_matches_message_and_code() -> None:
    assert is_busy_or_locked(sqlite3.OperationalError("database is locked"))
    assert is_busy_or_locked(sqlite3.OperationalError("database table is locked"))
    assert is_busy_or_locked(sqlite3.OperationalError("SQLITE_BUSY"))
    assert not is_busy_or_locked(
        BusyRetryExhausted(
            "SQLITE_BUSY/LOCKED retries exhausted",
            attempts=3,
            cause=sqlite3.OperationalError("database is locked"),
        )
    )
    assert not is_busy_or_locked(sqlite3.OperationalError("no such table: x"))
    assert not is_busy_or_locked(ValueError("database is locked"))


def test_backoff_is_bounded_and_non_decreasing_base() -> None:
    policy = BusyRetryPolicy(max_attempts=5, base_delay_ms=10, max_delay_ms=40, jitter_ms=0)
    delays = [compute_backoff_ms(i, policy) for i in range(4)]
    assert delays == [10.0, 20.0, 40.0, 40.0]


def test_run_with_busy_retry_succeeds_after_transient_busy() -> None:
    metrics = BusyRetryMetrics()
    state = {"n": 0}

    def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = run_with_busy_retry(
        flaky,
        policy=BusyRetryPolicy(max_attempts=5, base_delay_ms=1, max_delay_ms=2, jitter_ms=0),
        metrics=metrics,
        op="flaky",
    )
    assert result == "ok"
    snap = metrics.snapshot()
    assert snap["successes"] == 1
    assert snap["retries"] == 2
    assert snap["exhausted"] == 0
    assert int(snap["attempts"]) >= 3


def test_run_with_busy_retry_exhausts_and_records_metric() -> None:
    metrics = BusyRetryMetrics()

    def always_busy() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(BusyRetryExhausted) as exc_info:
        run_with_busy_retry(
            always_busy,
            policy=BusyRetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1, jitter_ms=0),
            metrics=metrics,
            op="always_busy",
        )
    assert exc_info.value.attempts == 3
    snap = metrics.snapshot()
    assert snap["exhausted"] == 1
    assert snap["successes"] == 0
    assert snap["retries"] == 2


def test_non_busy_errors_are_not_retried() -> None:
    metrics = BusyRetryMetrics()
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: missing")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run_with_busy_retry(
            boom,
            policy=BusyRetryPolicy(max_attempts=5, base_delay_ms=1, max_delay_ms=1, jitter_ms=0),
            metrics=metrics,
            op="boom",
        )
    assert calls["n"] == 1
    assert metrics.snapshot()["retries"] == 0


def test_execute_write_transaction_contention_then_success(tmp_path: Path) -> None:
    """Real multi-connection BUSY: holder keeps RESERVED; contender retries until free."""
    db_path = tmp_path / "busy.db"
    bootstrap = _file_conn(db_path, timeout=5.0)
    bootstrap.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)")
    bootstrap.execute("INSERT INTO t(id, v) VALUES (1, 'seed')")
    bootstrap.close()

    holder = _file_conn(db_path, timeout=0.0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE t SET v = 'held' WHERE id = 1")

    released = threading.Event()

    def release_after_delay() -> None:
        time.sleep(0.12)
        holder.commit()
        released.set()

    threading.Thread(target=release_after_delay, daemon=True).start()

    contender = _file_conn(db_path, timeout=0.0)
    metrics = BusyRetryMetrics()

    def body(conn: sqlite3.Connection) -> str:
        conn.execute("UPDATE t SET v = 'won' WHERE id = 1")
        return "won"

    # execute_write_transaction opens BEGIN IMMEDIATE itself; contender must not
    # already be inside a transaction.
    result = execute_write_transaction(
        contender,
        body,
        policy=BusyRetryPolicy(max_attempts=30, base_delay_ms=15, max_delay_ms=40, jitter_ms=5),
        metrics=metrics,
        op="contender_update",
    )
    assert result == "won"
    assert released.wait(timeout=2.0)
    row = contender.execute("SELECT v FROM t WHERE id = 1").fetchone()
    assert row is not None and row["v"] == "won"
    snap = metrics.snapshot()
    assert snap["successes"] == 1
    assert int(snap["retries"]) >= 1, f"expected real contention retries, got {snap}"
    contender.close()
    holder.close()


def test_execute_write_transaction_exhaustion_under_held_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "exhaust.db"
    bootstrap = _file_conn(db_path, timeout=5.0)
    bootstrap.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)")
    bootstrap.execute("INSERT INTO t(id, v) VALUES (1, 'seed')")
    bootstrap.close()

    holder = _file_conn(db_path, timeout=0.0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE t SET v = 'held' WHERE id = 1")

    contender = _file_conn(db_path, timeout=0.0)
    metrics = BusyRetryMetrics()

    with pytest.raises(BusyRetryExhausted):
        execute_write_transaction(
            contender,
            lambda conn: conn.execute("UPDATE t SET v = 'nope' WHERE id = 1"),
            policy=BusyRetryPolicy(max_attempts=2, base_delay_ms=5, max_delay_ms=5, jitter_ms=0),
            metrics=metrics,
            op="exhaust",
        )
    assert metrics.snapshot()["exhausted"] == 1
    # Holder still owns the row; contender must not have committed.
    holder.commit()
    row = holder.execute("SELECT v FROM t WHERE id = 1").fetchone()
    assert row is not None and row["v"] == "held"
    contender.close()
    holder.close()


def test_transaction_safe_retry_does_not_double_apply() -> None:
    """A full-unit retry re-invokes the whole body; partial work is not left applied.

    Simulate: first attempt raises BUSY *after* mutating an in-memory counter
    but before commit (via run_with_busy_retry on a body that rolls back). The
    successful attempt must leave exactly one committed side effect.
    """
    metrics = BusyRetryMetrics()
    applied: list[str] = []
    state = {"n": 0}

    def unit() -> str:
        state["n"] += 1
        # Simulate work that would be dangerous if retried without rollback.
        applied.append(f"attempt-{state['n']}")
        if state["n"] == 1:
            # Caller contract: raise before commit → no durable side effect.
            applied.pop()
            raise sqlite3.OperationalError("database is locked")
        return "committed"

    result = run_with_busy_retry(
        unit,
        policy=BusyRetryPolicy(max_attempts=5, base_delay_ms=1, max_delay_ms=2, jitter_ms=0),
        metrics=metrics,
        op="once",
    )
    assert result == "committed"
    assert applied == ["attempt-2"], f"expected one successful unit of work, got {applied}"
    assert metrics.snapshot()["retries"] == 1


def test_sqlite_store_write_uses_busy_retry_under_contention(tmp_path: Path) -> None:
    """Owned store path: SqliteStore._write retries when another connection holds the DB."""
    from omniagentos.contracts import utc_now_iso

    reset_busy_retry_metrics()
    db_path = tmp_path / "store.db"
    store = make_store(SqliteStore, str(db_path), wal_checkpoint_interval_s=0)
    now = utc_now_iso()
    assert store.create_discipline(
        {
            "id": "d1",
            "name": "Disc",
            "metric_contract": "{}",
            "status": "active",
            "created_at": now,
        }
    )

    holder = _file_conn(Path(db_path), timeout=0.0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("SELECT 1 FROM disciplines LIMIT 1")

    def release() -> None:
        time.sleep(0.1)
        holder.commit()

    threading.Thread(target=release, daemon=True).start()

    # Force immediate BUSY at the SQLite layer so the app-level retry is exercised
    # (default busy_timeout would otherwise wait inside SQLite).
    store._connection.execute("PRAGMA busy_timeout=0")

    assert store.create_discipline(
        {
            "id": "d2",
            "name": "Disc2",
            "metric_contract": "{}",
            "status": "active",
            "created_at": now,
        }
    )
    row = store._connection.execute("SELECT id FROM disciplines WHERE id = 'd2'").fetchone()
    assert row is not None
    holder.close()
    store.close()


def test_handoff_api_surface_is_importable() -> None:
    """L09/L10 adoption surface: named exports only, no private store wiring."""
    import omniagentos.db.busy as busy

    for name in (
        "BusyRetryPolicy",
        "BusyRetryMetrics",
        "BusyRetryExhausted",
        "run_with_busy_retry",
        "execute_write_transaction",
        "is_busy_or_locked",
        "busy_retry_metrics_snapshot",
        "reset_busy_retry_metrics",
        "DEFAULT_BUSY_RETRY_POLICY",
        "BUSY_RETRY_METRICS",
    ):
        assert hasattr(busy, name), name
    assert "run_with_busy_retry" in busy.__all__
    assert "execute_write_transaction" in busy.__all__
