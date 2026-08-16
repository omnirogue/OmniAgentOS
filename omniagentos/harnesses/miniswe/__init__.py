"""mini-swe-agent harness (B1 baseline ladder arm)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter

__all__ = ["MiniSweAdapter"]


def __getattr__(name: str) -> object:
    if name == "MiniSweAdapter":
        from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter as _MiniSweAdapter

        return _MiniSweAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
