"""Retry policy. See RETRY.md for the contract this must satisfy."""

from __future__ import annotations


def should_retry(status: int, attempt: int, max_attempts: int) -> bool:
    """Whether another attempt should be made after seeing ``status``."""
    if attempt >= max_attempts:
        return False
    return status >= 400


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Exponential backoff for the given 1-indexed attempt."""
    if attempt < 1:
        raise ValueError("attempt is 1-indexed")
    return min(base * 2 ** (attempt - 1), cap)
