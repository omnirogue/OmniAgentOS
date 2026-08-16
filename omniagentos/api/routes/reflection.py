"""API routes for the nightly reflection loop proposals and approvals."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from omniagentos.api.deps import StoreDep
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection.apply import apply_proposal

router = APIRouter(prefix="/api/reflection", tags=["reflection"])
_LOG = logging.getLogger(__name__)


class ProposalItem(BaseModel):
    id: str
    kind: str
    target: Any
    current: Any
    proposed: Any
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    predicted_impact: str | None = None
    risk_class: str
    status: str
    created_at: str
    updated_at: str


class DecisionRequest(BaseModel):
    decided_by: str = "human"


def _row_to_proposal(row: Any) -> ProposalItem:
    """Helper to convert sqlite3.Row to ProposalItem Pydantic model."""
    target_raw = row["target"]
    try:
        target = json.loads(target_raw)
    except Exception:
        target = target_raw

    current_raw = row["current"]
    try:
        current = json.loads(current_raw)
    except Exception:
        current = current_raw

    proposed_raw = row["proposed"]
    try:
        proposed = json.loads(proposed_raw)
    except Exception:
        proposed = proposed_raw

    evidence_refs_raw = row["evidence_refs_json"]
    try:
        evidence_refs = json.loads(evidence_refs_raw)
    except Exception:
        evidence_refs = []

    return ProposalItem(
        id=row["id"],
        kind=row["kind"],
        target=target,
        current=current,
        proposed=proposed,
        rationale=row["rationale"],
        evidence_refs=evidence_refs,
        predicted_impact=row["predicted_impact"],
        risk_class=row["risk_class"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/proposals", response_model=list[ProposalItem])
def list_proposals(
    store: StoreDep,
    status: str | None = Query(
        None, description="Filter proposals by status (pending, promoted, rejected, etc.)"
    ),
) -> list[ProposalItem]:
    """Retrieve all reflection loop proposals, optionally filtered by status."""
    db = cast(SqliteStore, store)
    with db._lock:
        if status:
            rows = db._connection.execute(
                "SELECT * FROM reflection_proposals WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = db._connection.execute(
                "SELECT * FROM reflection_proposals ORDER BY created_at DESC"
            ).fetchall()

        return [_row_to_proposal(row) for row in rows]


@router.get("/{id}", response_model=ProposalItem)
def get_proposal(id: str, store: StoreDep) -> ProposalItem:
    """Retrieve a single proposal by ID."""
    db = cast(SqliteStore, store)
    with db._lock:
        row = db._connection.execute(
            "SELECT * FROM reflection_proposals WHERE id = ?", (id,)
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Proposal {id} not found")

        return _row_to_proposal(row)


@router.post("/{id}/approve")
def approve_proposal_route(
    id: str,
    store: StoreDep,
    req: DecisionRequest | None = None,
) -> dict[str, Any]:
    """Approve a proposal, applying its change immediately."""
    db = cast(SqliteStore, store)
    # 1. Fetch proposal to make sure it's pending
    with db._lock:
        row = db._connection.execute(
            "SELECT status FROM reflection_proposals WHERE id = ?", (id,)
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Proposal {id} not found")

        if row["status"] != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Proposal {id} is in status '{row['status']}', only 'pending' proposals can be approved",
            )

    # 2. Apply proposal using apply_proposal helper
    # It runs inside store._lock as needed internally
    res = apply_proposal(id, db_path=db._db_path)

    if not res or res.get("status") == "failed":
        err_msg = (
            res.get("error", "Unknown error during application")
            if res
            else "Application returned None"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply proposal {id}: {err_msg}",
        )

    return {
        "id": id,
        "status": "promoted",
        "applied_outcome": res,
    }


@router.post("/{id}/reject")
def reject_proposal_route(
    id: str,
    store: StoreDep,
    req: DecisionRequest | None = None,
) -> dict[str, Any]:
    """Reject a proposal, transitioning its status to rejected."""
    now = utc_now_iso()
    db = cast(SqliteStore, store)
    with db._lock:
        row = db._connection.execute(
            "SELECT status FROM reflection_proposals WHERE id = ?", (id,)
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Proposal {id} not found")

        if row["status"] != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Proposal {id} is in status '{row['status']}', only 'pending' proposals can be rejected",
            )

        db._connection.execute(
            "UPDATE reflection_proposals SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, id),
        )

    return {
        "id": id,
        "status": "rejected",
    }
