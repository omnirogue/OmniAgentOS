"""Deploy command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the deploy command."""
    return 2 if not args else len(args) * 2
