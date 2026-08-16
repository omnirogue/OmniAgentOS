"""OmniAgentOS loop runtime — LangGraph 1.2.10 bridged to the existing control plane.

Phase 1 of ``devtasks/langgraph-comparison/AUTONOMOUS_SYSTEM_FAST_TRACK_PLAN.md``,
shaped by ``MIGRATION_ARCHITECTURE.md`` (Option G thin hybrid). The division of
labour is deliberate and narrow:

* LangGraph owns ONLY loop-body durability: checkpoints, resume, retries,
  interrupts. One checkpoint file per loop family under ``var/loops/``.
* The control plane stays authoritative for everything that matters: policy
  (``omniagentos.policy.evaluate_action``), approvals (the ``approvals`` table +
  SSE + the existing dashboard), observability (the ``events`` ledger), spend
  (the ``llm/`` short-call client's $10/day cap) and exactly-once external
  effects (the ``idempotency`` table).

Nothing here is importable from the production venv and nothing here is imported
by production code: the only production seam is
``omniagentos/scheduler/loop_jobs.py``, which launches this package as a
subprocess in its own venv.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Repository root of the checkout this package lives in
#: (``<repo>/loops/omniagentos_loops/__init__.py`` -> ``<repo>``).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

__version__ = "0.1.0"


def bootstrap_repo_path() -> Path:
    """Make ``omniagentos`` importable from THIS checkout, and return the root.

    The loops venv deliberately does not install the ``omniagentos`` package
    (see ``loops/requirements.txt``): a second installed copy could silently
    diverge from the code the API and runner actually execute, which is exactly
    how a "policy is enforced" claim becomes false. Resolving the checkout root
    from ``__file__`` binds the bridge to the tree it was launched from.
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


bootstrap_repo_path()

__all__ = ["REPO_ROOT", "__version__", "bootstrap_repo_path"]
