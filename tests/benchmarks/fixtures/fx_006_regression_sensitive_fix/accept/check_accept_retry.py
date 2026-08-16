"""FROZEN acceptance check for fx_006_regression_sensitive_fix.

Enforces the whole RETRY.md contract, including the two transient 4xx codes the
naive fix drops.
"""

from __future__ import annotations

import pytest
import retry


@pytest.mark.parametrize("status", [200, 201, 204, 301, 304, 399])
def test_never_retries_success_or_redirect(status: int) -> None:
    assert retry.should_retry(status, 1, 5) is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 410, 418, 422, 451])
def test_never_retries_permanent_client_errors(status: int) -> None:
    assert retry.should_retry(status, 1, 5) is False


@pytest.mark.parametrize("status", [408, 429])
def test_always_retries_transient_client_errors(status: int) -> None:
    assert retry.should_retry(status, 1, 5) is True


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_always_retries_server_errors(status: int) -> None:
    assert retry.should_retry(status, 1, 5) is True


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_attempt_budget_wins_over_retryability(status: int) -> None:
    assert retry.should_retry(status, 3, 3) is False
    assert retry.should_retry(status, 4, 3) is False
    assert retry.should_retry(status, 2, 3) is True


def test_backoff_delay_untouched() -> None:
    assert retry.backoff_delay(1, 0.5, 30.0) == 0.5
    assert retry.backoff_delay(2, 0.5, 30.0) == 1.0
    assert retry.backoff_delay(3, 0.5, 30.0) == 2.0
    assert retry.backoff_delay(10, 0.5, 30.0) == 30.0
    with pytest.raises(ValueError):
        retry.backoff_delay(0)
