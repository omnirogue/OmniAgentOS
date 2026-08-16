"""OmniAgentOS V2: Self-improving reliability system.

Detects failures, proposes improvements, judges them, and applies approved changes
with rollback on regression. Append-only audit log enforces immutability of the system
that governs itself. See docs/architecture/V2-DESIGN.md for the full spec.
"""

from __future__ import annotations

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


_warn_frozen_engine("reliability")

__all__ = [
    "taxonomy",
    "contracts",
    "store",
]
