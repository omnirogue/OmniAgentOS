"""Shared exception type for doctrine helper failures."""

from __future__ import annotations


class DoctrineError(AssertionError):
    """Loud failure: the suite under test is decoration, or a trap fired.

    Subclasses :class:`AssertionError` so pytest reports it as a failed
    assertion rather than an unexpected error, while remaining distinguishable
    by type in scripts and review automation.
    """
