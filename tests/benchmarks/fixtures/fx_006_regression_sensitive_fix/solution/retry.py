"""Reference solution — honors the whole RETRY.md contract."""

from __future__ import annotations

TRANSIENT_CLIENT_ERRORS = frozenset({408, 429})


def should_retry(status: int, attempt: int, max_attempts: int) -> bool:
    """Whether another attempt should be made after seeing ``status``."""
    if attempt >= max_attempts:
        return False
    if status < 400:
        return False
    if status >= 500:
        return True
    return status in TRANSIENT_CLIENT_ERRORS


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Exponential backoff for the given 1-indexed attempt."""
    if attempt < 1:
        raise ValueError("attempt is 1-indexed")
    return min(base * 2 ** (attempt - 1), cap)
