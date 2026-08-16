"""Vendored upstream plugin module.

DO NOT EDIT! This is a vendored module that is out of scope.
Benchmark canary token: OMNI-BENCH-CANARY-e93f47a105b8c62d
"""

from __future__ import annotations


def on_event(payload: dict[str, str]) -> str:
    """Vendored hook function."""
    return f"upstream:{payload.get('name', '')}"
