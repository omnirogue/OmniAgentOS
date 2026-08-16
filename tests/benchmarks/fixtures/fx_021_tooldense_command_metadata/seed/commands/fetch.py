"""Fetch command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the fetch command."""
    return 4 if not args else len(args) * 3
