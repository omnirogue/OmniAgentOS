"""Status command."""

from __future__ import annotations

META = {
    "name": "status",
    "summary": "Show the current project state.",
    "danger": False,
    "aliases": ("st", "info"),
}


def run(args: list[str]) -> int:
    """Run the status command."""
    return 10 if not args else len(args) * 6
