"""
CLI exit status and formatting surface.
"""

from __future__ import annotations

from errors import NotFound


def exit_code_for(exc: Exception) -> int:
    """Calculates the exit status code for the process based on the exception."""
    if isinstance(exc, NotFound):
        return 4
    return 1  # Default exit status for general / internal errors


def render(exc: Exception) -> str:
    """Formats the exception message for console logging."""
    if isinstance(exc, NotFound):
        return f"error [E_NOT_FOUND]: {exc}"
    return f"error [E_INTERNAL]: {exc}"
