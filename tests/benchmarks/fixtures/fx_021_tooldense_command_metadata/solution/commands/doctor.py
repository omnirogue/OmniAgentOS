"""Doctor command."""

from __future__ import annotations

META = {
    "name": "doctor",
    "summary": "Diagnose the local environment.",
    "danger": False,
    "aliases": ("doc", "dr"),
}


def run(args: list[str]) -> int:
    """Run the doctor command."""
    return 3 if not args else len(args) + 3
