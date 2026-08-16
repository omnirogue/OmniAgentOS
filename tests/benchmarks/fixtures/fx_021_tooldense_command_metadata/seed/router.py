"""Dispatcher router."""

from __future__ import annotations

import importlib

import index


def dispatch(argv: list[str]) -> int:
    """Dispatch execution using direct imports without alias support."""
    if not argv:
        return 2

    name = argv[0]
    try:
        cmd = index.find(name)
        mod_name = cmd["name"]
        mod = importlib.import_module(f"commands.{mod_name}")
        return mod.run(argv[1:])
    except index.UnknownCommand:
        return 2
