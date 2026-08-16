"""Deterministic resource-behaviour guards for steady-state helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omniagentos.sessions import token


def test_token_load_does_not_sleep_to_mask_resource_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token reads are direct filesystem work, never a wall-clock throttle."""
    override = tmp_path / "sessions-token"
    monkeypatch.setattr(token, "TOKEN_PATH", override)

    def forbidden_sleep(seconds: float) -> None:
        pytest.fail(f"steady-state token load slept {seconds}s instead of exposing resource churn")

    monkeypatch.setattr(time, "sleep", forbidden_sleep)

    first = token.load_or_create_token()
    second = token.load_or_create_token()

    assert first == second
    assert override.read_text(encoding="utf-8").strip() == first
