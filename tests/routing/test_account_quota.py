"""M5: durable per-account weekly quota enforcement in ``AccountPool``."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.routing.account_pool import AccountPool
from omniagentos.routing.config import Account, AccountPoolConfig, ProviderPool
from omniagentos.routing.limit_state import weekly_attempts_quota_remaining
from tests.support.db_template import migrated_db


@pytest.fixture(autouse=True)
def _no_machine_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])


@pytest.fixture
def db(tmp_path: Path) -> str:
    return migrated_db(SqliteStore, tmp_path / "account_quota.db")


def _seed_account(
    db: str,
    config_dir: Path,
    *,
    quota: int = 250,
    used: int = 0,
    reset_at: str | None = None,
) -> None:
    now = utc_now_iso()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO claude_accounts "
            "(id, label, auth_type, config_dir, enabled, is_default, status, provider, "
            "weekly_attempt_quota, weekly_attempts_used, quota_reset_at, created_at, updated_at) "
            "VALUES (?, ?, 'config_dir', ?, 1, 0, 'ok', 'claude', ?, ?, ?, ?, ?)",
            ("acct_quota", "quota", str(config_dir), quota, used, reset_at, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _pool(db: str, tmp_path: Path, **quota: Any) -> AccountPool:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    quota.setdefault(
        "reset_at", (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _seed_account(db, config_dir, **quota)
    return AccountPool(
        AccountPoolConfig(
            providers={
                "claude": ProviderPool(
                    cooldown_seconds=60,
                    accounts=[Account(id="configured-quota-account", config_dir=str(config_dir))],
                )
            }
        ),
        durable_db_path=db,
    )


def test_quota_exhaustion_skips_account(db: str, tmp_path: Path) -> None:
    pool = _pool(db, tmp_path, quota=20, used=20)

    assert pool.pick("claude") is None


def test_reserve_floor_not_consumed_by_automated(db: str, tmp_path: Path) -> None:
    # The configured default reserve floor is ten attempts.
    pool = _pool(db, tmp_path, quota=20, used=10)

    assert pool.pick("claude") is None


def test_interactive_caller_gets_reserve(db: str, tmp_path: Path) -> None:
    pool = _pool(db, tmp_path, quota=20, used=10)

    account = pool.pick("claude", interactive=True)

    assert account is not None
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT weekly_attempts_used FROM claude_accounts WHERE id = 'acct_quota'"
        ).fetchone()[0] == 11
    finally:
        conn.close()


def test_quota_reset_weekly(db: str, tmp_path: Path) -> None:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    expired = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_account(db, config_dir, quota=20, used=20, reset_at=expired)

    remaining, reserve_floor, can_use_reserve = weekly_attempts_quota_remaining(
        "acct_quota", "claude", db, interactive=False
    )

    assert (remaining, reserve_floor, can_use_reserve) == (20, 10, False)
    conn = sqlite3.connect(db)
    try:
        used, reset_at = conn.execute(
            "SELECT weekly_attempts_used, quota_reset_at FROM claude_accounts WHERE id = 'acct_quota'"
        ).fetchone()
        assert used == 0
        assert datetime.fromisoformat(reset_at.replace("Z", "+00:00")) > datetime.now(UTC)
    finally:
        conn.close()


def test_quota_read_failure_fails_closed(tmp_path: Path) -> None:
    # An empty SQLite file has no claude_accounts table: it must not expose
    # capacity or the interactive reserve to an automated caller.
    remaining, reserve_floor, can_use_reserve = weekly_attempts_quota_remaining(
        "acct_missing", "claude", str(tmp_path / "unmigrated.db"), interactive=False
    )

    assert (remaining, reserve_floor, can_use_reserve) == (0, 10, False)
