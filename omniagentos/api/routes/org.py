"""API routes for organization structure and agent management (§9)."""

from __future__ import annotations

import subprocess
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import _authorized, _emit, fail
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.taxonomy import AgentRequestStatus

router = APIRouter(prefix="/api/org", tags=["organization"])


class AgentToggleRequest(BaseModel):
    """Request to toggle agent enabled status."""

    pass


class AgentRequestSubmit(BaseModel):
    """Request to create a new agent request."""

    description: str


class AgentRequestApprove(BaseModel):
    """Request to approve an agent request."""

    decided_by: str


class AgentRequestReject(BaseModel):
    """Request to reject an agent request."""

    decided_by: str
    reason: str = ""


def _rel_store(store: StoreDep) -> ReliabilityStore:
    """Cast generic store to ReliabilityStore protocol."""
    return cast(ReliabilityStore, store)  # Already implements ReliabilityStore protocol


def _value(obj: Any, field: str) -> Any:
    return getattr(obj, field) if hasattr(obj, field) else obj.get(field)


def _org_unit_dict(unit: Any) -> dict[str, Any]:
    """Convert org_unit object to dict."""
    return {
        field: _value(unit, field)
        for field in (
            "id",
            "name",
            "kind",
            "parent_id",
            "charter",
            "status",
            "created_at",
            "updated_at",
        )
    }


def _agent_dict(agent: Any) -> dict[str, Any]:
    """Convert agent object to dict."""
    return {
        field: _value(agent, field)
        for field in (
            "id",
            "name",
            "model",
            "org_unit_id",
            "org_role",
            "title",
            "charter",
            "schedule_json",
            "harness",
            "enabled",
            "vault_note_path",
            "created_at",
            "updated_at",
        )
    }


@router.get("/tree")
def get_org_tree(
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get organization hierarchy."""
    del _
    rel_store = _rel_store(store)

    # Fetch all org units
    all_units = rel_store.list_org_units(status="active")

    # Build tree: company at root, departments as children
    company = next((u for u in all_units if _value(u, "kind") == "company"), None)
    departments = [u for u in all_units if _value(u, "kind") == "department"]
    teams = [u for u in all_units if _value(u, "kind") == "team"]

    # Build nested structure
    tree = {
        "company": _org_unit_dict(company) if company else None,
        "departments": [
            {
                **_org_unit_dict(dept),
                "teams": [
                    _org_unit_dict(team)
                    for team in teams
                    if _value(team, "parent_id") == _value(dept, "id")
                ],
            }
            for dept in departments
        ],
    }

    return tree


@router.get("/agents")
def list_agents(
    store: StoreDep,
    org_unit_id: str | None = Query(default=None),
    org_role: str | None = Query(default=None),
    enabled: int | None = Query(default=None),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List agents with optional filters."""
    del _
    rel_store = _rel_store(store)
    agents = rel_store.list_agents(org_unit_id=org_unit_id, org_role=org_role, enabled=enabled)
    return [_agent_dict(agent) for agent in agents]


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get a specific agent."""
    del _
    rel_store = _rel_store(store)
    agent = rel_store.get_agent(agent_id)
    if not agent:
        fail(404, "not_found", "agent not found", {"id": agent_id})

    result = _agent_dict(agent)

    # Add scorecard sparkline (last 7 days, stub)
    scorecards = rel_store.list_scorecards(
        subject_type="agent", subject_id=agent_id, window="day", limit=7
    )
    result["scorecard_sparkline"] = [
        {"period_start": sc.period_start, "metrics": sc.metrics_json} for sc in scorecards
    ]

    return result


@router.get("/agents/{agent_id}/activity")
def get_agent_activity(
    agent_id: str,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get agent activity: recent runs, events, votes, log entries."""
    del _
    rel_store = _rel_store(store)
    agent = rel_store.get_agent(agent_id)
    if not agent:
        fail(404, "not_found", "agent not found", {"id": agent_id})

    # Stub: return empty activity structure
    # In full implementation, would query runs, events, improvement_votes, reliability_log
    return {
        "agent_id": agent_id,
        "runs": [],
        "events": [],
        "votes": [],
        "log_entries": [],
    }


@router.post("/agents/{agent_id}/toggle")
def toggle_agent(
    agent_id: str,
    store: StoreDep,
    body: AgentToggleRequest | None = None,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Toggle agent enabled status."""
    del _
    rel_store = _rel_store(store)
    agent = rel_store.get_agent(agent_id)
    if not agent:
        fail(404, "not_found", "agent not found", {"id": agent_id})

    new_enabled = 1 - _value(agent, "enabled")  # Toggle
    rel_store.update_agent(agent_id, enabled=new_enabled)

    # Emit event
    _emit(
        store,
        "org.updated",
        "agent.toggled",
        target_type="agent",
        target_id=agent_id,
        payload={"enabled": new_enabled},
    )

    updated = rel_store.get_agent(agent_id)
    return _agent_dict(updated)


@router.post("/agent-requests")
def create_agent_request(
    store: StoreDep,
    body: AgentRequestSubmit,
    x_agent_id: Annotated[str | None, Header()] = None,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Create a new agent request (U-E7).

    Attribution comes from the ``X-Agent-ID`` header, defaulting to ``system``
    for internal events. Canonical spellings: lane:*, loop:*, job:*, human:*,
    system. NOT agent:* (rejected as non-canonical at the store's write path).

    KNOWN LIMITATION — X-Agent-ID is an UNVERIFIED CLAIM, not a proven identity.
    ``_authorized`` gates this route on ``X-Session-Token``, which is one shared
    machine-local secret (``var/secrets/sessions-token``): it proves the caller
    is on this box's control plane and carries no per-principal identity at all.
    There is therefore nothing on the authenticated channel to validate the
    header against, so any caller holding the token can attribute a request to
    any canonical spelling — including ``human:owner``.

    Narrowing this to a derived principal needs a channel that actually carries
    one (per-session tokens minted per lane, or a signed lane assertion stamped
    into the session environment by the supervisor). That is a design change to
    the session credential, not a repair of this route, and building the wrong
    one here would be worse than the honest gap: it is deliberately NOT faked by
    validating the claim against itself. Until it lands, treat ``from_agent_id``
    on API-created rows as caller-asserted provenance, and note that store-level
    callers (the CLI, the scheduler, the loops) are truthful because they name
    themselves in-process.
    """
    del _
    rel_store = _rel_store(store)

    # Get from_agent_id from header, default to 'system' for internal events.
    from_agent_id = x_agent_id or "system"

    try:
        req_id = rel_store.create_agent_request(
            description=body.description,
            from_agent_id=from_agent_id,
        )
    except ValueError as e:
        # Canonical spelling validation failed
        fail(400, "invalid_identity", str(e), {"from_agent_id": from_agent_id})

    # Emit event
    _emit(
        store,
        "org.updated",
        "agent_request.created",
        target_type="agent_request",
        target_id=req_id,
        payload={"description": body.description},
    )

    return {"id": req_id, "status": "pending"}


@router.get("/agent-requests")
def list_agent_requests(
    store: StoreDep,
    status: str | None = Query(default=None),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List agent requests."""
    del _
    rel_store = _rel_store(store)
    requests = rel_store.list_agent_requests(status=status, limit=100)
    return [
        {
            field: _value(r, field)
            for field in (
                "id",
                "description",
                "requested_by",
                "status",
                "design_json",
                "improvement_id",
                "agent_id",
                "created_at",
                "updated_at",
            )
        }
        for r in requests
    ]


@router.post("/agent-requests/{request_id}/approve", status_code=202)
def approve_agent_request(
    request_id: str,
    store: StoreDep,
    body: AgentRequestApprove,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Approve an agent request (spawn design worker, return 202)."""
    del _
    rel_store = _rel_store(store)
    request = rel_store.get_agent_request(request_id)
    if not request:
        fail(404, "not_found", "agent request not found", {"id": request_id})

    request_status = _value(request, "status")
    if request_status != AgentRequestStatus.PENDING.value:
        fail(
            409,
            "conflict",
            f"cannot approve from status {request_status}",
            {"id": request_id, "current_status": request_status},
        )

    # The detached design step consumes this existing request row.
    rel_store.update_agent_request_status(
        request_id,
        status=AgentRequestStatus.DESIGNING.value,
    )

    # Spawn detached design worker. Spawn failure must fail closed (H-42):
    # never emit agent_request.approved and mark the request failed.
    try:
        subprocess.Popen(
            [
                "python",
                "-m",
                "omniagentos.orgdims.company_requests",
                "design",
                "--request",
                request_id,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        error_message = f"design agent spawn failed: {type(exc).__name__}: {exc}"
        rel_store.update_agent_request_status(
            request_id,
            status=AgentRequestStatus.FAILED.value,
            design_json={"error": error_message, "phase": "spawn"},
        )
        _emit(
            store,
            "org.updated",
            "agent_request.spawn_failed",
            target_type="agent_request",
            target_id=request_id,
            payload={
                "decided_by": body.decided_by,
                "error": error_message,
                "status": AgentRequestStatus.FAILED.value,
            },
        )
        fail(
            503,
            "spawn_failed",
            "design agent subprocess could not be started",
            {
                "id": request_id,
                "status": AgentRequestStatus.FAILED.value,
                "error": error_message,
            },
        )

    # Emit successful approval only after the process handle is created.
    _emit(
        store,
        "org.updated",
        "agent_request.approved",
        target_type="agent_request",
        target_id=request_id,
        payload={"decided_by": body.decided_by},
    )

    updated = rel_store.get_agent_request(request_id)
    return {
        "id": _value(updated, "id"),
        "description": _value(updated, "description"),
        "status": _value(updated, "status"),
        "requested_by": _value(updated, "requested_by"),
    }


@router.post("/agent-requests/{request_id}/reject")
def reject_agent_request(
    request_id: str,
    store: StoreDep,
    body: AgentRequestReject,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Reject an agent request."""
    del _
    rel_store = _rel_store(store)
    request = rel_store.get_agent_request(request_id)
    if not request:
        fail(404, "not_found", "agent request not found", {"id": request_id})

    request_status = _value(request, "status")
    if request_status != AgentRequestStatus.PENDING.value:
        fail(
            409,
            "conflict",
            f"cannot reject from status {request_status}",
            {"id": request_id, "current_status": request_status},
        )

    # Transition to rejected
    rel_store.update_agent_request_status(request_id, status="rejected")

    # Emit event
    _emit(
        store,
        "org.updated",
        "agent_request.rejected",
        target_type="agent_request",
        target_id=request_id,
        payload={"decided_by": body.decided_by, "reason": body.reason},
    )

    updated = rel_store.get_agent_request(request_id)
    return {
        "id": _value(updated, "id"),
        "description": _value(updated, "description"),
        "status": _value(updated, "status"),
        "requested_by": _value(updated, "requested_by"),
    }
