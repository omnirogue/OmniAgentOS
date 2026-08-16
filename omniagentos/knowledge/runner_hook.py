"""Merge-order-safe runner reflection hook."""

from __future__ import annotations

from typing import Any


def safe_ingest_run_reflection(run_row: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    """Invoke the optional reflection package lazily and never fail finalization."""
    try:
        from omniagentos.knowledge import ingest  # type: ignore[attr-defined]
    except Exception:
        # ImportError (merge-order gap) OR any module-load-time error in ingest/its
        # transitive imports must degrade to a no-op — this hook must NEVER fail run
        # finalization (a raise here can mark a COMPLETED run FAILED). Council finding.
        return

    try:
        from omniagentos.knowledge.recall import _get_store

        ingest.ingest_run_reflection(_get_store(), run_row, steps)
    except Exception:
        return
