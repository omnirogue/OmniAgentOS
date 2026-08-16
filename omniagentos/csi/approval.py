"""Human approval cards — CSI propose → improvements row (no auto-apply)."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from omniagentos.csi.models import SynthesisResult
from omniagentos.db.migrate import migrate

LOG = logging.getLogger(__name__)


def create_approval_card(
    *,
    db_path: str,
    run_id: str,
    routine_id: str,
    synthesis: SynthesisResult,
    conflict: dict[str, Any] | None = None,
) -> str | None:
    """Insert an improvements row for human review. Returns improvement id.

    Uses origin ``agent_request`` and kind ``skill`` (Self-Learning) — values
    allowed by migration 042 CHECK constraints. Never auto-approves.
    """
    if synthesis.verdict != "propose" or not synthesis.accepted_proposals:
        return None

    proposals = [p.model_dump() for p in synthesis.accepted_proposals]
    titles = [str(p.get("change") or "")[:80] for p in proposals]
    title = f"CSI {routine_id}: {titles[0]}" if titles else f"CSI {routine_id} proposal"
    summary = (
        f"Continuous self-improvement proposal from run {run_id}. "
        "Human must Approve before any worktree implement. "
        "Nothing merges without a separate human merge."
    )
    proposal_json = {
        "csi_run_id": run_id,
        "routine_id": routine_id,
        "proposals": proposals,
        "conflict": conflict or {},
        "requires_human_merge": True,
        "auto_apply": False,
    }

    try:
        migrate(db_path)
        from omniagentos.reliability.store import SqliteReliabilityStore

        store = SqliteReliabilityStore(db_path)
        imp_id = store.create_improvement(
            origin="agent_request",
            kind="skill",
            title=title[:200],
            summary=summary,
            root_cause=synthesis.reason or "csi_synthesis",
            proposal_json=proposal_json,
            created_by="csi",
        )
        return str(imp_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("create_improvement failed: %s", exc, exc_info=True)
        return None


def improvement_is_approved(db_path: str, improvement_id: str) -> bool:
    """True when improvements.status is approved (human click)."""
    try:
        migrate(db_path)
        from omniagentos.reliability.store import SqliteReliabilityStore

        store = SqliteReliabilityStore(db_path)
        imp = store.get_improvement(improvement_id)
        if imp is None:
            return False
        status = str(getattr(imp, "status", "") or "").lower()
        # status may be enum
        if hasattr(imp.status, "value"):
            status = str(imp.status.value).lower()
        return status in {"approved", "applying"}
    except Exception:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM improvements WHERE id=?", (improvement_id,)
            ).fetchone()
            conn.close()
            if not row:
                return False
            return str(row["status"]).lower() in {"approved", "applying"}
        except Exception:
            return False


def mark_improvement_approved(db_path: str, improvement_id: str) -> bool:
    """Test/operator helper: set status=approved. Not called by engine."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE improvements SET status='approved', updated_at=datetime('now') WHERE id=?",
            (improvement_id,),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error:
        return False


__all__ = [
    "create_approval_card",
    "improvement_is_approved",
    "mark_improvement_approved",
]
