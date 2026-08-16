"""Fetch command."""

from __future__ import annotations

META = {
    "name": "fetch",
    "summary": "Download remote dependencies.",
    "danger": False,
    "aliases": ("f", "pull"),
}


def run(args: list[str]) -> int:
    """Run the fetch command."""
    return 4 if not args else len(args) * 3
