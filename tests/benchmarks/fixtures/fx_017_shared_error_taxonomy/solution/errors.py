"""
Shared error taxonomy registry and base exception types.
This file holds the system-wide ErrorSpec definitions and domain exception classes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    http_status: int
    retryable: bool
    exit_code: int


# Central registry for all supported error specifications
REGISTRY: dict[str, ErrorSpec] = {
    "E_NOT_FOUND": ErrorSpec("E_NOT_FOUND", 404, False, 4),
    "E_CONFLICT": ErrorSpec("E_CONFLICT", 409, False, 5),
    "E_RATE_LIMITED": ErrorSpec("E_RATE_LIMITED", 429, True, 6),
    "E_TIMEOUT": ErrorSpec("E_TIMEOUT", 504, True, 7),
    "E_INTERNAL": ErrorSpec("E_INTERNAL", 500, True, 1),
}


def spec_for(code: str) -> ErrorSpec:
    """Returns the ErrorSpec matching the given code, raising KeyError if not found."""
    if code not in REGISTRY:
        raise KeyError(code)
    return REGISTRY[code]


def all_codes() -> tuple[str, ...]:
    """Returns a sorted tuple of all code strings currently in the registry."""
    return tuple(sorted(REGISTRY.keys()))


class AppError(Exception):
    """Base application exception class."""

    code: str = "E_INTERNAL"


class NotFound(AppError):
    """Exception raised when a requested resource is not found."""

    code: str = "E_NOT_FOUND"


class Conflict(AppError):
    """Exception raised when a conflict occurs during an operation."""

    code: str = "E_CONFLICT"


class RateLimited(AppError):
    """Exception raised when request threshold limits are exceeded."""

    code: str = "E_RATE_LIMITED"


class Timeout(AppError):
    """Exception raised when an operation times out."""

    code: str = "E_TIMEOUT"


class Internal(AppError):
    """Exception raised when an internal unexpected error occurs."""

    code: str = "E_INTERNAL"
