"""Init command."""

from __future__ import annotations

META = {
    "name": "init",
    "summary": "Create a new project skeleton.",
    "danger": False,
    "aliases": ("new",),
}


def run(args: list[str]) -> int:
    """Run the init command."""
    return 5 if not args else len(args) + 5
