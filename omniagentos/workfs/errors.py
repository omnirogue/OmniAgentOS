"""Errors raised by the workfs convention module."""

from __future__ import annotations


class WorkfsError(Exception):
    """Base class for workfs failures (path policy, containment, I/O)."""

    def __init__(self, code: str, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class WorkfsPathError(WorkfsError):
    """A scope component or resolved path is refused by policy/containment."""
