"""Owner-scoped Executive Decision Center reads and P2 decision actions.

Owner-scoped read + a generic decide endpoint for the P1 actions
(``dismiss`` / ``snooze`` / ``note`` / ``edit-reclassify``). Two invariants make
this surface safe to expose:

* **Owner is derived from the authenticated principal, NEVER a request param.**
  ``_owner_for_principal`` resolves the ``X-Omni-Authenticated-Principal`` header
  to a roster employee (collab.py's pattern). The ``system`` machine principal
  (and any unmapped principal) resolves to no owner and is refused 403 — the
  machine identity does not own a private decision queue. A foreign decision id
  reads as absent (404), so an id-probe cannot tell "not yours" from
  "does not exist".
* **No global broadcast (CODEX F08).** A decide returns only the owner's own
  updated decision. There is deliberately no cross-owner refresh/SSE signal here
  that could leak another owner's activity or a stable private id; any future
  refresh signal must be per-owner (principal-scoped).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict

from omniagentos.api.routes.control import ApiError
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.edc.actions import draft_sha256, run_executor, safe_error
from omniagentos.edc.reply import create_reply_draft, edit_reply_draft
from omniagentos.edc.snooze import resolve_snooze, suggested_snoozes
from omniagentos.edc.store import (
    AUTOMATION_RULE_KINDS,
    DecisionConflictError,
    DecisionStore,
    available_actions_for,
    machine_spec,
)

router = APIRouter(prefix="/api/decisions")

_P2_ACTIONS = frozenset({"reply", "approve", "deny", "edit", "snooze", "dismiss", "note"})
# P3 review F2: delegate/defer are advertised by ``available_actions_for`` on an
# open decision and MUST be serviceable here (they were previously refused 400).
# ``execute`` is intentionally absent — no generic execute executor exists yet, so
# ``available_actions_for`` no longer advertises it either (read model never offers
# an unbuilt write path).
_P3_ACTIONS = frozenset({"delegate", "defer"})
_DECIDE_ACTIONS = _P2_ACTIONS | _P3_ACTIONS
#: The one owner the matrix lets add a card to the SHARED queue (defer:queue).
_OPERATOR_EMPLOYEE_ID = "emp_owner"
# Review F1: the recovery states are decidable — a failed_retryable send can be
# retried/dropped and a reconcile_required crash can be dismissed by its owner.
_DECIDABLE = frozenset(
    {
        "open",
        "snoozed",
        "draft_pending",
        "awaiting_approval",
        "failed_retryable",
        "reconcile_required",
    }
)


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    params: dict[str, Any] | None = None
    note: str | None = None
    tags: list[str] | None = None


class CreateRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nl_text: str


class PromoteRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    live: bool = False


def fail(status: int, code: str, message: str, detail: Any = None) -> NoReturn:
    raise ApiError(status, code, message, detail)


@lru_cache(maxsize=1)
def get_decision_store() -> DecisionStore:
    """Process-wide decision store (same default DB path as the H1 Store)."""
    return DecisionStore(SqliteStore(default_db_path()))


DecisionStoreDep = Annotated[DecisionStore, Depends(get_decision_store)]


def _owner_for_principal(base_store: SqliteStore | None, principal: str) -> str | None:
    """Resolve a transport principal to the roster employee who owns its queue.

    ``system`` (and any principal that does not map to a real employee) resolves
    to ``None`` — the caller turns that into 403. Mirrors collab.py's resolver
    so identity is derived one way estate-wide.
    """
    from omniagentos.api.main import _SYSTEM_PRINCIPAL
    from omniagentos.api.routes.collab import _employee_for_email

    if principal == _SYSTEM_PRINCIPAL or base_store is None:
        return None
    goals = CompanyGoalsStore(base_store)
    if principal.startswith("emp_") and goals.get_employee(principal) is not None:
        return principal
    mapped = _employee_for_email(principal)
    if mapped and mapped.startswith("emp_") and goals.get_employee(mapped) is not None:
        return mapped
    return None


def _require_owner(store: DecisionStore, principal_header: str | None) -> str:
    """The authenticated owner, or 403 for the system/unmapped principal."""
    from omniagentos.api.main import _request_principal

    principal = _request_principal(principal_header)
    owner = _owner_for_principal(getattr(store, "_store", None), principal)
    if owner is None:
        fail(
            403,
            "system_principal_forbidden",
            "an authenticated employee principal is required for decisions",
        )
    return owner


def _present(decision: dict[str, Any]) -> dict[str, Any]:
    """Attach only server-derived affordances to an owner-scoped row."""
    presented = dict(decision)
    presented["available_actions"] = available_actions_for(decision)
    presented["suggested_snoozes"] = suggested_snoozes(decision)
    return presented


@router.get("")
def list_decisions(
    store: DecisionStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
    status: str | None = Query(default=None),
    classification: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """The authenticated owner's decisions, newest first. Owner-scoped only."""
    owner = _require_owner(store, x_omni_authenticated_principal)
    rows = store.list_decisions(
        owner_employee_id=owner,
        status=status,
        classification=classification,
        limit=limit,
    )
    return {"decisions": [_present(row) for row in rows], "owner_employee_id": owner}


@router.get("/{decision_id}")
def get_decision(
    decision_id: str,
    store: DecisionStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    """One decision — 404 unless it belongs to the authenticated owner."""
    owner = _require_owner(store, x_omni_authenticated_principal)
    decision = store.get_decision(decision_id, owner_employee_id=owner)
    if decision is None:
        fail(404, "not_found", "decision not found", {"id": decision_id})
    return _present(decision)


@router.post("/{decision_id}/decide")
def decide_decision(
    decision_id: str,
    body: DecideRequest,
    store: DecisionStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    """Apply one P2 owner action through the audited store transition seam."""
    owner = _require_owner(store, x_omni_authenticated_principal)
    action = (body.action or "").strip().lower()
    if action not in _DECIDE_ACTIONS:
        fail(
            400,
            "unsupported_action",
            f"action must be one of {sorted(_DECIDE_ACTIONS)} in this phase",
            {"action": body.action},
        )
    decision = store.get_decision(decision_id, owner_employee_id=owner)
    if decision is None:
        fail(404, "not_found", "decision not found", {"id": decision_id})
    if decision["status"] not in _DECIDABLE:
        fail(
            409,
            "already_decided",
            "decision is no longer in a decidable state",
            {"id": decision_id, "status": decision["status"]},
        )
    if action not in available_actions_for(decision):
        fail(
            400,
            "action_unavailable",
            f"action {action!r} is unavailable for this decision",
            {"available_actions": available_actions_for(decision)},
        )

    # A rule_proposal Decision (synthesis §0.5) resolves through the rule
    # approve/decline/edit handlers, never the reply/executor paths.
    if decision.get("source") == "rule_proposal":
        try:
            return _present(
                _decide_rule_proposal(
                    store, decision, owner, action, params=body.params or {}, note=body.note
                )
            )
        except DecisionConflictError as exc:
            current = exc.current or store.get_decision(decision_id, owner_employee_id=owner)
            fail(409, "already_decided", "decision was already handled on another surface", current)
        except ValueError as exc:
            fail(400, "validation", str(exc))

    params = body.params or {}
    try:
        if action == "dismiss":
            updated = store.resolve(
                decision_id,
                actor=owner,
                resolution="dismiss",
                note=body.note,
                tags=body.tags,
            )
        elif action == "deny":
            updated = store.resolve(
                decision_id,
                actor=owner,
                resolution="deny",
                note=body.note,
                tags=body.tags,
            )
        elif action == "snooze":
            updated = _apply_snooze(
                store,
                decision,
                owner,
                params=params,
                note=body.note,
                tags=body.tags,
            )
        elif action == "note":
            updated = _apply_note(store, decision, owner, note=body.note)
        elif action == "reply":
            intent = str(params.get("intent") or "").strip()
            if not intent:
                fail(400, "validation", "reply requires params.intent")
            updated = create_reply_draft(
                store,
                decision,
                actor=owner,
                intent=intent,
                note=body.note,
                tags=body.tags,
            )
            if params.get("request_slack_approval") is True:
                updated = _request_slack_approval(store, updated, owner)
        elif action == "approve":
            updated = _apply_approve(
                store,
                decision,
                owner,
                params=params,
                note=body.note,
                tags=body.tags,
            )
        elif action == "delegate":
            updated = _apply_delegate(
                store,
                decision,
                owner,
                params=params,
                note=body.note,
                tags=body.tags,
            )
        elif action == "defer":
            updated = _apply_defer(
                store,
                decision,
                owner,
                params=params,
                note=body.note,
                tags=body.tags,
            )
        else:
            updated = _apply_edit(
                store,
                decision,
                owner,
                params=params,
                note=body.note,
                tags=body.tags,
            )
    except DecisionConflictError as exc:
        current = exc.current or store.get_decision(decision_id, owner_employee_id=owner)
        fail(
            409,
            "already_decided",
            "decision was already handled on another surface",
            current,
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return _present(updated)


@router.post("/rules")
def create_rule(
    body: CreateRuleRequest,
    store: DecisionStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    """Owner-scoped NL rule creation → a PROPOSED rule + its rule_proposal Decision.

    The owner is the authenticated principal (never a body field). The instruction
    is parsed and filed through the ONE proposal seam
    (:func:`omniagentos.edc.rules.propose_nl_rule`): the output is always
    ``state='proposed'`` and any ``auto_*`` guess is clamped to its pre-fill peer,
    so this route can NEVER activate a rule nor mint an automation kind — that is
    the ``/promote`` route's exclusive job. Returns ``(rule, decision)``; an
    un-parseable instruction is a clean 422.
    """
    owner = _require_owner(store, x_omni_authenticated_principal)
    text = (body.nl_text or "").strip()
    if not text:
        fail(400, "validation", "nl_text is required")
    from omniagentos.edc import rules as edc_rules

    try:
        result = edc_rules.propose_nl_rule(store, owner_employee_id=owner, text=text)
    except ValueError as exc:
        fail(400, "validation", str(exc))
    if result is None:
        fail(422, "unparseable_rule", "could not extract a usable rule from the instruction")
    rule, decision = result
    return {"rule": rule, "decision": _present(decision)}


@router.post("/rules/{rule_id}/promote")
def promote_rule(
    rule_id: str,
    body: PromoteRuleRequest,
    store: DecisionStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    """Owner-only, PER-RULE promotion of a proposed rule to an automation kind.

    This is the SOLE mint path for ``auto_delegate``/``auto_send`` (F11 /
    spec §15.12): a deliberate, per-rule owner action that flips the rule
    ``proposed→active`` with ``approved_by`` stamped, sets the automation ``kind``,
    and writes the live-execution gate PER RULE in ``action.live`` (never a global
    switch). A foreign/unknown rule id reads as absent (404), so an id-probe cannot
    distinguish "not yours" from "does not exist"; a non-automation ``kind`` is a
    clean 400.
    """
    owner = _require_owner(store, x_omni_authenticated_principal)
    kind = (body.kind or "").strip().lower()
    if kind not in AUTOMATION_RULE_KINDS:
        fail(
            400,
            "validation",
            f"promote kind must be one of {sorted(AUTOMATION_RULE_KINDS)}",
            {"kind": body.kind},
        )
    promoted = store.promote_rule(
        rule_id,
        owner_employee_id=owner,
        approved_by=owner,
        kind=kind,
        live=body.live,
    )
    if promoted is None:
        # get_rule missed (not the owner's / absent) OR the CAS found no
        # proposed/active source row — both surface as 404, never 403.
        fail(404, "not_found", "rule not found or not promotable", {"id": rule_id})
    return {"rule": promoted}


def _decide_rule_proposal(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    action: str,
    *,
    params: dict[str, Any],
    note: str | None = None,
) -> dict[str, Any]:
    """Approve / decline / note / edit a ``rule_proposal`` Decision (synthesis §0.5).

    Approve flips the linked rule ``proposed→active`` (keeping its pre-fill kind);
    decline flips it ``declined`` (30-day re-proposal suppression). Edit adjusts
    the still-proposed rule's matcher/action in place and re-renders the proposal.
    None of these ever mints an automation kind — that is an explicit per-rule
    promotion only.
    """
    from omniagentos.edc import rules as edc_rules

    if action == "approve":
        return edc_rules.approve_rule_proposal(store, decision, actor=owner)
    if action == "deny":
        return edc_rules.decline_rule_proposal(store, decision, actor=owner)
    if action == "note":
        # Review minor: read the note from ``body.note`` like every other decide
        # action, not ``params['note']`` — the decide contract carries the note at
        # the top level, so a rule_proposal note was silently dropped before.
        return _apply_note(store, decision, owner, note=note)
    if action != "edit":
        # Review minor: an action that is not one a rule_proposal supports must NOT
        # fall through to the edit branch and emit a misleading "edit requires
        # params.matcher" 400 — refuse it as an unsupported action explicitly.
        fail(
            400,
            "unsupported_action",
            f"action {action!r} is not supported on a rule proposal (use approve/deny/edit/note)",
            {"action": action},
        )
    # action == "edit": adjust the proposed rule and re-render the proposal.
    rule_id = str(decision.get("source_ref") or "")
    rule = store.get_rule(rule_id, owner_employee_id=owner) if rule_id else None
    if rule is None or rule.get("state") != "proposed":
        fail(409, "not_editable", "the proposed rule is no longer editable")
    fields: dict[str, Any] = {}
    if isinstance(params.get("matcher"), dict):
        fields["matcher"] = params["matcher"]
    if isinstance(params.get("action"), dict):
        fields["action"] = params["action"]
    if not fields:
        fail(400, "validation", "edit requires params.matcher and/or params.action")
    updated_rule = store.update_rule(rule_id, owner_employee_id=owner, fields=fields)
    assert updated_rule is not None
    human_line = edc_rules.render_rule(
        str(updated_rule["kind"]),
        dict(updated_rule.get("matcher") or {}),
        dict(updated_rule.get("action") or {}),
    )
    recommended = dict(decision.get("recommended") or {})
    recommended["human_line"] = human_line
    recommended.setdefault("params", {})
    recommended["params"] = {
        **recommended["params"],
        "matcher": dict(updated_rule.get("matcher") or {}),
        "action": dict(updated_rule.get("action") or {}),
    }
    refreshed = store.update_decision(
        decision["id"], owner_employee_id=owner, fields={"recommended": recommended}
    )
    assert refreshed is not None
    return refreshed


def _request_slack_approval(
    store: DecisionStore, decision: dict[str, Any], owner: str
) -> dict[str, Any]:
    """Explicit dashboard request to put the current draft in the shared DM loop."""
    from omniagentos.edc.approvals import register_edc_approval
    from omniagentos.edc.main import _build_notifier

    notifier, reverse = _build_notifier(False)
    slack_id = reverse.get(owner)
    if notifier is None or not slack_id or not hasattr(notifier, "open_dm"):
        fail(503, "slack_unavailable", "the owner's Slack approval channel is unavailable")
    register_edc_approval(
        Path(__file__).resolve().parents[3],
        store,
        decision,
        notifier=cast(Any, notifier),
        owner_slack_id=slack_id,
    )
    updated = store.get_decision(decision["id"], owner_employee_id=owner)
    assert updated is not None
    return updated


def _apply_snooze(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    params: dict[str, Any],
    note: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    until = str(params.get("until") or "").strip()
    if not until:
        fail(400, "validation", "snooze requires params.until (ISO timestamp)")
    return resolve_snooze(
        store,
        decision,
        actor=owner,
        until=until,
        acknowledge_deadline=params.get("acknowledge_deadline") is True,
        note=note,
        tags=tags,
    )


def _apply_note(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    note: str | None,
) -> dict[str, Any]:
    text = (note or "").strip()
    if not text:
        fail(400, "validation", "note requires a non-empty note")
    existing = str(decision.get("notes") or "").strip()
    merged = f"{existing}\n{text}".strip() if existing else text
    updated = store.update_decision(
        decision["id"], owner_employee_id=owner, fields={"notes": merged}
    )
    store.append_event(
        decision["id"],
        owner_employee_id=owner,
        actor=owner,
        event="edit",
        note=text,
    )
    assert updated is not None
    return updated


def _apply_approve(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    params: dict[str, Any],
    note: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    supplied_sha = str(params.get("draft_sha256") or "").strip()
    draft = dict(decision.get("draft") or {})
    current_sha = draft_sha256(draft)
    if not supplied_sha:
        fail(400, "validation", "approve requires params.draft_sha256")
    if supplied_sha != current_sha or draft.get("sha256") != current_sha:
        fail(
            409,
            "draft_changed",
            "the draft changed after it was displayed; review and approve the current draft",
            {"current_draft_sha256": current_sha},
        )
    draft.update({"approved_sha256": current_sha, "approved_at": utc_now_iso()})
    claimed = store.resolve(
        decision["id"],
        actor=owner,
        resolution="approve",
        note=note,
        tags=tags,
        params={"draft": draft, "expected_draft_sha256": current_sha},
    )
    try:
        return run_executor(store, claimed, actor=owner)
    except Exception as exc:  # noqa: BLE001 — executor persisted an F03 recovery state
        current = store.get_decision(decision["id"], owner_employee_id=owner)
        # F2: the 502 body carries a non-secret type+reason label, never str(exc).
        fail(502, "effect_failed", safe_error(exc), current)


def _apply_delegate(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    params: dict[str, Any],
    note: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    """Delegate the decision to another employee — one owned board card + one DM.

    The matrix is pre-validated HERE (real, active, non-self assignee) BEFORE
    ``resolve`` consumes the one-shot authority, so a bad assignee is a clean 4xx
    rather than a decision stranded ``in_progress``. The executor re-enforces the
    same matrix (defence in depth); the send-before-act preview is intrinsic —
    ``run_executor`` renders the delegate preview before creating the card.
    """
    from omniagentos.edc import actions as edc_actions
    from omniagentos.edc.main import _build_notifier
    from omniagentos.team import tasks as team_tasks

    assignee = str(params.get("assignee") or "").strip()
    if not assignee:
        fail(400, "validation", "delegate requires params.assignee")
    if assignee == owner:
        fail(400, "validation", "a decision cannot be delegated to its own owner")
    if assignee not in team_tasks.active_employee_ids(store._store):
        fail(400, "validation", f"{assignee} is not on the active roster")

    claimed = store.resolve(
        decision["id"],
        actor=owner,
        resolution="delegate",
        note=note,
        tags=tags,
        params={"execution": {"assignee": assignee}},
    )
    notifier, reverse = _build_notifier(False)
    try:
        return edc_actions.delegate(
            store, claimed, actor=owner, notifier=notifier, reverse_map=reverse
        )
    except PermissionError as exc:
        fail(403, "forbidden", str(exc))
    except Exception as exc:  # noqa: BLE001 — executor persisted an F03 recovery state
        current = store.get_decision(decision["id"], owner_employee_id=owner)
        fail(502, "effect_failed", safe_error(exc), current)


def _apply_defer(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    params: dict[str, Any],
    note: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    """Defer the decision to the shared queue (the operator) or a workqueue machine unit.

    The lane matrix is pre-validated BEFORE ``resolve`` consumes authority:
    queue-add is operator-only, and the machine lane needs a repo-shaped
    recommendation plus a reachable workqueue store. Servicing them here funnels
    through ``actions.defer`` → ``run_executor`` (owner-only CAS, idempotent
    re-entry from C1), keeping the ``defer`` resolution token the store expects.
    """
    from omniagentos.edc import actions as edc_actions
    from omniagentos.edc.main import _completion_wq_store

    mode = str(params.get("mode") or "queue").strip().lower()
    if mode not in {"queue", "machine"}:
        fail(400, "validation", "defer params.mode must be 'queue' or 'machine'")
    wq_store: Any = None
    if mode == "queue":
        if owner != _OPERATOR_EMPLOYEE_ID:
            fail(
                403,
                "forbidden",
                "adding to the shared queue is the operator-only; delegate or snooze instead",
            )
    else:  # machine
        if machine_spec(decision.get("recommended")) is None:
            fail(400, "validation", "defer:machine requires a repo-shaped recommended action")
        wq_store = _completion_wq_store()
        if wq_store is None:
            fail(503, "workqueue_unavailable", "defer:machine needs a workqueue store (set WQ_DB)")

    claimed = store.resolve(
        decision["id"],
        actor=owner,
        resolution="defer",
        note=note,
        tags=tags,
        params={"execution": {"defer_mode": mode}},
    )
    try:
        return edc_actions.defer(store, claimed, actor=owner, mode=mode, wq_store=wq_store)
    except PermissionError as exc:
        fail(403, "forbidden", str(exc))
    except Exception as exc:  # noqa: BLE001 — executor persisted an F03 recovery state
        current = store.get_decision(decision["id"], owner_employee_id=owner)
        fail(502, "effect_failed", safe_error(exc), current)


def _apply_edit(
    store: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    params: dict[str, Any],
    note: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    """Edit-reclassify: the MAYBE "needs me" promotion, or a recommended edit."""
    if decision.get("draft") and ("subject" in params or "body" in params):
        return edit_reply_draft(
            store,
            decision,
            actor=owner,
            subject=str(params.get("subject") or ""),
            body=str(params.get("body") or ""),
            note=note,
            tags=tags,
        )
    reclassify = str(params.get("reclassify") or "").strip().lower()
    if reclassify:
        if reclassify not in {"urgent", "needs_owner", "maybe", "ignore"}:
            fail(400, "validation", "reclassify must be a valid classification")
        updated = store.reclassify_decision(
            decision["id"],
            owner_employee_id=owner,
            fields={
                "classification": reclassify,
                "surfaced": 0,
            },
            actor=owner,
            note=note or f"owner reclassify -> {reclassify}",
        )
        if updated is None:
            fail(409, "not_reclassifiable", "decision can no longer be reclassified")
        return updated
    recommended = params.get("recommended")
    if not isinstance(recommended, dict):
        fail(
            400,
            "validation",
            "edit requires draft subject/body, params.reclassify, or params.recommended",
        )
    return store.resolve(
        decision["id"],
        actor=owner,
        resolution="edit",
        note=note or "recommended edited",
        tags=tags,
        params={"recommended": recommended},
    )


__all__ = ["router"]
