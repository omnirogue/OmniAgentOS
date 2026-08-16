"""Verify command."""

from __future__ import annotations

META = {
    "name": "verify",
    "summary": "Run the acceptance checks.",
    "danger": False,
    "aliases": ("v", "check"),
}


def run(args: list[str]) -> int:
    """Run the verify command."""
    return 11 if not args else len(args) + 11
