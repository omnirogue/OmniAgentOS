"""HTTP API for Cognitive Budget Manager — LIVE progressive escalation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from omniagentos.api.services import fail
from omniagentos.cbm.service import RUNGS, CognitiveBudgetService
from omniagentos.contracts import default_db_path

router = APIRouter(prefix="/api/cbm", tags=["cbm"])

_SERVICE: CognitiveBudgetService | None = None


def get_cbm_service() -> CognitiveBudgetService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CognitiveBudgetService(database=default_db_path())
    return _SERVICE


class AllocateBody(BaseModel):
    task_id: str | None = None
    stage: str = "execution"
    required_quality: float = Field(default=0.9, ge=0.0, le=1.0)
    risk_class: str = "reversible_internal"
    novelty: str = "low"
    difficulty: str = "medium"
    fingerprint: dict[str, Any] | None = None
    force_rung: int | None = Field(default=None, ge=0, le=6)


class EscalateBody(BaseModel):
    trigger_code: str
    evidence: list[str] = Field(default_factory=list)
    next_gate: str | None = None


class ContractBody(BaseModel):
    reason: str = "gate_passed"


class CloseBody(BaseModel):
    first_pass_accepted: bool
    wall_seconds: float | None = None
    repair_count: int = 0
    verifier_score: float | None = None
    production_outcome: str = "healthy"


@router.get("/health")
def health() -> dict[str, Any]:
    return get_cbm_service().health()


@router.get("/rungs")
def list_rungs() -> dict[str, Any]:
    return {"rungs": RUNGS, "live": True, "objective": "quality+ETAR"}


@router.post("/allocate")
def allocate(body: AllocateBody) -> dict[str, Any]:
    alloc = get_cbm_service().allocate(
        task_id=body.task_id,
        stage=body.stage,
        required_quality=body.required_quality,
        risk_class=body.risk_class,
        novelty=body.novelty,
        difficulty=body.difficulty,
        fingerprint=body.fingerprint,
        force_rung=body.force_rung,
    )
    return {"ok": True, "allocation": alloc}


@router.get("/allocations/{allocation_id}")
def get_allocation(allocation_id: str) -> dict[str, Any]:
    row = get_cbm_service().get_allocation(allocation_id)
    if row is None:
        fail(404, "not_found", f"allocation {allocation_id}")
    return {"allocation": row}


@router.post("/allocations/{allocation_id}/escalate")
def escalate(allocation_id: str, body: EscalateBody) -> dict[str, Any]:
    try:
        result = get_cbm_service().escalate(
            allocation_id,
            trigger_code=body.trigger_code,
            evidence=body.evidence,
            next_gate=body.next_gate,
        )
    except KeyError:
        fail(404, "not_found", f"allocation {allocation_id}")
    except ValueError as exc:
        fail(400, "escalate_rejected", str(exc))
    return {"ok": True, **result}


@router.post("/allocations/{allocation_id}/contract")
def contract(allocation_id: str, body: ContractBody) -> dict[str, Any]:
    try:
        result = get_cbm_service().contract(allocation_id, reason=body.reason)
    except KeyError:
        fail(404, "not_found", f"allocation {allocation_id}")
    return {"ok": True, **result}


@router.post("/allocations/{allocation_id}/close")
def close_allocation(allocation_id: str, body: CloseBody) -> dict[str, Any]:
    try:
        result = get_cbm_service().close_allocation(
            allocation_id,
            first_pass_accepted=body.first_pass_accepted,
            wall_seconds=body.wall_seconds,
            repair_count=body.repair_count,
            verifier_score=body.verifier_score,
            production_outcome=body.production_outcome,
        )
    except KeyError:
        fail(404, "not_found", f"allocation {allocation_id}")
    return {"ok": True, **result}


@router.get("/allocations/{allocation_id}/escalations")
def list_escalations(allocation_id: str) -> dict[str, Any]:
    return {"escalations": get_cbm_service().list_escalations(allocation_id)}


@router.get("/leaderboard")
def leaderboard() -> dict[str, Any]:
    return {"leaderboard": get_cbm_service().leaderboard()}
