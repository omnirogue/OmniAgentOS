"""Multiple Claude accounts: detection, registry, round-robin selection.

Hermetic — every test uses a tmp DB (migrations applied) and either a monkeypatched
``_home`` or an empty detection, so the machine's real ~/.claude accounts are never
touched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omniagentos.accounts import service as acc
from omniagentos.db.migrate import migrate


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "accounts.db")
    migrate(db)
    return db


def _config_dir(base: Path, name: str, email: str | None = None) -> str:
    directory = base / name
    directory.mkdir()
    if email is not None:
        (directory / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
        )
    return str(directory)


def test_add_enable_default_and_round_robin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])  # no machine accounts
    db = _db(tmp_path)
    dir_a = _config_dir(tmp_path, "cfgA", email="a@example.com")
    dir_b = _config_dir(tmp_path, "cfgB")  # no email -> status unknown

    account_a = acc.add_account(label="A", config_dir=dir_a, enabled=True, db_path=db)
    account_b = acc.add_account(label="B", config_dir=dir_b, enabled=False, db_path=db)

    assert account_a["email"] == "a@example.com"
    assert account_a["enabled"] is True
    assert account_b["enabled"] is False
    # A secret is never surfaced; config-dir accounts have none.
    assert account_a["has_secret"] is False
    assert "secret_ref" not in account_a

    # Only A is enabled -> selection returns A's config dir.
    picked = acc.next_account_for_spawn(db)
    assert picked is not None and picked.config_dir == dir_a and picked.env == {}

    # Enable B -> selection now round-robins across both.
    acc.set_enabled(account_b["id"], True, db_path=db)
    picks = [acc.next_account_for_spawn(db).config_dir for _ in range(6)]  # type: ignore[union-attr]
    assert set(picks) == {dir_a, dir_b}
    # Strict rotation (monotonic cursor): no two CONSECUTIVE picks are the same
    # account, even though these all run within the same clock-second.
    assert all(picks[i] != picks[i + 1] for i in range(len(picks) - 1))

    # Make B the default (implies enabled), exactly one default.
    assert acc.set_default(account_b["id"], db_path=db) is True
    by_id = {a["id"]: a for a in acc.list_accounts(db)}
    assert by_id[account_b["id"]]["is_default"] is True
    assert by_id[account_a["id"]]["is_default"] is False

    # A duplicate config dir is rejected, not silently added.
    with pytest.raises(ValueError):
        acc.add_account(label="dup", config_dir=dir_a, db_path=db)

    # Remove A.
    assert acc.remove_account(account_a["id"], db_path=db) is True
    assert account_a["id"] not in {a["id"] for a in acc.list_accounts(db)}


def test_detect_autoregisters_default_enabled_others_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "_home", lambda: str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "default@example.com"}}), encoding="utf-8"
    )
    _config_dir(tmp_path, ".claude-account-2", email="second@example.com")

    accounts = acc.list_accounts(_db(tmp_path))
    by_email = {a["email"]: a for a in accounts}

    # The default ~/.claude is registered enabled + default; a second account is
    # registered DISABLED (visible, but opt-in for rotation).
    assert by_email["default@example.com"]["is_default"] is True
    assert by_email["default@example.com"]["enabled"] is True
    assert by_email["second@example.com"]["enabled"] is False
    assert by_email["second@example.com"]["is_default"] is False


def test_no_enabled_accounts_selects_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing enabled, selection returns None -> spawner keeps the default login."""
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    account = acc.add_account(
        label="off", config_dir=_config_dir(tmp_path, "cfg"), enabled=False, db_path=db
    )
    assert account["enabled"] is False
    assert acc.next_account_for_spawn(db) is None


def test_token_account_stores_secret_outside_db_and_injects_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    monkeypatch.setattr(acc, "_secrets_dir", lambda: tmp_path / "secrets")
    db = _db(tmp_path)

    account = acc.add_account(
        label="token-acct", auth_type="oauth_token", secret="sk-oauth-XYZ", db_path=db
    )
    # The secret is NEVER in the account dict, only a has_secret flag.
    assert account["has_secret"] is True
    assert "sk-oauth-XYZ" not in json.dumps(account)
    # It lives on disk under var/secrets (0600), referenced indirectly.
    secret_files = list((tmp_path / "secrets").glob("claude-account-*"))
    assert len(secret_files) == 1
    assert secret_files[0].read_text().strip() == "sk-oauth-XYZ"

    # Selection injects it as the CLI auth env var, not a config dir.
    picked = acc.next_account_for_spawn(db)
    assert picked is not None
    assert picked.config_dir is None
    assert picked.env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-XYZ"}


def test_unreadable_secret_ref_is_not_reported_as_has_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roster must not claim has_secret=True when the secret file is gone.

    Reviewer spot-check: ``has_secret = bool(secret_ref)`` presented a phantom
    credential for an enabled row whose file could not exist. Counterfeit:
    only fix spawn/select paths and leave list/get flattering.
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    account = acc.add_account(
        label="phantom", auth_type="oauth_token", secret="sk-temp", db_path=db
    )
    assert account["has_secret"] is True
    secret_files = list(secrets.glob("claude-account-*"))
    assert len(secret_files) == 1
    secret_files[0].unlink()

    listed = acc.get_account(account["id"], db_path=db)
    assert listed is not None
    assert listed["has_secret"] is False
    # Still registered (enabled until selector quarantines) — just not "has secret".
    assert listed["enabled"] is True


def test_spawn_account_from_row_does_not_raise_on_missing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn_account_from_row must not raise when the oauth secret is gone.

    Reviewer gate (production caller): ``routing.limit_state.reserve_account``
    INSERT+LRU+commits, *then* calls spawn_account_from_row. A raise after that
    commit left reservation_count=1 and an advanced successful-pick cursor while
    the account stayed enabled/ok — non-result persisted as a successful pick.

    Counterfeit: re-introduce ``raise ValueError`` on missing oauth secret.
    This helper stays non-raising; selection refuses hollow picks separately.
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    account = acc.add_account(
        label="token-gone", auth_type="oauth_token", secret="sk-will-vanish", db_path=db
    )
    secret_files = list(secrets.glob("claude-account-*"))
    assert len(secret_files) == 1
    secret_files[0].unlink()

    row = {
        "id": account["id"],
        "label": account["label"],
        "auth_type": "oauth_token",
        "config_dir": None,
        "secret_ref": f"claude-account-{account['id']}",
    }
    # Must not raise — production callers may have already committed a pick.
    spawn = acc.spawn_account_from_row(row)
    assert spawn.env == {}
    assert acc._required_auth_env(row) is None


def test_missing_oauth_secret_is_not_a_successful_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """oauth_token accounts with an unreadable secret must not look selected.

    Non-result-as-favourable: next_account_for_spawn used to hand out
    SpawnAccount(env={}) as a real pick and advance last_used_seq.

    Counterfeit: hollow env / empty-string env still looks like a pick.
    Counterfeit: skip once but leave enabled+ok so the next call re-picks it.
    Counterfeit: only make spawn_account_from_row raise (breaks reserve_account)
    while leaving selection still advancing LRU on hollow picks.
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    account = acc.add_account(
        label="token-gone", auth_type="oauth_token", secret="sk-will-vanish", db_path=db
    )
    before = acc.get_account(account["id"], db_path=db)
    assert before is not None
    assert before.get("last_used_seq") is None
    secret_files = list(secrets.glob("claude-account-*"))
    assert len(secret_files) == 1
    secret_files[0].unlink()

    picked = acc.next_account_for_spawn(db)
    assert picked is None

    row = acc.get_account(account["id"], db_path=db)
    assert row is not None
    assert row["enabled"] is False
    assert row["status"] == "error"
    # Failed secret must not advance the successful-pick cursor.
    assert row.get("last_used_seq") is None
    assert row.get("last_used_at") is None
    assert acc.next_account_for_spawn(db) is None


def test_missing_api_key_secret_is_not_a_successful_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_key branch is independent of oauth_token — both must refuse hollow env.

    Counterfeit that would fake a oauth-only fix: restore soft hollow load for
    api_key in next_account while leaving oauth strict. This binds the api_key
    selection path. spawn_account_from_row itself stays non-raising.
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    account = acc.add_account(
        label="key-gone", auth_type="api_key", secret="sk-ant-will-vanish", db_path=db
    )
    secret_path = secrets / f"claude-account-{account['id']}"
    assert secret_path.is_file()
    secret_path.unlink()

    # Non-raising helper (same contract as oauth).
    spawn = acc.spawn_account_from_row(
        {
            "id": account["id"],
            "label": account["label"],
            "auth_type": "api_key",
            "config_dir": None,
            "secret_ref": f"claude-account-{account['id']}",
        }
    )
    assert spawn.env == {}

    picked = acc.next_account_for_spawn(db)
    assert picked is None
    # Never a hollow success with empty or blank ANTHROPIC_API_KEY.
    if picked is not None:  # pragma: no cover — defensive; assert above is the gate
        assert picked.env.get("ANTHROPIC_API_KEY")

    row = acc.get_account(account["id"], db_path=db)
    assert row is not None
    assert row["enabled"] is False
    assert row["status"] == "error"
    assert row.get("last_used_seq") is None


def test_broken_secret_skips_to_next_healthy_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine must continue selection to a usable account, not return None early.

    Counterfeit: `except ValueError: return None` after quarantine — pool still
    has a healthy row, but selection aborts. Counterfeit: advance last_used_seq
    on the broken row before skipping.
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    broken = acc.add_account(
        label="broken-first", auth_type="oauth_token", secret="sk-broken", db_path=db
    )
    healthy = acc.add_account(
        label="healthy-second", auth_type="oauth_token", secret="sk-healthy", db_path=db
    )
    (secrets / f"claude-account-{broken['id']}").unlink()
    # Never-used ranks ahead of used: push healthy down the LRU so broken is
    # the first AVAILABLE candidate regardless of id/created_at tie-breakers.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE claude_accounts SET last_used_seq = 1, last_used_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", healthy["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    broken_before = acc.get_account(broken["id"], db_path=db)
    assert broken_before is not None
    assert broken_before.get("last_used_seq") is None

    picked = acc.next_account_for_spawn(db)
    assert picked is not None
    assert picked.account_id == healthy["id"]
    assert picked.env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-healthy"}

    broken_after = acc.get_account(broken["id"], db_path=db)
    assert broken_after is not None
    assert broken_after["enabled"] is False
    assert broken_after["status"] == "error"
    assert broken_after.get("last_used_seq") is None
    assert broken_after.get("last_used_at") is None

    healthy_after = acc.get_account(healthy["id"], db_path=db)
    assert healthy_after is not None
    assert healthy_after.get("last_used_seq") is not None


def test_selector_pool_beyond_fixed_cutoff_still_picks_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard 64-attempt cap must not report no-account while a healthy row remains.

    Reviewer probe: 64 broken never-used credentials + 1 healthy → fixed
    ``for _ in range(64)`` returned None with the healthy account still enabled
    and loadable. Counterfeit: keep a fixed 64 (or any constant below pool size).
    """
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    broken_ids: list[str] = []
    for i in range(64):
        row = acc.add_account(
            label=f"broken-{i:02d}",
            auth_type="oauth_token",
            secret=f"sk-broken-{i}",
            db_path=db,
        )
        broken_ids.append(row["id"])
        (secrets / f"claude-account-{row['id']}").unlink()

    healthy = acc.add_account(
        label="healthy-beyond-cap",
        auth_type="oauth_token",
        secret="sk-healthy-beyond",
        db_path=db,
    )
    # Never-used ranks first: push healthy down so the 64 broken rows are
    # attempted before it (created_at/id order alone is not enough if healthy
    # sorts earlier by id).
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE claude_accounts SET last_used_seq = 1, last_used_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", healthy["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    picked = acc.next_account_for_spawn(db)
    assert picked is not None, (
        "eligible healthy account remained but selector returned None (fixed attempt cutoff)"
    )
    assert picked.account_id == healthy["id"]
    assert picked.env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-healthy-beyond"}

    # Broken rows were stop-the-lined, not left as flattering enabled/ok picks.
    for broken_id in broken_ids[:3]:  # sample; full 64 would be slow to re-read
        row = acc.get_account(broken_id, db_path=db)
        assert row is not None
        assert row["enabled"] is False
        assert row["status"] == "error"


def test_reserve_account_does_not_commit_unreadable_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reserve_account must not leave a reservation after a missing secret.

    Reviewer reproduction: INSERT+LRU+commit ran, then spawn returned hollow
    env (or raised) — reservation_count=1, last_used_seq advanced, account
    still enabled/ok. Non-result persisted as a successful pick.

    Counterfeit: restore post-INSERT spawn (commit before credential check).
    Counterfeit: catch hollow env and return None without rolling back the
    reservation already written.
    """
    from omniagentos.routing import limit_state

    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    account = acc.add_account(
        label="rsv-gone", auth_type="oauth_token", secret="sk-rsv-vanish", db_path=db
    )
    (secrets / f"claude-account-{account['id']}").unlink()
    before = acc.get_account(account["id"], db_path=db)
    assert before is not None
    assert before.get("last_used_seq") is None

    # Must not raise — broken credential is a skip/quarantine, not a crash.
    result = limit_state.reserve_account("claude", max_inflight=3, db_path=db)
    assert result is None

    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM account_reservations").fetchone()[0]
        row = conn.execute(
            "SELECT enabled, status, last_used_seq, last_used_at FROM claude_accounts WHERE id = ?",
            (account["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert n == 0, f"reservation must not persist for unreadable secret; got count={n}"
    assert row is not None
    assert int(row[0]) == 0  # enabled
    assert row[1] == "error"
    assert row[2] is None  # last_used_seq not advanced
    assert row[3] is None  # last_used_at not advanced


def test_reserve_account_skips_broken_secret_to_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken first candidate must not consume the pick; healthy second wins.

    Counterfeit: raise on first broken row and abort the whole reserve.
    Counterfeit: reserve the broken row then hand out hollow env.
    """
    from omniagentos.routing import limit_state

    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    broken = acc.add_account(
        label="rsv-broken", auth_type="oauth_token", secret="sk-broken", db_path=db
    )
    healthy = acc.add_account(
        label="rsv-healthy", auth_type="oauth_token", secret="sk-healthy", db_path=db
    )
    (secrets / f"claude-account-{broken['id']}").unlink()
    # Push healthy down the LRU so broken is tried first.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE claude_accounts SET last_used_seq = 1, last_used_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", healthy["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = limit_state.reserve_account("claude", max_inflight=3, db_path=db)
    assert result is not None
    assert result.account.account_id == healthy["id"]
    assert result.account.env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-healthy"}

    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM account_reservations").fetchone()[0]
        broken_row = conn.execute(
            "SELECT enabled, status, last_used_seq FROM claude_accounts WHERE id = ?",
            (broken["id"],),
        ).fetchone()
        healthy_row = conn.execute(
            "SELECT enabled, status, last_used_seq FROM claude_accounts WHERE id = ?",
            (healthy["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert n == 1
    assert broken_row is not None
    assert int(broken_row[0]) == 0 and broken_row[1] == "error"
    assert broken_row[2] is None  # broken did not advance LRU
    assert healthy_row is not None
    assert int(healthy_row[0]) == 1 and healthy_row[2] is not None


def test_reserve_distinct_skips_broken_first_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reserve_distinct_accounts must pre-validate credentials like reserve_account.

    Counterfeit: only fix reserve_account; leave _claim post-INSERT spawn so a
    multi-slot claim writes a reservation for the broken row with hollow env.
    """
    from omniagentos.routing import limit_state

    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(acc, "_secrets_dir", lambda: secrets)
    db = _db(tmp_path)

    broken = acc.add_account(
        label="rd-broken", auth_type="oauth_token", secret="sk-rd-broken", db_path=db
    )
    healthy = acc.add_account(
        label="rd-healthy", auth_type="oauth_token", secret="sk-rd-healthy", db_path=db
    )
    (secrets / f"claude-account-{broken['id']}").unlink()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE claude_accounts SET last_used_seq = 1, last_used_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", healthy["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    results = limit_state.reserve_distinct_accounts("claude", n=1, max_inflight=3, db_path=db)
    assert len(results) == 1
    assert results[0].account.account_id == healthy["id"]
    assert results[0].account.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-rd-healthy"

    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM account_reservations").fetchone()[0]
        broken_row = conn.execute(
            "SELECT enabled, status, last_used_seq FROM claude_accounts WHERE id = ?",
            (broken["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert n == 1
    assert broken_row is not None
    assert int(broken_row[0]) == 0 and broken_row[1] == "error"
    assert broken_row[2] is None
