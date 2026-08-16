"""Build command."""

from __future__ import annotations

META = {
    "name": "build",
    "summary": "Compile the project artifacts.",
    "danger": False,
    "aliases": ("b", "make"),
}


def run(args: list[str]) -> int:
    """Run the build command."""
    return 0 if not args else len(args)
