"""D10 UltraCode: ``limit_state.reserve_distinct_accounts`` (distinct-preferred,
degrade-to-reuse, never-block).

Hermetic like tests/routing/test_limit_state.py: tmp migrated SQLite DBs,
machine-account detection disabled, accounts config pinned.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.routing import limit_state


@pytest.fixture(autouse=True)
def _no_machine_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never auto-register this machine's real ~/.claude* dirs."""
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])


@pytest.fixture(autouse=True)
def _isolated_accounts_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "providers:\n  claude:\n    cooldown_seconds: 100\n    accounts: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_ACCOUNTS_CONFIG", str(path))


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "distinct.db")
    migrate(path)
    return path


def _conn(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    return connection


def _iso_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_account(
    db: str,
    account_id: str,
    *,
    provider: str = "claude",
    enabled: int = 1,
    status: str = "ok",
    cooldown_until: str | None = None,
) -> None:
    now = utc_now_iso()
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO claude_accounts "
            "(id, label, auth_type, config_dir, enabled, is_default, status, provider, "
            " cooldown_until, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                account_id,
                "config_dir",
                None,
                enabled,
                0,
                status,
                provider,
                cooldown_until,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_three(db: str) -> None:
    for account_id in ("acct_a", "acct_b", "acct_c"):
        _seed_account(db, account_id)


def _account_ids(reservations: list) -> list[str]:
    return [str(r.account.account_id) for r in reservations]


class TestDistinctPreference:
    def test_three_reservations_land_on_three_distinct_accounts(self, db: str) -> None:
        _seed_three(db)
        reservations = limit_state.reserve_distinct_accounts(
            "claude", 3, max_inflight=3, db_path=db
        )
        assert len(reservations) == 3
        assert len(set(_account_ids(reservations))) == 3

    def test_cap_clamps_at_three(self, db: str) -> None:
        """The D10 hard cap is per-ultra-run: n > 3 is clamped, never honored."""
        _seed_three(db)
        reservations = limit_state.reserve_distinct_accounts(
            "claude", 7, max_inflight=3, db_path=db
        )
        assert len(reservations) == 3

    def test_exclude_account_ids_prefers_untouched_accounts(self, db: str) -> None:
        """A top-up call passes the run's already-used accounts: the next
        reservation prefers an account NOT in that set."""
        _seed_three(db)
        reservations = limit_state.reserve_distinct_accounts(
            "claude",
            1,
            exclude_account_ids={"acct_a", "acct_b"},
            max_inflight=3,
            db_path=db,
        )
        assert _account_ids(reservations) == ["acct_c"]


class TestDegrade:
    def test_one_account_cooling_degrades_to_reuse_never_blocks(self, db: str) -> None:
        """3 requested, 2 available (one cooling): distinctness reduces —
        concurrency does not. Two distinct accounts, one reused."""
        _seed_three(db)
        conn = _conn(db)
        try:
            conn.execute(
                "UPDATE claude_accounts SET cooldown_until = ? WHERE id = 'acct_c'",
                (_iso_in(600),),
            )
            conn.commit()
        finally:
            conn.close()

        reservations = limit_state.reserve_distinct_accounts(
            "claude", 3, max_inflight=3, db_path=db
        )

        ids = _account_ids(reservations)
        assert len(reservations) == 3
        assert set(ids) == {"acct_a", "acct_b"}  # cooled account untouched
        assert len([i for i in ids if ids.count(i) > 1]) == 2  # one account reused

    def test_reuse_respects_the_per_account_ceiling(self, db: str) -> None:
        """Degrade reuses only up to max_inflight: 2 accounts x ceiling 1 →
        exactly 2 reservations for a request of 3 (never block, never over-book)."""
        _seed_account(db, "acct_a")
        _seed_account(db, "acct_b")

        reservations = limit_state.reserve_distinct_accounts(
            "claude", 3, max_inflight=1, db_path=db
        )

        assert sorted(_account_ids(reservations)) == ["acct_a", "acct_b"]

    def test_no_capacity_anywhere_returns_empty_not_blocking(self, db: str) -> None:
        _seed_account(db, "acct_a", cooldown_until=_iso_in(600))
        reservations = limit_state.reserve_distinct_accounts(
            "claude", 3, max_inflight=1, db_path=db
        )
        assert reservations == []

    def test_degrade_prefers_non_excluded_reuse(self, db: str) -> None:
        """When only reuse remains, non-excluded accounts are reused before
        the caller's excluded ones."""
        _seed_account(db, "acct_a")
        _seed_account(db, "acct_b")
        reservations = limit_state.reserve_distinct_accounts(
            "claude",
            2,
            exclude_account_ids={"acct_b"},
            max_inflight=2,
            db_path=db,
        )
        assert _account_ids(reservations) == ["acct_a", "acct_a"]


class TestConcurrency:
    def test_two_parallel_reservers_never_over_book(self, db: str) -> None:
        """BEGIN IMMEDIATE serializes the whole loop: two reservers asking for
        2 each against 3 accounts at ceiling 1 split the 3 slots — 3 total,
        no account booked twice."""
        _seed_three(db)
        results: list[list] = []
        lock = threading.Lock()

        def worker() -> None:
            outcome = limit_state.reserve_distinct_accounts("claude", 2, max_inflight=1, db_path=db)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        all_ids = [i for outcome in results for i in _account_ids(outcome)]
        assert len(all_ids) == 3  # 2 + 1 split, never 4
        assert sorted(all_ids) == ["acct_a", "acct_b", "acct_c"]  # no double-booking

    def test_reservations_count_as_inflight_for_later_calls(self, db: str) -> None:
        _seed_three(db)
        first = limit_state.reserve_distinct_accounts("claude", 3, max_inflight=1, db_path=db)
        assert len(first) == 3
        second = limit_state.reserve_distinct_accounts("claude", 3, max_inflight=1, db_path=db)
        assert second == []


class TestHousekeeping:
    def test_release_frees_the_slot(self, db: str) -> None:
        _seed_account(db, "acct_a")
        first = limit_state.reserve_distinct_accounts("claude", 1, max_inflight=1, db_path=db)
        assert len(first) == 1
        assert limit_state.reserve_distinct_accounts("claude", 1, max_inflight=1, db_path=db) == []
        assert limit_state.release_reservation(first[0].id, db_path=db) is True
        assert (
            len(limit_state.reserve_distinct_accounts("claude", 1, max_inflight=1, db_path=db)) == 1
        )

    def test_zero_or_negative_n_is_a_noop(self, db: str) -> None:
        _seed_three(db)
        assert limit_state.reserve_distinct_accounts("claude", 0, db_path=db) == []
        assert limit_state.reserve_distinct_accounts("claude", -2, db_path=db) == []
