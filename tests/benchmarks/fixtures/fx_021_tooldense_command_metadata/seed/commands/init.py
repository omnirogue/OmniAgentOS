"""Init command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the init command."""
    return 5 if not args else len(args) + 5
