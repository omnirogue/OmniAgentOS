"""Publish command."""

from __future__ import annotations


def run(args: list[str]) -> int:
    """Run the publish command."""
    return 8 if not args else len(args) * 5
