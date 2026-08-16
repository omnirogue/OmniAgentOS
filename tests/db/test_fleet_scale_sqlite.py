"""SQLite settings the 200-agent fleet depends on, and the hot-count query plans.

"database is locked" is the single most likely failure mode at 200 concurrent
writers, and it has exactly two mitigations at the connection layer: WAL (so
readers never block the writer) and a non-zero busy_timeout (so a writer queues
instead of erroring). Both were already in place when this package audited them
-- these tests exist so they STAY in place, because losing either one is
invisible until the fleet is wide.

The query-plan assertions cover the other half: a count that degrades to a full
table scan is fine at 12 sessions and pathological at 200 with a year of
history behind it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore, _connect
from omniagentos.routing import limit_state
from tests.support.db_template import make_store


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.db")
    make_store(SqliteStore, path)
    return path


class TestConnectionSettings:
    def test_wal_is_enabled(self, db: str) -> None:
        conn = _connect(db)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()

    def test_busy_timeout_is_non_zero(self, db: str) -> None:
        """A zero busy_timeout turns every write collision into an immediate
        SQLITE_BUSY. At 200 agents that is not an edge case, it is the norm."""
        conn = _connect(db)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        finally:
            conn.close()

    def test_wal_survives_reconnect(self, db: str) -> None:
        """journal_mode is a property of the FILE, so every later connection --
        including the ones that open the db without going through _connect --
        inherits WAL."""
        _connect(db).close()
        raw = sqlite3.connect(db)
        try:
            assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            raw.close()

    def test_swarm_dal_sets_both(self, db: str) -> None:
        from omniagentos.swarm.dal import SwarmDal

        dal = SwarmDal(db)
        try:
            conn = dal._connection
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        finally:
            dal.close()

    def test_concurrent_writers_do_not_raise_database_is_locked(self, db: str) -> None:
        """Two connections interleaving short write transactions must queue on
        the busy timeout, not fail. This is the 200-agent failure mode in
        miniature."""
        first = _connect(db)
        second = _connect(db)
        try:
            first.execute("BEGIN IMMEDIATE")
            first.execute(
                "INSERT INTO sessions "
                "(id, source, project_dir, provider, state, created_at, updated_at) "
                "VALUES ('ses_a', 'bridge', '/tmp', 'claude', 'running', ?, ?)",
                (utc_now_iso(), utc_now_iso()),
            )
            first.commit()
            second.execute("BEGIN IMMEDIATE")
            second.execute(
                "INSERT INTO sessions "
                "(id, source, project_dir, provider, state, created_at, updated_at) "
                "VALUES ('ses_b', 'bridge', '/tmp', 'claude', 'running', ?, ?)",
                (utc_now_iso(), utc_now_iso()),
            )
            second.commit()
            # A reader on a THIRD connection sees both without blocking (WAL).
            reader = _connect(db)
            try:
                assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
            finally:
                reader.close()
        finally:
            first.close()
            second.close()


class TestHotCountQueryPlans:
    """Every count the scheduler runs per pass must be index-served."""

    @staticmethod
    def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
        return " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_live_session_count_uses_an_index(self, db: str) -> None:
        conn = _connect(db)
        try:
            plan = self._plan(
                conn,
                "SELECT COUNT(*) AS n FROM sessions WHERE state IN ('starting','running') "
                "AND kill_requested = 0 "
                "AND COALESCE(last_activity_at, updated_at, created_at) >= ?",
                ("2020-01-01T00:00:00Z",),
            )
            assert "SCAN sessions" not in plan, plan
            assert "idx_sessions_state" in plan, plan
        finally:
            conn.close()

    def test_per_account_inflight_uses_the_compound_index(self, db: str) -> None:
        conn = _connect(db)
        try:
            plan = self._plan(
                conn,
                "SELECT account_id, COUNT(*) AS n FROM sessions "
                "WHERE account_id IN (?) AND state IN ('starting','running') "
                "AND kill_requested = 0 "
                "AND COALESCE(last_activity_at, updated_at, created_at) >= ? "
                "GROUP BY account_id",
                ("acct_1", "2020-01-01T00:00:00Z"),
            )
            assert "idx_sessions_account_state" in plan, plan
        finally:
            conn.close()

    def test_live_swarm_attempt_count_uses_the_partial_index(self, db: str) -> None:
        """``ended_at IS NULL`` is served by the partial live-attempt index, so
        the count never walks the (permanently growing) attempt history."""
        conn = _connect(db)
        try:
            plan = self._plan(
                conn, "SELECT COUNT(*) AS n FROM swarm_attempts WHERE ended_at IS NULL"
            )
            assert "idx_swarm_attempts_live" in plan, plan
        finally:
            conn.close()

    def test_active_board_listing_skips_the_archive(self, db: str) -> None:
        """board_sweep now asks SQLite for active cards instead of reading every
        archived row and filtering in Python."""
        conn = _connect(db)
        try:
            plan = self._plan(
                conn,
                "SELECT * FROM board_tasks WHERE archived_at IS NULL "
                "ORDER BY created_at DESC, id DESC",
            )
            assert "idx_board_tasks_archived_created" in plan, plan
        finally:
            conn.close()

    def test_active_swarm_run_count_uses_an_index(self, db: str) -> None:
        conn = _connect(db)
        try:
            plan = self._plan(
                conn,
                "SELECT COUNT(*) AS n FROM swarm_runs "
                "WHERE status IN ('planning','running','merging')",
            )
            assert "idx_swarm_runs_status" in plan, plan
        finally:
            conn.close()


class TestBatchedInflightQueryCount:
    """The reservation path holds a write lock while it counts. That count must
    cost a FIXED number of queries, not one fan-out per account."""

    def test_query_count_is_flat_in_the_account_pool_size(self, db: str) -> None:
        conn = _connect(db)
        now = utc_now_iso()
        try:
            for index in range(40):
                conn.execute(
                    "INSERT INTO claude_accounts "
                    "(id, label, config_dir, enabled, status, provider, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'ok', 'claude', ?, ?)",
                    (f"acct_{index}", f"a{index}", f"/tmp/a{index}", now, now),
                )
            conn.commit()

            counted: list[str] = []
            conn.set_trace_callback(counted.append)
            try:
                limit_state.inflight_by_account(conn, [f"acct_{index}" for index in range(40)])
            finally:
                conn.set_trace_callback(None)
            # 3 counting queries + 2 sqlite_master probes, for 40 accounts.
            # The per-account version would have issued 200.
            assert len(counted) <= 5, counted
        finally:
            conn.close()

    def test_reserve_account_holds_the_write_lock_briefly(self, db: str) -> None:
        """Sanity that the batched read did not break reservation semantics."""
        conn = _connect(db)
        now = utc_now_iso()
        try:
            for index in range(3):
                conn.execute(
                    "INSERT INTO claude_accounts "
                    "(id, label, config_dir, enabled, status, provider, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'ok', 'claude', ?, ?)",
                    (f"acct_{index}", f"a{index}", f"/tmp/a{index}", now, now),
                )
            conn.commit()
        finally:
            conn.close()
        ceiling = limit_state.max_inflight_per_account("claude")
        reservations = []
        for _ in range(3 * ceiling):
            reservation = limit_state.reserve_account("claude", db_path=db)
            assert reservation is not None
            reservations.append(reservation)
        # Every account is now at its ceiling: the next request must be refused.
        assert limit_state.reserve_account("claude", db_path=db) is None
        assert len({r.account.account_id for r in reservations}) == 3
