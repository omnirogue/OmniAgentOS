"""Visible check. Run: python -m pytest check_retry.py"""

from __future__ import annotations

import retry


def test_permanent_client_error_is_not_retried() -> None:
    assert retry.should_retry(400, 1, 3) is False
    assert retry.should_retry(404, 1, 3) is False


def test_server_error_is_retried() -> None:
    assert retry.should_retry(500, 1, 3) is True


def test_attempts_are_bounded() -> None:
    assert retry.should_retry(500, 3, 3) is False
