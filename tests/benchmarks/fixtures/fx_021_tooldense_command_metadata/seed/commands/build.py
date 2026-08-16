"""Build command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the build command."""
    return 0 if not args else len(args)
