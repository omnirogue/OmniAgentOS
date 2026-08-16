"""
API response classification surface.
"""

from __future__ import annotations

import errors


def to_response(exc: Exception) -> dict[str, object]:
    """Converts an exception to a standard API response structure dynamically."""
    spec = _get_spec(exc)
    return {
        "code": spec.code,
        "status": spec.http_status,
        "retryable": spec.retryable,
    }


def _get_spec(exc: Exception) -> errors.ErrorSpec:
    """Helper to retrieve the ErrorSpec for an exception."""
    code = "E_INTERNAL"
    if isinstance(exc, errors.AppError):
        code = getattr(exc, "code", "E_INTERNAL")
    return errors.spec_for(code)
