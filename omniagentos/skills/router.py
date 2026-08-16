"""FastAPI router for the frozen S-A skills-library routes."""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, Query

from omniagentos.skills import (
    decide_proposal,
    get_skill,
    list_proposals,
    list_skills,
    list_tree,
    mark_preferred,
    new_version,
    propose_update,
    release_quarantine,
    restore_version,
    search,
    upsert_skill,
)
from omniagentos.skills.models import (
    PreferredMethod,
    ProposalDecision,
    QuarantineRelease,
    SkillCreate,
    SkillEdit,
    UpdateProposalCreate,
)

router = APIRouter()


def _not_found_or_bad_request(exc: KeyError | ValueError) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/skills/tree")
def skills_tree() -> list[dict]:
    return list_tree()


@router.get("/skills")
def skills_list(
    include_archived: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[dict]:
    """Flat skill registry (spawn-time selection and dashboard tables)."""
    return list_skills(include_archived=include_archived, limit=limit)


# Literal routes must precede /skills/{skill_id} so FastAPI never interprets
# "search" as an identifier.
@router.get("/skills/search")
def search_skills(q: str = Query(min_length=1)) -> list[dict]:
    return search(q, limit=20)


@router.post("/skills")
def create_skill(body: SkillCreate) -> str:
    try:
        return upsert_skill(body.model_dump())
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.get("/updates")
def updates(
    state: Literal["pending", "approved", "rejected"] | None = Query(default=None),
) -> list[dict[str, Any]]:
    return list_proposals(state=state)


@router.post("/updates/{id}/decide")
def decide_update(id: str, body: ProposalDecision) -> dict:
    try:
        return decide_proposal(
            id,
            approve=body.approve,
            note=body.note,
            decided_by=body.decided_by,
        )
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.put("/skills/{id}")
def edit_skill(id: str, body: SkillEdit) -> int:
    try:
        return new_version(
            id,
            content=body.content,
            preferred_method=body.preferred_method,
            fallback_method=body.fallback_method,
            change_reason=body.change_reason,
            author="api",
        )
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.post("/skills/{id}/propose")
def propose_skill_update(id: str, body: UpdateProposalCreate) -> str:
    try:
        return propose_update(
            id,
            proposed_content=body.proposed_content,
            proposed_diff=body.proposed_diff,
            evidence_files=body.evidence_files,
            linked_execution=body.linked_execution,
            risk=body.risk,
            created_by=body.created_by,
        )
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.post("/skills/{id}/restore/{version}")
def restore_skill_version(id: str, version: int) -> dict:
    try:
        return restore_version(id, version)
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.post("/skills/{id}/preferred")
def prefer_skill_method(id: str, body: PreferredMethod) -> dict:
    try:
        return mark_preferred(id, body.method)
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.post("/skills/{id}/release-quarantine")
def release_skill_quarantine(id: str, body: QuarantineRelease) -> dict:
    """Lift a machine quarantine so the skill is selectable again (U-16).

    This is the operator-facing reversal of the auto-capture quarantine: the
    rows were never deleted, so disagreeing with the machine costs one call.
    404 for an unknown skill, 400 when the skill was not quarantined.
    """
    try:
        return release_quarantine(id, released_by=body.released_by, note=body.note)
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


@router.get("/skills/{id}")
def read_skill(id: str) -> dict:
    try:
        return get_skill(id)
    except (KeyError, ValueError) as exc:
        _not_found_or_bad_request(exc)


__all__ = ["router"]
