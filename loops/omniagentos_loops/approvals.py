"""interrupt ⇄ approvals-row bridge.

LangGraph's ``interrupt()`` parks a thread; OmniAgentOS's ``approvals`` table
(+ SSE + the existing ``/approvals`` dashboard page) is where a human decides.
This module is the only place the two meet:

* entering a gated effect writes ONE pending ``approvals`` row, an
  ``approval.requested`` event, and a Slack page with a deep link;
* the worker's next tick reads that row back and turns it into the value passed
  to ``Command(resume=...)``.

Two invariants are load-bearing:

**Idempotent row creation.** LangGraph re-runs the code *before* ``interrupt()``
when a thread resumes (PROTOTYPE_RESULTS P2 caveat). The approval id is
therefore derived deterministically from (instance, node, business key), so a
resume finds its own row instead of minting a duplicate every tick.

**Expiry aborts, never approves.** A pending row past ``expires_at`` is closed
as ``expired`` by the runtime and the effect is abandoned. A late human approve
on an already-expired row is not resume authority either — that rule lives in
``policy.approval_satisfies_gate`` and is reused here rather than re-derived.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.contracts import ApprovalState, Events, PolicyDecision, utc_now_iso
from omniagentos.policy import approval_satisfies_gate
from omniagentos_loops.observability import LoopEvents, emit
from omniagentos_loops.policy_gate import GateVerdict
from omniagentos_loops.tools import LoopTool

logger = logging.getLogger(__name__)

#: The approvals ``risk`` string that marks a row as owned by the loop runtime.
LOOP_APPROVAL_KIND = "loop_approval"

#: A loop approval is ALWAYS always_human: no bot, no runner, and no
#: ``system:loops`` expiry writer can satisfy it (policy._is_automation_identity).
LOOP_APPROVAL_DECISION = PolicyDecision(
    requires_approval=True,
    always_human=True,
    reason="loop effect at or above the T2 approval floor",
)

DEFAULT_TTL_HOURS = 8


def approval_id(instance_id: str, template: str, node: str, tool: str, business_key: str) -> str:
    """Deterministic ``apr_<20 hex>`` id for one (instance, template, node, tool, effect).

    Shape matches ``contracts.new_id('apr')`` so nothing downstream can tell a
    loop approval from any other row; determinism is what makes creation
    idempotent across the interrupt replay.

    Every axis that can distinguish two DIFFERENT real-world actions is in the
    preimage. Without ``template`` and ``tool``, two instances that share a name
    — or one instance whose row was retargeted at another template — mint the
    same id and can consume each other's approvals. The id is only half the
    defence: :func:`read_outcome` also re-checks the stored binding, so an id
    collision alone cannot authorise anything.
    """
    parts = "\x1f".join((instance_id, template, node, tool, business_key))
    return f"apr_{hashlib.sha256(parts.encode()).hexdigest()[:20]}"


def _get_row(ctx: Any, approval_row_id: str) -> dict[str, Any] | None:
    """Read one approvals row by id.

    A raw parameterized read on the store's shared connection — the same
    one-off-query escape hatch ``scheduler/routines_tick._latest_event_ts`` and
    ``RoutinesStore`` already use for reads outside their owning DAL. Writes
    still go through the store's own methods.
    """
    row = ctx.store._connection.execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_row_id,)
    ).fetchone()
    return None if row is None else dict(row)


@dataclass(frozen=True)
class ApprovalOutcome:
    """Authoritative reading of one approvals row."""

    approval_id: str
    state: str
    approved: bool
    terminal: bool
    decided_by: str
    reason: str

    def as_resume(self) -> dict[str, Any]:
        """The value handed to ``Command(resume=...)``."""
        return {
            "approval_id": self.approval_id,
            "state": self.state,
            "approved": self.approved,
            "decided_by": self.decided_by,
            "reason": self.reason,
        }


def stored_binding(row: Mapping[str, Any]) -> dict[str, Any]:
    """The effect identity recorded on an approvals row when it was created."""
    try:
        params = json.loads(str(row.get("params_json") or "{}"))
    except ValueError:
        return {}
    binding = params.get("binding")
    return dict(binding) if isinstance(binding, dict) else {}


def read_outcome(
    ctx: Any,
    approval_row_id: str,
    *,
    now_iso: str | None = None,
    binding: Mapping[str, Any] | None = None,
) -> ApprovalOutcome:
    """Classify an approvals row WITHOUT writing anything. Fails closed.

    When *binding* is supplied (the seam always supplies it), the row's RECORDED
    identity — template, instance, node, tool, action class and the digest of
    the exact arguments — must match the effect about to run. A human approves a
    specific action, not a slot: if any of those drifted after the row was
    written, this is a different action and the approval does not carry.
    """
    moment = now_iso or utc_now_iso()
    row = _get_row(ctx, approval_row_id)
    if row is None:
        return ApprovalOutcome(
            approval_row_id, "missing", False, True, "", "approval row not found"
        )

    if binding is not None:
        recorded = stored_binding(row)
        if recorded != dict(binding):
            differing = sorted(
                key
                for key in set(recorded) | set(binding)
                if recorded.get(key) != dict(binding).get(key)
            )
            return ApprovalOutcome(
                approval_row_id,
                str(row.get("state") or ""),
                False,
                True,
                str(row.get("decided_by") or ""),
                f"approval is bound to a different action (differs on {differing})",
            )

    gate = approval_satisfies_gate(row, LOOP_APPROVAL_DECISION, actor=ctx.actor, now_iso=moment)
    state = str(row.get("state") or "")
    decided_by = str(row.get("decided_by") or "")

    if gate.expired:
        # Binds even over a state='approved' row: a late approve on an expired
        # request is not resume authority (policy T-CODE-002).
        return ApprovalOutcome(
            approval_row_id, ApprovalState.EXPIRED.value, False, True, decided_by, "expired"
        )
    if state == ApprovalState.APPROVED.value:
        if gate.human_ok:
            return ApprovalOutcome(approval_row_id, state, True, True, decided_by, "human approved")
        return ApprovalOutcome(
            approval_row_id,
            state,
            False,
            True,
            decided_by,
            f"decided by non-human identity {decided_by!r}; a loop approval is always_human",
        )
    if state in (ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value):
        return ApprovalOutcome(approval_row_id, state, False, True, decided_by, f"human {state}")
    return ApprovalOutcome(
        approval_row_id, ApprovalState.PENDING.value, False, False, decided_by, "awaiting a human"
    )


def resolve_for_resume(ctx: Any, approval_row_id: str) -> ApprovalOutcome:
    """Read the row and CLOSE it if its TTL has elapsed. Expiry = abort."""
    outcome = read_outcome(ctx, approval_row_id)
    if outcome.state == ApprovalState.EXPIRED.value:
        # Best-effort transition pending -> expired so the inbox stops showing a
        # dead request. decide_approval is a no-op unless the row is pending.
        closed = ctx.store.decide_approval(
            approval_row_id,
            ApprovalState.EXPIRED.value,
            "system:loops",
            "expired without a human decision — loop effect aborted (fail-closed)",
        )
        if closed:
            emit(
                ctx,
                LoopEvents.APPROVAL,
                "loop.approval.expired",
                {"approval_id": approval_row_id},
            )
    return outcome


def _expires_at(ctx: Any, ttl_hours: int | None) -> str:
    hours = ttl_hours
    if hours is None:
        hours = int(
            getattr(ctx.policy_cfg, "approval_expiry_hours", DEFAULT_TTL_HOURS) or DEFAULT_TTL_HOURS
        )
    deadline = datetime.now(UTC) + timedelta(hours=max(1, hours))
    return deadline.strftime("%Y-%m-%dT%H:%M:%SZ")


def deep_link(ctx: Any, approval_row_id: str) -> str:
    return f"{ctx.dashboard_origin.rstrip('/')}/approvals?approval={approval_row_id}"


def ensure_approval(
    ctx: Any,
    *,
    node: str,
    tool: LoopTool,
    args: Mapping[str, Any],
    business_key: str,
    verdict: GateVerdict,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Create (once) the pending approvals row for a gated effect.

    Idempotent by construction: a second call for the same
    (instance, template, node, tool, business key) returns the existing row and
    pages nobody. The row records the effect's full binding (see
    :func:`omniagentos_loops.tools.effect_binding`) so the seam can later prove
    the human approved THIS action.
    """
    from omniagentos_loops.tools import effect_binding

    row_id = approval_id(ctx.instance_id, ctx.template, node, tool.name, business_key)
    existing = _get_row(ctx, row_id)
    if existing is not None:
        return existing

    params = {
        "loop_instance": ctx.instance_id,
        "loop_template": ctx.template,
        "node": node,
        "tool": tool.name,
        "tier": verdict.tier.name,
        "business_key": business_key,
        "binding": effect_binding(ctx, node=node, tool=tool, args=args),
        "args": _redacted(args),
    }
    row = {
        "id": row_id,
        "action_class": verdict.action_class.value,
        "proposed_action": f"{ctx.instance_id}/{node}: {tool.description or tool.name}",
        "params_json": json.dumps(params, sort_keys=True, default=str),
        "risk": LOOP_APPROVAL_KIND,
        "evidence": verdict.reason,
        "state": ApprovalState.PENDING.value,
        "expires_at": _expires_at(ctx, ttl_hours),
        "created_at": utc_now_iso(),
    }
    ctx.store.create_approval(row)
    ctx.store.insert_event(
        type=Events.APPROVAL_REQUESTED,
        actor=ctx.actor,
        action="loop effect requires approval",
        target_type="approval",
        target_id=row_id,
        payload=params,
        trace_id=ctx.instance_id,
    )
    emit(ctx, LoopEvents.APPROVAL, "loop.approval.requested", {"approval_id": row_id, **params})
    page(ctx, row_id, tool=tool, node=node, verdict=verdict)
    return _get_row(ctx, row_id) or row


#: Key-name fragments whose VALUE is a credential regardless of shape.
_SECRET_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "KEY",
    "WEBHOOK",
    "CREDENTIAL",
    "AUTHORIZATION",
    "COOKIE",
    "SESSION_ID",
    "SIGNATURE",
    "PRIVATE",
)

#: Credential VALUE shapes, matched wherever they appear — including inside a
#: string whose key is innocuous (``"curl -H 'Authorization: Bearer …'"``).
_SECRET_VALUE_RE = re.compile(
    r"(?i)("
    r"bearer\s+[\w.\-+/=]{8,}"
    r"|basic\s+[A-Za-z0-9+/=]{12,}"
    r"|sk-[A-Za-z0-9_\-]{12,}"
    r"|xox[abeoprs]-[A-Za-z0-9\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|https://hooks\.slack\.com/\S+"
    r")"
)

#: Nesting depth past which a payload is summarised rather than walked. Also the
#: cycle guard: a self-referential structure terminates here.
_REDACT_MAX_DEPTH = 6

_REDACTED = "<redacted>"


def _is_secret_key(key: Any) -> bool:
    upper = str(key).upper()
    return any(marker in upper for marker in _SECRET_KEY_MARKERS)


def _redact_value(value: Any, depth: int) -> Any:
    """Redact one node of an argument tree.

    Recursive on purpose. The previous revision inspected only TOP-LEVEL keys,
    so ``{"headers": {"Authorization": "Bearer …"}}`` — the single most common
    way a credential appears in a connector call — was stringified whole and
    written verbatim into ``params_json``, which the approvals page renders to
    whoever opens it. Every nested key is now matched by NAME, and every leaf is
    additionally matched by VALUE SHAPE, because the key that carries a token is
    not always called one.
    """
    if depth > _REDACT_MAX_DEPTH:
        return f"<depth>{type(value).__name__}"
    if isinstance(value, Mapping):
        return {
            str(key): (_REDACTED if _is_secret_key(key) else _redact_value(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact_value(item, depth + 1) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    text = _SECRET_VALUE_RE.sub(_REDACTED, str(value))
    return text if len(text) <= 400 else text[:400] + "…"


def _redacted(args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments for the approvals UI with credential shapes removed, at any depth."""
    result = _redact_value(args, 0)
    return result if isinstance(result, dict) else {}


def page(
    ctx: Any, approval_row_id: str, *, tool: LoopTool, node: str, verdict: GateVerdict
) -> None:
    """Page a human on Slack with the deep link. Never raises.

    The audit's hard lesson was 15/16 approvals expiring undecided: a request
    nobody sees is a request nobody answers, and under fail-closed expiry that
    silently means "the loop never acts".
    """
    text = (
        f":lock: *Loop approval needed* — `{ctx.instance_id}` / `{node}`\n"
        f"tool `{tool.name}` (tier {verdict.tier.name}, {verdict.action_class.value})\n"
        f"{verdict.reason}\n"
        f"{deep_link(ctx, approval_row_id)}"
    )
    try:
        result = ctx.notifier(text)
        detail = getattr(result, "detail", str(result))
        ok = bool(getattr(result, "ok", False))
    except Exception as exc:  # noqa: BLE001 — paging must never fail the loop
        ok, detail = False, f"notifier raised: {type(exc).__name__}"
    if not ok:
        logger.warning("loop approval page not delivered (%s): %s", approval_row_id, detail)
    emit(
        ctx,
        LoopEvents.APPROVAL,
        "loop.approval.paged",
        {"approval_id": approval_row_id, "delivered": ok, "detail": detail},
    )


__all__ = [
    "DEFAULT_TTL_HOURS",
    "LOOP_APPROVAL_DECISION",
    "LOOP_APPROVAL_KIND",
    "ApprovalOutcome",
    "approval_id",
    "deep_link",
    "ensure_approval",
    "page",
    "read_outcome",
    "resolve_for_resume",
]
