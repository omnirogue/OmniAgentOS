"""Durable rate-limit state integration with the default account pool.

Tests that the process-wide singleton (get_default_pool) correctly wires
durable_db_path so that cooldowns written by other processes (longhaul engine,
sessions daemon) are honored in production. Also verifies that in-memory
behavior remains available for tests (via reset_default_pool).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.routing import limit_state
from omniagentos.routing.account_pool import (
    AccountPool,
    Outcome,
    get_default_pool,
    reset_default_pool,
)
from omniagentos.routing.config import Account, AccountPoolConfig, ProviderPool
from tests.support.db_template import migrated_db


class FakeClock:
    """Deterministic, manually-advanced clock for cooldown tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(autouse=True)
def _reset_default_pool() -> None:
    """Clear the process singleton before and after each test."""
    reset_default_pool(None)
    yield
    reset_default_pool(None)


@pytest.fixture(autouse=True)
def _no_machine_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never auto-register this machine's real ~/.claude* dirs."""
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])


@pytest.fixture(autouse=True)
def _isolated_accounts_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin configs/accounts.yaml to a known transient base (claude: 100s)."""
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "providers:\n  claude:\n    cooldown_seconds: 100\n    accounts: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_ACCOUNTS_CONFIG", str(path))


@pytest.fixture
def db(tmp_path: Path) -> str:
    """Migrated SQLite DB for testing durable state."""
    return migrated_db(SqliteStore, tmp_path / "account_pool_durable.db")


def _conn(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    return connection


def _seed_account(
    db: str,
    account_id: str,
    *,
    provider: str = "claude",
    enabled: int = 1,
    status: str = "ok",
    cooldown_until: str | None = None,
    config_dir: str | None = None,
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
                config_dir,
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


def _account_row(db: str, account_id: str) -> dict:
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT * FROM claude_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _iso_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool_for(db: str, tmp_path: Path, clock: FakeClock, ttl: float = 5.0) -> AccountPool:
    """Create an AccountPool wired to a durable database (like _pool_for in test_limit_state)."""
    config_dir = tmp_path / "cfg-pool"
    config_dir.mkdir(exist_ok=True)
    _seed_account(db, "acct_pool", config_dir=str(config_dir))
    config = AccountPoolConfig(
        providers={
            "claude": ProviderPool(
                cooldown_seconds=60,
                accounts=[Account(id="account-1", config_dir=str(config_dir))],
            )
        }
    )
    return AccountPool(config, now=clock, durable_db_path=db, durable_ttl_seconds=ttl)


class TestDefaultPoolDurable:
    """get_default_pool() wires durable_db_path correctly."""

    def test_default_pool_uses_durable_db_path(
        self, db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default singleton pool is constructed with durable_db_path set."""
        # Set OMNIAGENTOS_DB so default_db_path() resolves to our test db.
        monkeypatch.setenv("OMNIAGENTOS_DB", db)
        # Get the default pool (singleton, wired with durable_db_path).
        pool = get_default_pool()
        assert pool._durable_db_path == db

    def test_default_pool_ttl_is_default(self, db: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default singleton uses the standard durable TTL."""
        monkeypatch.setenv("OMNIAGENTOS_DB", db)
        pool = get_default_pool()
        from omniagentos.routing.account_pool import DEFAULT_DURABLE_TTL_SECONDS

        assert pool._durable_ttl == DEFAULT_DURABLE_TTL_SECONDS

    def test_durable_cooldown_honored(self, db: str, tmp_path: Path) -> None:
        """A pool with durable_db_path honors cooldowns written by other processes."""
        clock = FakeClock()
        pool = _pool_for(db, tmp_path, clock)
        # Simulate another process (longhaul engine) cooling the account durably.
        limit_state.report_outcome(
            "claude",
            "acct_pool",
            "transient_rate_limit",
            db_path=db,
        )
        # The pool should see the durable cooldown and skip the account.
        picked = pool.pick("claude")
        assert picked is None

    def test_durable_write_through_rate_limited(self, db: str, tmp_path: Path) -> None:
        """A pool with durable_db_path writes RATE_LIMITED outcomes to durable state."""
        clock = FakeClock()
        pool = _pool_for(db, tmp_path, clock)
        # Pick and report a rate-limited outcome.
        picked = pool.pick("claude")
        assert picked is not None
        pool.report("account-1", Outcome.RATE_LIMITED)
        # The durable store should record the cooldown.
        row = _account_row(db, "acct_pool")
        assert row["cooldown_until"] is not None
        assert row["status"] == "rate_limited"

    def test_durable_write_through_ok(self, db: str, tmp_path: Path) -> None:
        """A pool with durable_db_path writes OK outcomes to durable state."""
        clock = FakeClock()
        pool = _pool_for(db, tmp_path, clock)
        # Pick and report an OK outcome.
        picked = pool.pick("claude")
        assert picked is not None
        pool.report("account-1", Outcome.OK)
        # The durable store should record OK status.
        row = _account_row(db, "acct_pool")
        assert row["status"] == "ok"
        assert row["cooldown_until"] is None

    def test_in_memory_behavior_available_for_tests(self, tmp_path: Path) -> None:
        """Tests can still pass durable_db_path=None to get pure in-memory pools."""
        # Construct a pool with durable_db_path=None for pure in-memory behavior.
        config_dir = tmp_path / "cfg-test"
        config_dir.mkdir(exist_ok=True)
        config = AccountPoolConfig(
            providers={
                "claude": ProviderPool(
                    cooldown_seconds=60,
                    accounts=[Account(id="account-1", config_dir=str(config_dir))],
                )
            }
        )
        clock = FakeClock()
        pool = AccountPool(config, now=clock, durable_db_path=None)
        # Verify it has no durable path.
        assert pool._durable_db_path is None
        # Pure in-process behavior: pick, cool, and verify recovery works.
        first = pool.pick("claude")
        assert first is not None
        pool.report("account-1", Outcome.RATE_LIMITED)
        # Account is cooling; can't pick it.
        second = pool.pick("claude")
        assert second is None
        # Advance the clock past the in-memory cooldown.
        clock.advance(61)
        third = pool.pick("claude")
        assert third is not None
