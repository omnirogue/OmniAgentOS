"""Command index."""

from __future__ import annotations

# Stale hardcoded list
COMMANDS = (
    {
        "name": "build",
        "summary": "Compile the project artifacts.",
        "danger": False,
        "aliases": ("b", "make"),
    },
    {
        "name": "clean",
        "summary": "Remove build output and caches.",
        "danger": True,
        "aliases": ("cl",),
    },
    {
        "name": "deploy",
        "summary": "Ship the current build to an environment.",
        "danger": True,
        "aliases": ("dep", "ship"),
    },
    {
        "name": "doctor",
        "summary": "Diagnose the local environment.",
        "danger": False,
        "aliases": ("doc", "dr"),
    },
    {
        "name": "fetch",
        "summary": "Download remote dependencies.",
        "danger": False,
        "aliases": ("f", "pull"),
    },
)


class UnknownCommand(KeyError):
    """Raised when a command is unknown."""


def find(name: str) -> dict[str, object]:
    """Find a command by exact name."""
    for cmd in COMMANDS:
        if cmd["name"] == name:
            return cmd
    raise UnknownCommand(name)


def help_text() -> str:
    """Return a stale help text."""
    return "build  Compile the project artifacts."
