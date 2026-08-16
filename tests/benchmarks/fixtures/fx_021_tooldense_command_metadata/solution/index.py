"""Command index."""

from __future__ import annotations

import importlib

# Explicit module-name list
_MODULE_NAMES = (
    "build",
    "clean",
    "deploy",
    "doctor",
    "fetch",
    "init",
    "lint",
    "migrate",
    "publish",
    "rollback",
    "status",
    "verify",
)


class UnknownCommand(KeyError):
    """Raised when a command is unknown."""


def _load_commands() -> tuple[dict[str, object], ...]:
    collected = []
    for name in _MODULE_NAMES:
        mod = importlib.import_module(f"commands.{name}")
        meta = mod.META
        collected.append(meta)
    return tuple(sorted(collected, key=lambda x: x["name"]))


COMMANDS: tuple[dict[str, object], ...] = _load_commands()


def find(token: str) -> dict[str, object]:
    """Find a command by its name or any of its aliases."""
    for cmd in COMMANDS:
        if cmd["name"] == token:
            return cmd
        aliases = cmd.get("aliases", ())
        if token in aliases:
            return cmd
    raise UnknownCommand(token)


def help_text() -> str:
    """Return formatted help text for all commands."""
    max_len = max(len(cmd["name"]) for cmd in COMMANDS)
    lines = []
    for cmd in COMMANDS:
        name = cmd["name"]
        summary = cmd["summary"]
        danger_suffix = " [danger]" if cmd["danger"] else ""
        padded_name = name.rjust(max_len)
        lines.append(f"{padded_name}  {summary}{danger_suffix}")
    return "\n".join(lines)
