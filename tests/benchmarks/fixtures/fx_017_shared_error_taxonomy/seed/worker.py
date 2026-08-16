"""
Worker routing and retry surface.
"""

from __future__ import annotations

from errors import NotFound


def should_retry(exc: Exception) -> bool:
    """Decides if the job failing with the given exception should be retried."""
    if isinstance(exc, NotFound):
        return False
    return True  # Internal and general errors are retryable


def dead_letter_reason(exc: Exception) -> str:
    """Returns the code and message of the exception for dead-letter queuing."""
    if isinstance(exc, NotFound):
        return f"E_NOT_FOUND: {exc}"
    return f"E_INTERNAL: {exc}"
