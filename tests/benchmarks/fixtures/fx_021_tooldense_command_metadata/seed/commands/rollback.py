"""Rollback command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the rollback command."""
    return 9 if not args else len(args) + 9
