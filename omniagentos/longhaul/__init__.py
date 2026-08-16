"""Longhaul: long-horizon agentic coding lane."""

import logging
import warnings

_warned = False


def _warn_frozen_engine(engine_name: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        msg = f"DEPRECATION WARNING: The {engine_name} engine is frozen and deprecated. It will be removed in a future release."
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        logging.getLogger("omniagentos").warning(msg)


_warn_frozen_engine("longhaul")

from .store import (  # noqa: E402 -- after the frozen-engine warning, deliberately
    Category,
    LonghaulStore,
    TaskSession,
)

__all__ = [
    "Category",
    "LonghaulStore",
    "TaskSession",
]
