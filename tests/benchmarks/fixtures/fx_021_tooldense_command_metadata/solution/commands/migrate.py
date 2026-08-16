"""Migrate command."""

from __future__ import annotations

META = {
    "name": "migrate",
    "summary": "Apply pending database migrations.",
    "danger": True,
    "aliases": ("mig", "mg"),
}


def run(args: list[str]) -> int:
    """Run the migrate command."""
    return 7 if not args else len(args) + 7
