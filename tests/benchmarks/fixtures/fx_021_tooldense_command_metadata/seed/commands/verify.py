"""Verify command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the verify command."""
    return 11 if not args else len(args) + 11
