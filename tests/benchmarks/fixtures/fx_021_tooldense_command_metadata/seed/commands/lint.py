"""Lint command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the lint command."""
    return 6 if not args else len(args) * 4
