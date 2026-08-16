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


# Seed registry currently only covers NotFound and Internal
REGISTRY: dict[str, ErrorSpec] = {
    "E_NOT_FOUND": ErrorSpec("E_NOT_FOUND", 404, False, 4),
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

    pass


class NotFound(AppError):
    """Exception raised when a requested resource is not found."""

    pass


class Internal(AppError):
    """Exception raised when an internal unexpected error occurs."""

    pass
