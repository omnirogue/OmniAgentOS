"""Status command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the status command."""
    return 10 if not args else len(args) * 6
