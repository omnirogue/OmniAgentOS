"""Continuous Self-Improvement (CSI) — HANDOFF/self-improvement foundation.

Off by default (``configs/self_improvement.yaml`` ``enabled: false``).
Routines may propose changes but **nothing merges without a human click**.
Frozen surfaces are unwritable by routine proposals (enforced, not prompted).
"""

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


_warn_frozen_engine("csi")

from omniagentos.csi.config import (  # noqa: E402 -- after the frozen-engine warning, deliberately
    CsiConfig,
    load_csi_config,
)
from omniagentos.csi.engine import CsiEngine, run_routine, run_self_learning  # noqa: E402
from omniagentos.csi.frozen import (  # noqa: E402
    assert_writable,
    is_frozen_path,
    reject_frozen_paths,
)
from omniagentos.csi.implement import (  # noqa: E402
    cleanup_implementation,
    implement_approved,
    recover_implementation,
)

__all__ = [
    "CsiConfig",
    "CsiEngine",
    "assert_writable",
    "cleanup_implementation",
    "implement_approved",
    "is_frozen_path",
    "load_csi_config",
    "reject_frozen_paths",
    "recover_implementation",
    "run_routine",
    "run_self_learning",
]
