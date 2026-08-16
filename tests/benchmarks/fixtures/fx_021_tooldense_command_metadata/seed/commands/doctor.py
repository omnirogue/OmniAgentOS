"""Doctor command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the doctor command."""
    return 3 if not args else len(args) + 3
