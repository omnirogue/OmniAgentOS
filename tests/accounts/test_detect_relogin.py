"""Re-login reconciliation: a config dir's registered identity must track reality.

``claude /login`` inside an existing config dir swaps which account it
authenticates as, without telling the registry. Detection has to notice, or the
row keeps describing an account that no longer lives there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.accounts import service as acc
from omniagentos.db.migrate import migrate


def _login_as(directory: Path, email: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
    )


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "accounts.db")
    migrate(path)
    return path


def _detect_once(monkeypatch: pytest.MonkeyPatch, directory: Path, email: str) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [(str(directory), email)])


def test_relogin_updates_the_stored_identity(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "cfg"
    _login_as(directory, "old@example.com")
    _detect_once(monkeypatch, directory, "old@example.com")
    acc.list_accounts(db_path=db)

    _login_as(directory, "new@example.com")
    _detect_once(monkeypatch, directory, "new@example.com")
    account = acc.list_accounts(db_path=db)[0]

    assert account["email"] == "new@example.com"
    assert account["label"] == "new@example.com"


def test_relogin_clears_the_previous_logins_error(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug that benched a healthy account: an error recorded against the
    account that USED to live in this dir kept describing the one that does."""
    directory = tmp_path / "cfg"
    _login_as(directory, "old@example.com")
    _detect_once(monkeypatch, directory, "old@example.com")
    account_id = acc.list_accounts(db_path=db)[0]["id"]
    acc.mark_status(account_id, "error", "OAuth session expired", db_path=db)

    _login_as(directory, "new@example.com")
    _detect_once(monkeypatch, directory, "new@example.com")
    account = acc.list_accounts(db_path=db)[0]

    assert account["status"] == "ok"
    assert account["status_detail"] == "re-login detected: was old@example.com"


def test_relogin_does_not_re_enable_a_disabled_account(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`enabled` belongs to the operator and to the auth stop-the-line."""
    directory = tmp_path / "cfg"
    _login_as(directory, "old@example.com")
    _detect_once(monkeypatch, directory, "old@example.com")
    account_id = acc.list_accounts(db_path=db)[0]["id"]
    acc.set_enabled(account_id, False, db_path=db)

    _login_as(directory, "new@example.com")
    _detect_once(monkeypatch, directory, "new@example.com")

    assert acc.get_account(account_id, db_path=db)["enabled"] is False


def test_operator_chosen_label_survives_a_relogin(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "cfg"
    _login_as(directory, "old@example.com")
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    acc.add_account(label="Billing account", config_dir=str(directory), db_path=db)

    _login_as(directory, "new@example.com")
    _detect_once(monkeypatch, directory, "new@example.com")
    account = acc.list_accounts(db_path=db)[0]

    assert account["label"] == "Billing account"  # not overwritten
    assert account["email"] == "new@example.com"  # but identity still corrected


def test_unchanged_login_leaves_status_alone(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spurious reset: only a CHANGED email invalidates recorded health."""
    directory = tmp_path / "cfg"
    _login_as(directory, "same@example.com")
    _detect_once(monkeypatch, directory, "same@example.com")
    account_id = acc.list_accounts(db_path=db)[0]["id"]
    acc.mark_status(account_id, "error", "genuinely broken", db_path=db)

    acc.list_accounts(db_path=db)  # detection runs again, same login

    account = acc.get_account(account_id, db_path=db)
    assert account["status"] == "error"
    assert account["status_detail"] == "genuinely broken"


def test_first_readable_email_is_still_backfilled(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "cfg"
    directory.mkdir()
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    account_id = acc.add_account(  # registered before any login existed
        label="fresh", config_dir=str(directory), db_path=db
    )["id"]
    assert acc.get_account(account_id, db_path=db)["status"] == "unknown"

    _login_as(directory, "arrived@example.com")
    _detect_once(monkeypatch, directory, "arrived@example.com")
    acc.list_accounts(db_path=db)  # get_account alone does not run detection
    account = acc.get_account(account_id, db_path=db)

    assert account["email"] == "arrived@example.com"
    assert account["status"] == "ok"
