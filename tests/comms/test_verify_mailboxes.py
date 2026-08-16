"""Unit tests for the mailbox-access verifier's security logic.

``scripts/email/verify_mailboxes.py`` is a script, not a package module, so it is loaded
via importlib. Importing it is side-effect-free (network/env only touched inside main()).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_VM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "email" / "verify_mailboxes.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_mailboxes_under_test", _VM_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass/module lookups need this registered first
    spec.loader.exec_module(mod)
    return mod


vm = _load()

_READ = "https://www.googleapis.com/auth/gmail.readonly"
_SEND = "https://www.googleapis.com/auth/gmail.send"


def _idtok(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return "h." + payload + ".s"


@pytest.mark.parametrize("bad", ["٤٤٣", "²", "+443", " ", "46a5", "0", "70000"])
def test_resolve_port_rejects_non_ascii_and_junk(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("PORT_UT", bad)
    port, err = vm._resolve_port("PORT_UT", 465)
    assert port is None and err


def test_resolve_port_valid_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT_UT", " 465 ")
    assert vm._resolve_port("PORT_UT", 465) == (465, None)
    monkeypatch.delenv("PORT_UT", raising=False)
    assert vm._resolve_port("PORT_UT", 465) == (465, None)  # absent -> default


def test_email_from_id_token_is_shape_bounded() -> None:
    assert vm._email_from_id_token(_idtok("owner@Example.com")) == "owner@example.com"
    # a token/secret-shaped (non-email) claim must NOT be surfaced
    assert vm._email_from_id_token(_idtok("not-an-email-just-a-token-string")) is None
    assert vm._email_from_id_token("garbage") is None
    assert vm._email_from_id_token(_idtok("x" * 300 + "@e.com")) is None  # length-bounded


def test_check_google_flags_wrong_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_refresh_google_token",
                        lambda mb: (vm.OK, f"openid email {_READ} {_SEND}", "wrong@other.com", None))
    res = vm.check_google({"address": "owner@initech.example"})
    assert res["read"] == vm.WRONG_ACCOUNT and res["send"] == vm.WRONG_ACCOUNT


def test_check_google_scope_is_exact_not_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_refresh_google_token",
                        lambda mb: (vm.OK, "openid email https://mail.google.com.evil/", "owner@initech.example", None))
    res = vm.check_google({"address": "owner@initech.example"})
    assert res["read"] == vm.MISSING_SCOPE and res["send"] == vm.MISSING_SCOPE


def test_check_google_read_requires_read_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_refresh_google_token",
                        lambda mb: (vm.OK, f"openid email {_SEND}", "owner@initech.example", None))
    res = vm.check_google({"address": "owner@initech.example"})
    assert res["read"] == vm.MISSING_SCOPE and res["send"] == vm.OK


def test_check_google_missing_address_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_refresh_google_token",
                        lambda mb: (vm.OK, f"openid email {_READ} {_SEND}", "owner@x.com", None))
    res = vm.check_google({})
    assert res["read"] == vm.UNKNOWN and res["send"] == vm.UNKNOWN


def test_oauth_error_allowlist_is_exact() -> None:
    assert "invalid_grant" in vm._GOOGLE_OAUTH_ERRORS
    assert "refresh_token_canary_xyz" not in vm._GOOGLE_OAUTH_ERRORS
