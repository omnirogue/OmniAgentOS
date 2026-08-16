"""Unit tests for the agent-facing file-search brief hint."""

from __future__ import annotations

import sys

import pytest

from omniagentos.filesearch.hint import ENV_HINT, brief_hint, hint_enabled


def test_hint_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_HINT, raising=False)
    assert not hint_enabled()


def test_hint_enabled_only_by_exact_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_HINT, "1")
    assert hint_enabled()
    monkeypatch.setenv(ENV_HINT, "true")
    assert not hint_enabled()
    monkeypatch.setenv(ENV_HINT, "0")
    assert not hint_enabled()


def test_brief_hint_gives_a_runnable_command_from_any_cwd() -> None:
    hint = brief_hint()
    # sys.executable (the product venv) makes the command cwd-independent —
    # a bare "python" would resolve to whatever is on the agent's PATH.
    assert sys.executable in hint
    assert "-m omniagentos.filesearch" in hint
    assert hint.startswith("<file-search>")
    assert hint.endswith("</file-search>")
