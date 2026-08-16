"""
Worker routing and retry surface.
"""

from __future__ import annotations

import errors


def should_retry(exc: Exception) -> bool:
    """Decides if the job failing with the given exception should be retried."""
    return _get_spec(exc).retryable


def dead_letter_reason(exc: Exception) -> str:
    """Returns the code and message of the exception for dead-letter queuing."""
    spec = _get_spec(exc)
    return f"{spec.code}: {exc}"


def _get_spec(exc: Exception) -> errors.ErrorSpec:
    """Helper to retrieve the ErrorSpec for an exception."""
    code = "E_INTERNAL"
    if isinstance(exc, errors.AppError):
        code = getattr(exc, "code", "E_INTERNAL")
    return errors.spec_for(code)
