"""Clean command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the clean command."""
    return 1 if not args else len(args) + 1
