"""Coverage for the per-session, narrowly-scoped hook-eval credential (AC-policy hook-auth)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omniagentos.sessions import hook_token


@pytest.fixture(autouse=True)
def _isolated_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")


def test_issue_creates_an_owner_only_random_token_per_session() -> None:
    token_a = hook_token.issue_hook_token("ses_a")
    token_b = hook_token.issue_hook_token("ses_b")
    assert token_a and token_b
    assert token_a != token_b
    path = hook_token.hook_token_path("ses_a")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == token_a


def test_issue_rotates_on_repeated_calls_for_the_same_session() -> None:
    first = hook_token.issue_hook_token("ses_a")
    second = hook_token.issue_hook_token("ses_a")
    assert first != second
    assert hook_token.read_hook_token("ses_a") == second


def test_read_missing_token_returns_none() -> None:
    assert hook_token.read_hook_token("ses_never_issued") is None


def test_verify_matches_only_the_exact_current_token() -> None:
    token = hook_token.issue_hook_token("ses_a")
    assert hook_token.verify_hook_token("ses_a", token) is True
    assert hook_token.verify_hook_token("ses_a", "wrong") is False
    assert hook_token.verify_hook_token("ses_a", None) is False
    assert hook_token.verify_hook_token("ses_a", "") is False


def test_verify_rejects_a_different_sessions_own_valid_token() -> None:
    """SEC: a real, currently-valid credential for ses_a does not authorize ses_b --
    the check is bound to the exact session_id it was minted for, not just 'any
    live hook token'."""
    token_for_a = hook_token.issue_hook_token("ses_a")
    hook_token.issue_hook_token("ses_b")
    assert hook_token.verify_hook_token("ses_b", token_for_a) is False


def test_verify_unknown_session_is_false_not_an_error() -> None:
    assert hook_token.verify_hook_token("ses_never_issued", "anything") is False


def test_revoke_removes_the_file_and_is_idempotent() -> None:
    hook_token.issue_hook_token("ses_a")
    hook_token.revoke_hook_token("ses_a")
    assert hook_token.read_hook_token("ses_a") is None
    hook_token.revoke_hook_token("ses_a")  # missing file: never raises


def test_session_id_cannot_escape_the_tokens_root_via_path_separators() -> None:
    """Defense in depth: every real id is a contracts.new_id('ses') hex slug, but
    a malformed/attacker-supplied id must still fail closed and stay confined to
    HOOK_TOKENS_ROOT rather than resolving elsewhere on disk."""
    path = hook_token.hook_token_path("../../etc/passwd")
    assert hook_token.HOOK_TOKENS_ROOT in path.parents


def test_empty_session_id_after_sanitization_raises() -> None:
    with pytest.raises(ValueError):
        hook_token.hook_token_path("../../")
