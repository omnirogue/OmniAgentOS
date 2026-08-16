"""Placeholder Resolver Stub

This module resolves nested placeholder references (${section.key}) within configurations.
"""

from __future__ import annotations


class MissingReference(ValueError):
    """Raised when a referenced section or key is missing."""

    pass


class CircularReference(ValueError):
    """Raised when a circular reference chain is detected."""

    pass


def resolve(config: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Resolve all ${section.key} placeholders in the configuration values.

    Args:
        config: The parsed INI dictionary.

    Returns:
        A new dict with all placeholders fully resolved.

    Raises:
        MissingReference: If a reference targets a missing section or key.
        CircularReference: If a reference loop is detected.
    """
    raise NotImplementedError("resolve is not implemented yet")
