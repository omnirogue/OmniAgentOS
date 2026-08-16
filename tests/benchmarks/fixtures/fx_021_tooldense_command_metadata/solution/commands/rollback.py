"""Rollback command."""

from __future__ import annotations

META = {
    "name": "rollback",
    "summary": "Revert the last deployment.",
    "danger": True,
    "aliases": ("rb", "undo"),
}


def run(args: list[str]) -> int:
    """Run the rollback command."""
    return 9 if not args else len(args) + 9
