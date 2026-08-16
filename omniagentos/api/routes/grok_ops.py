"""OmniAgentOS operator surfaces — interactions, grants, allocate, gates, revise.

Separate product only. Registered under /api/grok/*.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.db.store import SqliteStore

router = APIRouter(prefix="/api/grok", tags=["grok"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- interactions (TN.10) ----------------------------------------------------


class CreateInteractionRequest(_Body):
    work_ref_type: str
    work_ref_id: str
    direction: str = "user_to_agent"
    kind: str = "nudge"
    body: str
    session_id: str | None = None
    blocking_policy: str = "none"
    author: str | None = None
    parent_id: str | None = None
    expires_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/interactions")
def create_interaction(body: CreateInteractionRequest, store: StoreDep) -> dict[str, Any]:
    from omniagentos.interactions.store import InteractionsStore

    try:
        return InteractionsStore(cast(SqliteStore, store)).create(
            work_ref_type=body.work_ref_type,
            work_ref_id=body.work_ref_id,
            direction=body.direction,
            kind=body.kind,
            body=body.body,
            session_id=body.session_id,
            blocking_policy=body.blocking_policy,
            author=body.author,
            parent_id=body.parent_id,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except ValueError as exc:
        fail(400, "invalid", str(exc))


@router.get("/interactions")
def list_interactions(
    store: StoreDep,
    work_ref_type: str | None = None,
    work_ref_id: str | None = None,
    session_id: str | None = None,
    blocking_only: bool = False,
) -> list[dict[str, Any]]:
    from omniagentos.interactions.store import InteractionsStore

    return InteractionsStore(cast(SqliteStore, store)).list_pending(
        work_ref_type=work_ref_type,
        work_ref_id=work_ref_id,
        session_id=session_id,
        blocking_only=blocking_only,
    )


@router.post("/interactions/{interaction_id}/deliver")
def deliver_interaction(interaction_id: str, store: StoreDep) -> dict[str, Any]:
    from omniagentos.interactions.store import InteractionsStore

    row = InteractionsStore(cast(SqliteStore, store)).mark_delivered(interaction_id)
    if row is None:
        fail(404, "not_found", "interaction not found", {"id": interaction_id})
    return row


class AnswerInteractionRequest(_Body):
    body: str
    author: str | None = None


@router.post("/interactions/{interaction_id}/answer")
def answer_interaction(
    interaction_id: str, body: AnswerInteractionRequest, store: StoreDep
) -> dict[str, Any]:
    """Thread an answer under a question/nudge (does not overwrite the original).

    App-level session-token auth (``require_session_token``) gates this mutation.
    """
    from omniagentos.interactions.consumer import InteractionConsumer
    from omniagentos.interactions.store import InteractionsStore

    consumer = InteractionConsumer(InteractionsStore(cast(SqliteStore, store)))
    try:
        result = consumer.answer(interaction_id, body.body, author=body.author)
    except ValueError as exc:
        fail(400, "invalid", str(exc))
    if result is None:
        fail(404, "not_found", "interaction not found", {"id": interaction_id})
    return result


class ConsumeInteractionsRequest(_Body):
    work_ref_type: str
    work_ref_id: str


@router.post("/interactions/consume")
def consume_interactions(body: ConsumeInteractionsRequest, store: StoreDep) -> dict[str, Any]:
    """Expire overdue rows for the work ref, then deliver remaining pending."""
    from omniagentos.interactions.consumer import InteractionConsumer
    from omniagentos.interactions.store import InteractionsStore

    consumer = InteractionConsumer(InteractionsStore(cast(SqliteStore, store)))
    delivered = consumer.consume_pending(body.work_ref_type, body.work_ref_id)
    return {
        "work_ref_type": body.work_ref_type,
        "work_ref_id": body.work_ref_id,
        "delivered": delivered,
        "count": len(delivered),
    }


@router.post("/interactions/expire")
def expire_interactions(store: StoreDep) -> dict[str, Any]:
    """Expire active and delivered-unanswered interactions past ``expires_at``.

    Plain-data shape matches the L10 handoff
    (:func:`omniagentos.interactions.consumer.expire_due_interactions`).
    """
    from omniagentos.interactions.consumer import expire_due_interactions

    return expire_due_interactions(cast(SqliteStore, store))


# --- Decision Center (M-15) — truthful not-implemented contract ---------------


@router.get("/decision-center")
@router.get("/recommended-next-action")
def decision_center_capability() -> dict[str, Any]:
    """Canonical Decision Center surface: the implemented capability contract.

    The Executive Decision Center (``omniagentos/edc``) is the producer. Returns
    the implemented contract pointing at the live owner-scoped ``/api/decisions``
    surfaces (P0–P4). The former 501 not-implemented placeholder was flipped once
    EDC shipped an end-to-end product.
    """
    from omniagentos.routing.decision_center import decision_center_contract

    return {"contract": decision_center_contract()}


# --- campaign grants (TN.7) --------------------------------------------------


class CreateGrantRequest(_Body):
    capability: str
    label: str = ""
    target_set: list[str] = Field(default_factory=list)
    project_id: str | None = None
    # Required bounds — no silent unbounded grants (HANDOFF Task 3).
    approval_id: str
    max_actions: int
    max_spend_usd: float
    expires_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/grants")
def create_grant(body: CreateGrantRequest, store: StoreDep) -> dict[str, Any]:
    from omniagentos.grants import GrantsStore

    try:
        return GrantsStore(cast(SqliteStore, store)).create_grant(
            body.capability,
            label=body.label,
            target_set=body.target_set,
            project_id=body.project_id,
            approval_id=body.approval_id,
            max_actions=body.max_actions,
            max_spend_usd=body.max_spend_usd,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except ValueError as exc:
        fail(400, "invalid_grant", str(exc))


@router.get("/grants")
def list_grants(
    store: StoreDep,
    capability: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    from omniagentos.grants import GrantsStore

    return GrantsStore(cast(SqliteStore, store)).list_active_grants(
        capability=capability, project_id=project_id
    )


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, store: StoreDep, reason: str = "operator_revoke") -> dict[str, Any]:
    from omniagentos.grants import GrantsStore

    gs = GrantsStore(cast(SqliteStore, store))
    row = gs.revoke_grant(grant_id, reason=reason)
    if row is None:
        fail(404, "not_found", "grant not found or already revoked", {"id": grant_id})
    return row


# --- allocation / gates / revise (pure compute) ------------------------------


class SimulateRequest(_Body):
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    capacity: dict[str, Any] = Field(default_factory=dict)


@router.post("/allocate/simulate")
def simulate_allocate(body: SimulateRequest) -> dict[str, Any]:
    from omniagentos.allocation import audit_decomposition, simulate_fanout

    audit = audit_decomposition(body.tasks)
    sim = simulate_fanout(body.tasks, body.capacity or None)
    return {"audit": audit.to_dict(), "simulation": sim.to_dict()}


class GateEvalRequest(_Body):
    gate: str  # g0|g2|g3|g5|g6|g8
    evidence: dict[str, Any] = Field(default_factory=dict)


@router.post("/gates/eval")
def eval_gate(body: GateEvalRequest) -> dict[str, Any]:
    from omniagentos.gates import GateService

    svc = GateService()
    name = body.gate.lower().replace("-", "")
    method = {
        "g0": svc.g0_intake,
        "g0intake": svc.g0_intake,
        "g2": svc.g2_dispatch,
        "g2dispatch": svc.g2_dispatch,
        "g3": svc.g3_tool,
        "g3tool": svc.g3_tool,
        "g5": svc.g5_local_verify,
        "g5localverify": svc.g5_local_verify,
        "g6": svc.g6_independent_review,
        "g6independentreview": svc.g6_independent_review,
        "g8": svc.g8_release,
        "g8release": svc.g8_release,
    }.get(name)
    if method is None:
        fail(400, "invalid_gate", f"unknown gate {body.gate!r}")
    evidence = dict(body.evidence or {})
    # G8 live grant lookup: inject GrantsStore when grant_id present and caller
    # did not supply grant_store. Must pass db_path (SqliteStore requires it).
    if name in {"g8", "g8release"} and evidence.get("grant_id") and "grant_store" not in evidence:
        try:
            from omniagentos.contracts import default_db_path
            from omniagentos.db.store import SqliteStore
            from omniagentos.grants import GrantsStore

            evidence["grant_store"] = GrantsStore(SqliteStore(default_db_path()))
        except Exception as exc:  # noqa: BLE001
            # Leave store absent; g8_release fails closed for consequential.
            evidence["grant_inject_error"] = f"{type(exc).__name__}:{exc}"
    decision = method(evidence)
    # grant_store is not JSON-serializable; strip before response.
    out_evidence = {k: v for k, v in (decision.evidence or {}).items() if k != "grant_store"}
    return {
        "gate_id": decision.gate_id.value,
        "decision": decision.decision,
        "evidence": out_evidence,
        "next_state": decision.next_state,
        "policy_version": decision.policy_version,
    }


class ReviseRequest(_Body):
    prior_manifest: dict[str, Any]
    feedback: str
    requested_write_set: list[str] | None = None


@router.post("/revise")
def create_revision(body: ReviseRequest) -> dict[str, Any]:
    from omniagentos.workmodes.revise import RevisionError, create_revision_spec

    try:
        spec = create_revision_spec(
            body.prior_manifest,
            body.feedback,
            requested_write_set=body.requested_write_set,
        )
    except RevisionError as exc:
        fail(400, "invalid_revision", str(exc))
    return spec.to_dict()


class ContractHashRequest(_Body):
    contract: dict[str, Any]


@router.post("/contracts/hash")
def hash_contract(body: ContractHashRequest) -> dict[str, Any]:
    from omniagentos.taskcontract import TaskContract, TaskContractError

    try:
        tc = TaskContract.from_dict(body.contract)
    except (TaskContractError, ValueError, KeyError, TypeError) as exc:
        fail(400, "invalid_contract", str(exc))
    return {"contract_hash": tc.contract_hash(), "contract": tc.to_dict()}


@router.get("/health")
def grok_health() -> dict[str, Any]:
    return {
        "product": "OmniAgentOS",
        "separate": True,
        "merge_to_omniagentos": False,
        "surfaces": [
            "interactions",
            "interactions/answer",
            "interactions/consume",
            "interactions/expire",
            "decision-center",
            "recommended-next-action",
            "grants",
            "allocate/simulate",
            "gates/eval",
            "revise",
            "contracts/hash",
        ],
        "decision_center": {
            "implemented": True,
            "status": "implemented",
            "producer": "omniagentos.edc",
        },
    }
