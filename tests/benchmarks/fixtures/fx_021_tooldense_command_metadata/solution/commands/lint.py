"""Lint command."""

from __future__ import annotations

META = {
    "name": "lint",
    "summary": "Check the source for style problems.",
    "danger": False,
    "aliases": ("l",),
}


def run(args: list[str]) -> int:
    """Run the lint command."""
    return 6 if not args else len(args) * 4
