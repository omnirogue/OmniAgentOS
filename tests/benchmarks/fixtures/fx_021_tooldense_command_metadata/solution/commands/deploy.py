"""Deploy command."""

from __future__ import annotations

META = {
    "name": "deploy",
    "summary": "Ship the current build to an environment.",
    "danger": True,
    "aliases": ("dep", "ship"),
}


def run(args: list[str]) -> int:
    """Run the deploy command."""
    return 2 if not args else len(args) * 2
