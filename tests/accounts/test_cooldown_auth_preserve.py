"""M-05: auth stop-the-line and disabled accounts survive late cooldowns.

Regression for the review failure: set_account_cooldown and expired-cooldown
sweeps must not erase authentication error status/detail or make a disabled
account appear healthy after transient/quota/overloaded outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.accounts import service as acc
from omniagentos.db.migrate import migrate


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "accounts.db")
    migrate(db)
    return db


def _config_dir(base: Path, name: str) -> str:
    directory = base / name
    directory.mkdir()
    return str(directory)


def _now_offsets() -> tuple[str, str, str]:
    now_dt = datetime.now(UTC)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (now_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (now_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return now, past, future


def _auth_disabled_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    account = acc.add_account(
        label="auth-broken",
        config_dir=_config_dir(tmp_path, "cfg-auth"),
        enabled=True,
        db_path=db,
    )
    account_id = account["id"]
    acc.set_enabled(account_id, False, db_path=db)
    acc.mark_status(account_id, "error", "401 OAuth token revoked", db_path=db)
    return db, account_id


def _row(db: str, account_id: str) -> dict:
    accounts = {a["id"]: a for a in acc.list_accounts(db)}
    return accounts[account_id]


@pytest.mark.parametrize(
    "status_kw,detail",
    [
        ("rate_limited", "transient rate limit"),  # default/transient path
        ("rate_limited", "quota exhausted"),
        (None, "provider overloaded"),  # overloaded: status=None still wrote detail
    ],
)
def test_set_account_cooldown_preserves_auth_error_status_and_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_kw: str | None,
    detail: str,
) -> None:
    db, account_id = _auth_disabled_account(tmp_path, monkeypatch)
    _, _, future = _now_offsets()

    ok = acc.set_account_cooldown(
        account_id,
        future,
        detail,
        db_path=db,
        status=status_kw,
    )
    assert ok is True

    row = _row(db, account_id)
    assert row["enabled"] is False
    assert row["status"] == "error"
    assert row["status_detail"] == "401 OAuth token revoked"
    assert row["cooldown_until"] == future


def test_set_account_cooldown_preserves_operator_disabled_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled rows must not be painted rate_limited/healthy by late cooldowns."""
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    account = acc.add_account(
        label="operator-off",
        config_dir=_config_dir(tmp_path, "cfg-off"),
        enabled=False,
        db_path=db,
    )
    account_id = account["id"]
    acc.mark_status(account_id, "ok", None, db_path=db)
    _, _, future = _now_offsets()

    acc.set_account_cooldown(
        account_id, future, "transient rate limit", db_path=db, status="rate_limited"
    )

    row = _row(db, account_id)
    assert row["enabled"] is False
    assert row["status"] == "ok"
    assert row["status_detail"] is None
    assert row["cooldown_until"] == future


def test_clear_expired_cooldowns_does_not_heal_auth_disabled_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account_id = _auth_disabled_account(tmp_path, monkeypatch)
    now, past, _ = _now_offsets()

    # Late cooldown after auth disable (status preserved by set_account_cooldown).
    acc.set_account_cooldown(
        account_id, past, "transient rate limit", db_path=db, status="rate_limited"
    )

    cleared = acc.clear_expired_cooldowns(now, db_path=db)
    assert account_id in cleared

    row = _row(db, account_id)
    assert row["enabled"] is False
    assert row["status"] == "error"
    assert row["status_detail"] == "401 OAuth token revoked"
    assert row["cooldown_until"] is None


def test_clear_expired_cooldowns_still_heals_enabled_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    account = acc.add_account(
        label="ok-rate",
        config_dir=_config_dir(tmp_path, "cfg-ok"),
        enabled=True,
        db_path=db,
    )
    account_id = account["id"]
    now, past, _ = _now_offsets()

    acc.set_account_cooldown(account_id, past, "rate limited", db_path=db, status="rate_limited")
    cleared = acc.clear_expired_cooldowns(now, db_path=db)
    assert account_id in cleared

    row = _row(db, account_id)
    assert row["enabled"] is True
    assert row["status"] == "ok"
    assert row["status_detail"] is None
    assert row["cooldown_until"] is None


def test_sweep_does_not_ok_disabled_rate_limited_if_status_was_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: even if status is rate_limited, disabled stays non-ok."""
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    account = acc.add_account(
        label="disabled-rl",
        config_dir=_config_dir(tmp_path, "cfg-rl"),
        enabled=True,
        db_path=db,
    )
    account_id = account["id"]
    now, past, _ = _now_offsets()
    # Enable path first so set_account_cooldown may write rate_limited, then disable.
    acc.set_account_cooldown(account_id, past, "rate limited", db_path=db, status="rate_limited")
    acc.set_enabled(account_id, False, db_path=db)

    cleared = acc.clear_expired_cooldowns(now, db_path=db)
    assert account_id in cleared

    row = _row(db, account_id)
    assert row["enabled"] is False
    assert row["status"] == "rate_limited"  # not rewritten to ok
    assert row["cooldown_until"] is None
