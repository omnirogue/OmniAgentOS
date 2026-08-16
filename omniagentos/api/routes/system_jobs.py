"""Read-only HTTP surface for the system's scheduled jobs.

``GET /api/system-jobs`` answers the question the Loops page could not: "what
runs on a schedule on this system, and is it healthy?" — across launchd agents
(rendered by ``scripts/*/install-*.sh``), the CSI self-improvement pipeline
routines (``configs/self_improvement.yaml``), and the remote cron jobs
documented in ``HANDOFF/LOOPS-VISIBILITY.md``. Managed DB routines keep living
under ``/api/routines``; this endpoint is everything ELSE, read-only by design
(HANDOFF §L1: "Do not add enable/disable/run-now for launchd or remote cron in
the first pass").

The heavy lifting — catalog, plist parsing, launchctl/log enrichment, health
derivation — is :mod:`omniagentos.scheduler.system_jobs`, kept pure and
injectable so it is unit-tested without touching the real machine. This module
is deliberately NOT imported by ``omniagentos.api.main`` here; registration is
a one-line ``include_router`` the integrator owns (see
docs/ui-redesign/LOOPS-REPORT-KIMI.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from omniagentos.scheduler.system_jobs import cached_list_system_jobs

router = APIRouter(prefix="/api/system-jobs", tags=["system-jobs"])


def _repo_root() -> Path:
    """Repo root = grandparent of the api package (the contracts.py anchor idiom,
    re-derived locally per repo convention so imports stay one-directional)."""
    return Path(__file__).resolve().parents[3]


@router.get("")
def get_system_jobs(fresh: int = 0) -> dict[str, Any]:
    """Snapshot of every known/discovered scheduled job with health.

    Best-effort by construction: a non-macOS host, a wedged launchctl, or a
    missing LaunchAgents dir degrades individual fields (loaded/last_run) —
    never the whole response. Cached ~30s (see
    :class:`omniagentos.scheduler.system_jobs.SnapshotCache`) so a page load
    never re-runs the full launchctl/plist scan more than needed; pass
    ``?fresh=1`` to bypass the cache."""
    return cached_list_system_jobs(repo_root=_repo_root(), fresh=bool(fresh))
