"""Publish command."""

from __future__ import annotations

META = {
    "name": "publish",
    "summary": "Upload a release to the registry.",
    "danger": True,
    "aliases": ("pub",),
}


def run(args: list[str]) -> int:
    """Run the publish command."""
    return 8 if not args else len(args) * 5
