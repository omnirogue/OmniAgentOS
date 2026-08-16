"""
CLI exit status and formatting surface.
"""

from __future__ import annotations

import errors


def exit_code_for(exc: Exception) -> int:
    """Calculates the exit status code for the process based on the exception."""
    return _get_spec(exc).exit_code


def render(exc: Exception) -> str:
    """Formats the exception message for console logging."""
    spec = _get_spec(exc)
    return f"error [{spec.code}]: {exc}"


def _get_spec(exc: Exception) -> errors.ErrorSpec:
    """Helper to retrieve the ErrorSpec for an exception."""
    code = "E_INTERNAL"
    if isinstance(exc, errors.AppError):
        code = getattr(exc, "code", "E_INTERNAL")
    return errors.spec_for(code)
