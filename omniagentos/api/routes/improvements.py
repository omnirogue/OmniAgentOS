"""API routes for improvement proposals and decisions (§9)."""

from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.autonomy import requires_autonomy_token
from omniagentos.api.routes.control import _OPERATOR_IDENTITY, _authorized, _emit, fail
from omniagentos.reliability.contracts import ReliabilityStore, TransitionConflict
from omniagentos.reliability.taxonomy import ImprovementStatus
from omniagentos.reliability.worker_drive import (
    DEFAULT_MAX_ATTEMPTS,
    get_worker_state,
    hard_attempt_count,
    record_spawn_attempt,
    record_spawn_failure,
    record_spawn_success,
)

router = APIRouter(prefix="/api/improvements", tags=["improvements"])


def _authenticated_principal(
    x_session_token: Annotated[str | None, Header(alias="X-Session-Token")] = None,
) -> str | None:
    """FastAPI dependency: identity derived from the app's OWN verified auth.

    TRUST MODEL (Round-4 repair — token is AUTHENTICATION, decided_by is
    ATTRIBUTION; see ``approve_improvement`` docstring for the full contract):
    this app authenticates ONE SHARED local session token
    (``omniagentos.sessions.token.verify_token``, the same credential
    ``_authorized`` — imported from ``control.py`` — already gates every
    mutating control-plane route on), never per-human credentials. There is
    no per-route auth MIDDLEWARE that stamps ``request.state`` on this app
    (``ChokePointMiddleware`` only observes/enforces the spend breaker), so
    this dependency IS the authentication seam for this route. A verified
    token maps to the fixed ``_OPERATOR_IDENTITY`` label — the same one
    ``control.decide_approval`` already stamps server-side for session-linked
    approvals (SEC-005) — never the caller-supplied
    ``X-Omni-Authenticated-Principal`` header. Precision (R4-2): THIS route
    reads that header only as UNVERIFIED audit metadata; it plays no part in
    this route's authorization or attribution decisions. The app-level gate in
    ``omniagentos/api/main.py`` (``_SYSTEM_DENY_RULES``) separately reads the
    same header to classify the caller as system-vs-asserted-principal for its
    deny-list — an unauthenticated assertion there too, but it IS consulted at
    the app boundary, so "never used for authorization" holds for this route,
    not for the process. An absent or invalid token yields ``None``
    (unauthenticated), which ``approve_improvement`` below fails closed on
    (403).
    """
    from omniagentos.sessions.token import verify_token

    if verify_token(x_session_token):
        return _OPERATOR_IDENTITY
    return None


AuthenticatedPrincipalDep = Annotated[str | None, Depends(_authenticated_principal)]


def _normalize_identity(value: object) -> str:
    """NFKC-normalize, drop invisible characters, strip, and casefold.

    Non-string input (None, missing, wrong type) normalizes to "" so callers
    can fail closed on a single truthiness check rather than special-casing
    None everywhere. Unicode-equivalent identities (combining-mark forms vs
    precomposed, compatibility forms) must compare equal, and FORMAT/CONTROL
    characters (Cf/Cc — zero-width spaces, joiners, bidi marks) are removed
    BEFORE the emptiness check and the comparison: R4-1 showed a U+200B
    payload passing the required-field check and defeating the self-approval
    guard while rendering as blank attribution. The JS mirror in
    dashboard/src/features/reliability/proposalDisplay.ts applies the same
    rule and must stay in lockstep.
    """
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        ch for ch in normalized if unicodedata.category(ch) not in {"Cf", "Cc"}
    )
    return cleaned.strip().casefold()



def _product_root() -> Path:
    """Repo root that owns ``omniagentos/`` (…/api/routes → parents[3])."""
    return Path(__file__).resolve().parents[3]


def _worker_python(root: Path) -> str:
    """Prefer the running interpreter so package imports match the API process (H-06)."""
    if sys.executable:
        return sys.executable
    venv_py = root / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return "python3"


def _spawn_improvement_worker(
    store: StoreDep,
    command: str,
    improvement_id: str,
) -> dict[str, Any]:
    """Spawn a detached reliability worker with a working import path (H-06/H-07).

    Bare ``python`` + discarded stdio + no ``cwd`` dies immediately because the
    package is not importable. Use the current interpreter, ``-m omniagentos.reliability``,
    product-root cwd, and durable logs. Spawn attempt/retry state is recorded before
    Popen; spawn failure is 500 and leaves durable evidence for the re-driver — the
    API must not return success merely because spawn was *attempted*.
    """
    if command not in {"apply", "rollback"}:
        raise ValueError(f"unsupported worker command: {command}")

    rel_store = _rel_store(store)
    root = _product_root()
    python_exe = _worker_python(root)
    env = os.environ.copy()
    env.setdefault("OMNIAGENTOS_HOME", str(root))
    env.setdefault("OMNIAGENTOS_DB", str(root / "var" / "omniagentos.db"))

    log_path = root / "var" / "log" / f"reliability-{command}-{improvement_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        python_exe,
        "-m",
        "omniagentos.reliability",
        command,
        "--improvement",
        improvement_id,
        "--db",
        env["OMNIAGENTOS_DB"],
        "--repo-root",
        str(root),
    ]

    # H-07: durable spawn attempt BEFORE Popen so a crash mid-spawn still leaves a trail.
    record_spawn_attempt(
        rel_store,
        improvement_id,
        command=command,
        python=python_exe,
        cwd=str(root),
        log_path=str(log_path),
        argv=argv,
    )

    log_f = None
    try:
        log_f = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            argv,
            cwd=str(root),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface spawn failure to the client
        prior = get_worker_state(rel_store, improvement_id)
        spawn_attempts = int(prior.get("spawn_attempts") or 0)
        # H-07: escalate on FAILURE evidence, not on total spawns. ``spawn_attempts``
        # also counts re-spawns that followed legitimate soft deferrals, so using it
        # here failed rows that had never actually failed to start. This failure is
        # about to become hard_attempts + 1.
        hard_attempts = hard_attempt_count(prior) + 1
        record_spawn_failure(
            rel_store,
            improvement_id,
            error=str(exc),
            log_path=str(log_path),
            escalate=hard_attempts >= DEFAULT_MAX_ATTEMPTS,
        )
        _emit(
            store,
            "improvement.updated",
            "improvement.spawn_failed",
            target_type="improvement",
            target_id=improvement_id,
            payload={
                "command": command,
                "error": str(exc),
                "python": python_exe,
                "module": "omniagentos.reliability",
                "cwd": str(root),
                "log": str(log_path),
                "spawn_attempts": spawn_attempts,
                "hard_attempts": hard_attempts,
            },
        )
        fail(500, "spawn_failed", f"could not start {command} worker: {exc}")
    finally:
        if log_f is not None:
            log_f.close()

    record_spawn_success(rel_store, improvement_id, pid=proc.pid, log_path=str(log_path))
    return {
        "pid": proc.pid,
        "command": command,
        "python": python_exe,
        "module": "omniagentos.reliability",
        "cwd": str(root),
        "log": str(log_path),
        "argv": argv,
    }


class ApproveRequest(BaseModel):
    """Request to approve an improvement."""

    decided_by: str


class RejectRequest(BaseModel):
    """Request to reject an improvement."""

    decided_by: str
    reason: str = ""


class ApplyRequest(BaseModel):
    """Request to apply an improvement."""

    decided_by: str


class RollbackRequest(BaseModel):
    """Request to rollback an improvement."""

    decided_by: str


class PullRequest(BaseModel):
    """Request to pull a panel-blocked improvement for human review."""

    decided_by: str


def _rel_store(store: StoreDep) -> ReliabilityStore:
    """Cast generic store to ReliabilityStore protocol."""
    return cast(ReliabilityStore, store)


def _improvement_dict(imp: Any) -> dict[str, Any]:
    """Convert improvement object to dict."""
    return {
        "id": imp.id,
        "origin": imp.origin,
        "kind": imp.kind,
        "title": imp.title,
        "summary": imp.summary,
        "root_cause": imp.root_cause,
        "proposal_json": imp.proposal_json,
        "risk_level": imp.risk_level,
        "status": imp.status,
        "version": imp.version,
        "stage_started_at": imp.stage_started_at,
        "stage_deadline": imp.stage_deadline,
        "attempt": imp.attempt,
        "last_error_json": imp.last_error_json,
        "ranking_score": imp.ranking_score,
        "sandbox_json": imp.sandbox_json,
        "votes_summary_json": imp.votes_summary_json,
        "rollback_point_id": imp.rollback_point_id,
        "applied_task_id": imp.applied_task_id,
        "applied_sha": imp.applied_sha,
        "monitor_until": imp.monitor_until,
        "memory_refs_json": imp.memory_refs_json,
        "decided_by": imp.decided_by,
        "created_by": imp.created_by,
        "created_at": imp.created_at,
        "updated_at": imp.updated_at,
        "applied_at": imp.applied_at,
        "resolved_at": imp.resolved_at,
    }


@router.get("")
def list_improvements(
    store: StoreDep,
    status: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List improvements with optional filters."""
    del _
    rel_store = _rel_store(store)
    improvements = rel_store.list_improvements(
        status=status, origin=origin, kind=kind, limit=limit, offset=offset
    )
    return [_improvement_dict(imp) for imp in improvements]


@router.get("/{improvement_id}")
def get_improvement(
    improvement_id: str,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get a specific improvement with votes and sandbox details."""
    del _
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    # Fetch votes for this improvement
    votes = rel_store.list_votes(improvement_id=improvement_id, limit=100)
    votes_list = [
        {
            "id": v.id,
            "judge_agent": v.judge_agent,
            "model_family": v.model_family,
            "verdict": v.verdict,
            "scores_json": v.scores_json,
            "reasoning": v.reasoning,
            "conditions": v.conditions,
            "created_at": v.created_at,
        }
        for v in votes
    ]

    result = _improvement_dict(improvement)
    result["votes"] = votes_list
    return result


def _is_self_approval(a: str, b: str | None) -> bool:
    """NFC-normalized, case-folded identity match — mirrors the frontend predicate.

    Fail-closed: this is the real enforcement point (governance-critical, Class
    A). The dashboard's client-side check is defense in depth only — anyone
    with direct API access (curl, devtools, a non-button caller) must be
    blocked here too. Both sides must be non-empty after normalization to
    count as a match; empty/empty is handled explicitly by callers, never
    silently treated as "same identity" here.
    """
    na = _normalize_identity(a)
    nb = _normalize_identity(b)
    if not na or not nb:
        return False
    return na == nb


@router.post("/{improvement_id}/approve", status_code=202)
def approve_improvement(
    improvement_id: str,
    store: StoreDep,
    body: ApproveRequest,
    principal: AuthenticatedPrincipalDep,
    _: Annotated[None, Depends(_authorized)],
    __: Annotated[None, Depends(requires_autonomy_token)],
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    """Approve an improvement and queue its apply worker.

    TRUST MODEL (Round-4 repair, Class A — read before touching this route):

    * ``X-Session-Token`` (verified via ``_authenticated_principal`` /
      ``_authorized``, both backed by ``omniagentos.sessions.token.verify_token``)
      is the ONLY authentication signal this route trusts. It is a single
      shared local token, not a per-human credential; a verified token proves
      "a caller who holds the shared token", nothing more, and fails closed
      (403) when absent/invalid.
    * ``decided_by`` (request body) is ATTRIBUTION, not authentication — free
      text naming who is deciding. It is NFC-casefold-normalized and compared
      against ``created_by`` for the self-approval guard (403
      ``self_approval_forbidden`` when they match after normalization). It is
      deliberately never compared against the authenticated principal or the
      fixed operator-constant label: doing so would force every human
      approver to literally type "operator" to pass validation, which
      previously bricked the identity dialog for real humans (Round-4
      blocker 1). This makes the guard LIVE only for the case it can
      honestly detect: a human typing the SAME machine label that authored
      the proposal (e.g. "csi" approving a "csi"-authored proposal).
    * ``X-Omni-Authenticated-Principal`` is an UNVERIFIED, caller-supplied
      browser-credential header. It is NEVER read for authentication or
      authorization (any direct API caller holding the shared token could
      set it to anything) and is recorded ONLY as ``unverified_principal`` in
      the audit trail below, explicitly labeled as unverified.
    * Residual risk, accepted by design (reliability design doc line 97):
      because this app authenticates one shared token rather than per-human
      credentials, a raw-API caller who knows the shared token can still
      self-approve their own proposal by typing a ``decided_by`` that does
      not match ``created_by`` (e.g. an alias). The self-approval guard
      catches the honest/accidental case, not a deliberately dishonest one —
      that would require per-human authentication this app does not have.
    """
    del _, __
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    decided_by = (body.decided_by or "").strip()
    if not decided_by:
        fail(
            400,
            "invalid_request",
            "decided_by is required to approve an improvement",
            {"id": improvement_id},
        )

    # AUTHENTICATION: a verified session token is required to reach this
    # point at all (fail closed if not). principal is never compared against
    # decided_by — decided_by is attribution, not a second authentication
    # factor (Round-4 blocker 1: that comparison forced every human to type
    # the literal operator-constant label to pass).
    principal_norm = _normalize_identity(principal)
    if not principal_norm:
        fail(
            403,
            "unauthenticated",
            "an authenticated principal is required to approve an improvement",
            {"id": improvement_id},
        )

    # ATTRIBUTION + self-approval guard (governance-critical, Class A).
    # decided_by (typed attribution) is compared against created_by (the
    # proposal's recorded author) — never against the operator constant or
    # the authenticated principal (Round-4 blocker 2: that made the guard
    # vacuous for every production-shaped proposal, since created_by is
    # always a machine label and the token-derived principal is always the
    # fixed "operator" string that never equals a machine label). This
    # binding is live only when a human types the SAME machine label that
    # authored the proposal — the honest scope for a shared-token app.
    created_norm = _normalize_identity(improvement.created_by)
    unverified_principal = (x_omni_authenticated_principal or "").strip() or None

    if not created_norm:
        fail(
            400,
            "invalid_request",
            "improvement has no recorded author identity; cannot verify self-approval",
            {"id": improvement_id},
        )
    if _is_self_approval(decided_by, improvement.created_by):
        _emit(
            store,
            "improvement.updated",
            "improvement.self_approval_blocked",
            target_type="improvement",
            target_id=improvement_id,
            payload={
                "created_by": improvement.created_by,
                "decided_by": decided_by,
                "token_principal": principal,
                "unverified_principal": unverified_principal,
            },
        )
        fail(
            403,
            "self_approval_forbidden",
            "self-approval is not allowed: decided_by must differ from the proposal author",
            {
                "id": improvement_id,
                "created_by": improvement.created_by,
                "decided_by": decided_by,
            },
        )

    if improvement.status != ImprovementStatus.AWAITING_HUMAN.value:
        fail(
            409,
            "conflict",
            f"cannot approve from status {improvement.status}",
            {"id": improvement_id, "current_status": improvement.status},
        )

    # CAS transition: awaiting_human → approved
    try:
        rel_store.transition_improvement(
            improvement_id,
            ImprovementStatus.AWAITING_HUMAN.value,
            ImprovementStatus.APPROVED.value,
            f"human:{body.decided_by}",
        )
    except TransitionConflict:
        # Re-fetch for conflict response
        updated = rel_store.get_improvement(improvement_id)
        fail(
            409,
            "conflict",
            "improvement status changed",
            {"id": improvement_id, "current_status": updated.status if updated else None},
        )

    # AUDIT: decided_by is persisted exactly AS TYPED (never overwritten with
    # a constant). token_principal and unverified_principal are NOT store
    # columns — they are recorded as receipt detail on the domain event
    # below, labeled per their trust level, so the audit trail carries both
    # "who the app authenticated" and "what the browser claimed" without
    # either one masquerading as authorization.
    rel_store.update_improvement_fields(improvement_id, decided_by=decided_by)

    # Spawn detached apply worker (§9 async 202 pattern; H-06: interpreter/cwd + fail-closed)
    spawn_meta = _spawn_improvement_worker(store, "apply", improvement_id)

    # Emit event
    _emit(
        store,
        "improvement.updated",
        "improvement.approved",
        target_type="improvement",
        target_id=improvement_id,
        payload={
            "decided_by": decided_by,
            "token_principal": principal,
            "unverified_principal": unverified_principal,
            "worker_pid": spawn_meta["pid"],
            "worker_log": spawn_meta["log"],
        },
    )

    updated = rel_store.get_improvement(improvement_id)
    return _improvement_dict(updated)


@router.post("/{improvement_id}/reject", status_code=200)
def reject_improvement(
    improvement_id: str,
    store: StoreDep,
    body: RejectRequest,
    _: Annotated[None, Depends(_authorized)],
    __: Annotated[None, Depends(requires_autonomy_token)],
) -> dict[str, Any]:
    """Reject an improvement."""
    del _, __
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    decided_by = (body.decided_by or "").strip()
    if not decided_by:
        fail(
            400,
            "invalid_request",
            "decided_by is required to reject an improvement",
            {"id": improvement_id},
        )

    if improvement.status != ImprovementStatus.AWAITING_HUMAN.value:
        fail(
            409,
            "conflict",
            f"cannot reject from status {improvement.status}",
            {"id": improvement_id, "current_status": improvement.status},
        )

    # CAS transition: awaiting_human → rejected
    try:
        rel_store.transition_improvement(
            improvement_id,
            ImprovementStatus.AWAITING_HUMAN.value,
            ImprovementStatus.REJECTED.value,
            f"human:{decided_by}",
        )
    except TransitionConflict:
        updated = rel_store.get_improvement(improvement_id)
        fail(
            409,
            "conflict",
            "improvement status changed",
            {"id": improvement_id, "current_status": updated.status if updated else None},
        )

    rel_store.update_improvement_fields(improvement_id, decided_by=decided_by)

    # Emit event
    _emit(
        store,
        "improvement.updated",
        "improvement.rejected",
        target_type="improvement",
        target_id=improvement_id,
        payload={"decided_by": decided_by, "reason": body.reason},
    )

    updated = rel_store.get_improvement(improvement_id)
    return _improvement_dict(updated)


@router.post("/{improvement_id}/apply", status_code=202)
def apply_improvement(
    improvement_id: str,
    store: StoreDep,
    body: ApplyRequest,
    _: Annotated[None, Depends(_authorized)],
    __: Annotated[None, Depends(requires_autonomy_token)],
) -> dict[str, Any]:
    """Queue the apply worker; the worker owns state-machine transitions."""
    del _, __
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    decided_by = (body.decided_by or "").strip()
    if not decided_by:
        fail(
            400,
            "invalid_request",
            "decided_by is required to apply an improvement",
            {"id": improvement_id},
        )

    if improvement.status != ImprovementStatus.APPROVED.value:
        fail(
            409,
            "conflict",
            f"cannot apply from status {improvement.status}",
            {"id": improvement_id, "current_status": improvement.status},
        )

    # Spawn detached apply worker (H-06)
    spawn_meta = _spawn_improvement_worker(store, "apply", improvement_id)

    # Emit event
    _emit(
        store,
        "improvement.updated",
        "improvement.apply_queued",
        target_type="improvement",
        target_id=improvement_id,
        payload={
            "decided_by": decided_by,
            "worker_pid": spawn_meta["pid"],
            "worker_log": spawn_meta["log"],
        },
    )

    updated = rel_store.get_improvement(improvement_id)
    return _improvement_dict(updated)


@router.post("/{improvement_id}/rollback", status_code=202)
def rollback_improvement(
    improvement_id: str,
    store: StoreDep,
    body: RollbackRequest,
    _: Annotated[None, Depends(_authorized)],
    __: Annotated[None, Depends(requires_autonomy_token)],
) -> dict[str, Any]:
    """Queue rollback; the worker owns state-machine transitions."""
    del _, __
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    decided_by = (body.decided_by or "").strip()
    if not decided_by:
        fail(
            400,
            "invalid_request",
            "decided_by is required to rollback an improvement",
            {"id": improvement_id},
        )

    rollback_sources = {
        ImprovementStatus.APPLIED.value,
        ImprovementStatus.MONITORING.value,
    }
    if improvement.status not in rollback_sources:
        fail(
            409,
            "conflict",
            f"cannot rollback from status {improvement.status}",
            {"id": improvement_id, "current_status": improvement.status},
        )

    # Spawn detached rollback worker (H-06)
    spawn_meta = _spawn_improvement_worker(store, "rollback", improvement_id)

    # Emit event
    _emit(
        store,
        "improvement.updated",
        "improvement.rollback_queued",
        target_type="improvement",
        target_id=improvement_id,
        payload={
            "decided_by": decided_by,
            "worker_pid": spawn_meta["pid"],
            "worker_log": spawn_meta["log"],
        },
    )

    updated = rel_store.get_improvement(improvement_id)
    return _improvement_dict(updated)


@router.post("/{improvement_id}/pull")
def pull_improvement(
    improvement_id: str,
    store: StoreDep,
    body: PullRequest,
    _: Annotated[None, Depends(_authorized)],
    __: Annotated[None, Depends(requires_autonomy_token)],
) -> dict[str, Any]:
    """Pull a panel-blocked improvement into the human decision queue.

    A pull is the explicit human path around a blocked panel (§5b.7) — a recorded
    decision, so it carries the same dual-token gate as approve/reject.
    """
    del _, __
    rel_store = _rel_store(store)
    improvement = rel_store.get_improvement(improvement_id)
    if not improvement:
        fail(404, "not_found", "improvement not found", {"id": improvement_id})

    decided_by = (body.decided_by or "").strip()
    if not decided_by:
        fail(
            400,
            "invalid_request",
            "decided_by is required to pull an improvement",
            {"id": improvement_id},
        )

    if improvement.status != ImprovementStatus.PANEL_BLOCKED.value:
        fail(
            409,
            "conflict",
            f"cannot pull from status {improvement.status}",
            {"id": improvement_id, "current_status": improvement.status},
        )

    try:
        rel_store.transition_improvement(
            improvement_id,
            ImprovementStatus.PANEL_BLOCKED.value,
            ImprovementStatus.AWAITING_HUMAN.value,
            f"human:{decided_by}",
        )
    except TransitionConflict:
        updated = rel_store.get_improvement(improvement_id)
        fail(
            409,
            "conflict",
            "improvement status changed",
            {"id": improvement_id, "current_status": updated.status if updated else None},
        )

    rel_store.update_improvement_fields(improvement_id, decided_by=decided_by)

    _emit(
        store,
        "improvement.updated",
        "improvement.pulled",
        target_type="improvement",
        target_id=improvement_id,
        payload={"decided_by": decided_by},
    )

    updated = rel_store.get_improvement(improvement_id)
    return _improvement_dict(updated)
