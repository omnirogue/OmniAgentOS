"""
API response classification surface.
"""

from __future__ import annotations

from errors import NotFound


def to_response(exc: Exception) -> dict[str, object]:
    """Converts an exception to a standard API response structure."""
    if isinstance(exc, NotFound):
        return {
            "code": "E_NOT_FOUND",
            "status": 404,
            "retryable": False,
        }

    # Defaults to E_INTERNAL for any other exception
    return {
        "code": "E_INTERNAL",
        "status": 500,
        "retryable": True,
    }
