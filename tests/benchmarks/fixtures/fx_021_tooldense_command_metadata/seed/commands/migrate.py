"""Migrate command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the migrate command."""
    return 7 if not args else len(args) + 7
