"""The park/approve bridge: where an unattended loop stops and a human decides.

One row, one page, one decision. This module is the only place in the package
that mints an approval, and the only place that reads one back as authority.

Two invariants carry the whole design, and both are load-bearing rather than
tidy:

**Creation is idempotent because the id is deterministic.** A durable executor
re-runs the code *before* the park when a thread resumes, so a park that minted a
random id would mint a fresh row — and page a human — every single tick. The id
is derived from the effect's identity, so a resumed tick re-derives the same id,
finds its own row, and pages nobody.

**Expiry aborts; it never approves.** A pending row past its deadline is closed
as expired and the effect is abandoned. A late approval on an already-expired row
is not authority either: :func:`read_outcome` checks expiry *before* it looks at
the state, so a row that reads ``approved`` past its deadline still reads as not
approved. A human authorised an act at a moment, not a standing permission.

**A human approves an ACTION, never a slot.** The row records the effect's full
binding — template, instance, node, tool, action class, and a digest of the exact
arguments — and the execution seam re-checks it at the moment of the call. Change
one argument between the decision and the execution and the approval stops
matching. The id preimage carries the argument digest too, which is what stops
that rule from becoming a deadlock: changed arguments do not fail a re-check
against a stale row, they derive a *different* id and park again. Without that,
a T2 effect whose arguments moved under a fixed business key got the old row
back forever, failed the binding check forever, and could never execute.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import (
    ApprovalState,
    GateVerdict,
    LoopError,
    LoopTool,
    RecordKind,
    args_digest,
    digest_key,
)
from selfloop.ledger import AUTOMATION_ACTOR_PREFIX, DecisionRecord, emit, write_history

#: Marks a row as owned by this runtime, so a host whose approvals table is
#: shared with other producers can tell them apart without parsing the payload.
LOOP_APPROVAL_KIND = "loop_approval"

#: Hours a decision remains authority when neither the caller nor the policy
#: port says otherwise.
DEFAULT_TTL_HOURS = 8

#: Event channel for everything this module records.
APPROVAL_EVENT_KIND = "approval"

#: Identity prefixes that are NOT a human.
#:
#: The structural guarantee is the first one: :attr:`LoopContext.actor` is
#: ``loop:<instance>`` and there is no other string the loop can put in a
#: decision field, so self-approval is impossible rather than merely forbidden.
#: The rest are a courtesy net for hosts whose schedulers, bots and service
#: accounts follow the same convention — they close a hole the loop cannot open
#: on its own, which is an operator wiring a bot into the decision path.
AUTOMATION_IDENTITY_PREFIXES: tuple[str, ...] = (
    AUTOMATION_ACTOR_PREFIX,
    "system:",
    "service:",
    "svc:",
    "bot:",
    "agent:",
    "runner:",
    "cron:",
)

#: The ASCII unit separator, joining the parts of an id preimage. It cannot occur
#: in any of the parts, so the join is unambiguous — see :func:`approval_id`.
_SEP = "\x1f"


def approval_id(
    instance_id: str,
    template: str,
    node: str,
    tool: str,
    business_key: str,
    args_digest_: str,
) -> str:
    """Deterministic ``apr_<20 hex>`` id for one prospective action.

    Determinism is what makes row creation idempotent across a replay. Every axis
    that can distinguish two DIFFERENT real-world actions is in the preimage:

    * without ``template`` and ``tool``, two instances that share a name — or one
      instance whose row was retargeted at another template — mint the same id
      and can consume each other's approvals, which is the confused-deputy case;
    * without the **argument digest**, an effect whose arguments changed under a
      fixed business key re-derives the *old* id, gets the old row back, fails
      the binding re-check at the seam, and can never execute. Including it means
      changed arguments park again instead of deadlocking.

    ``\\x1f`` separators, so that ``node="a"`` + ``tool="bc"`` cannot collide with
    ``node="ab"`` + ``tool="c"``. The id is only half the defence in any case:
    :func:`read_outcome` re-checks the binding stored on the row, so an id
    collision on its own authorises nothing.
    """
    parts = _SEP.join((instance_id, template, node, tool, business_key, args_digest_))
    return f"apr_{hashlib.sha256(parts.encode()).hexdigest()[:20]}"


@dataclass(frozen=True)
class ApprovalOutcome:
    """The authoritative reading of one approval row. Produced by a pure read."""

    approval_id: str
    state: str
    #: True ONLY when a human approved this exact action and the decision has not
    #: expired. Every other reading — missing, pending, rejected, expired, bound
    #: to a different action, decided by an automation identity — is False.
    approved: bool
    #: True when there is nothing left to wait for. A pending row is the only
    #: non-terminal reading; everything else is decided one way or the other.
    terminal: bool
    decided_by: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "state": self.state,
            "approved": self.approved,
            "terminal": self.terminal,
            "decided_by": self.decided_by,
            "reason": self.reason,
        }


def _stamp(ctx: LoopContext) -> str:
    """``Clock.now_iso``, or ``""`` when the clock is unusable."""
    try:
        return ctx.clock.now_iso()
    except Exception:  # noqa: BLE001 - an unusable clock must not crash a read
        return ""


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, or ``None``. Naive stamps are read as UTC.

    Returning ``None`` rather than raising is deliberate: every caller here
    treats an unparseable stamp as *expired*, which is the fail-closed direction,
    and an exception would instead take the decision away from the code that
    knows which direction is safe.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_expired(row: Mapping[str, Any], moment: str) -> bool:
    """Has this row's deadline passed? Unreadable stamps count as expired.

    This is one of the few places in the package that compares wall-clock stamps
    rather than reading the monotonic clock, and it is a considered exception
    rather than an oversight. A monotonic reading has a per-process origin, so it
    cannot express "eight hours from when this row was written" to a *different*
    process on a *different* day, which is exactly what an approval TTL is.

    The consequence is stated plainly. A clock that runs fast expires an approval
    early: the effect does not run, which is the fail-closed direction. A clock
    that runs slow lets a decision stand longer than intended, and that is the
    real cost of using a wall clock here — which is why expiry is a bound on how
    long a human's decision is honoured, and not a security boundary on its own.
    The boundary is the binding re-check, and that reads no clock at all.
    """
    deadline = _parse_iso(row.get("expires_at"))
    if deadline is None:
        return True
    now = _parse_iso(moment)
    if now is None:
        return True
    return now >= deadline


def _is_automation_identity(who: str) -> bool:
    """True when *who* is not a person. Blank counts as not a person."""
    identity = who.strip().lower()
    if not identity:
        return True
    return any(identity.startswith(prefix) for prefix in AUTOMATION_IDENTITY_PREFIXES)


def stored_binding(row: Mapping[str, Any]) -> dict[str, Any]:
    """The effect identity recorded on an approval row when it was created."""
    params = row.get("params")
    if not isinstance(params, Mapping):
        return {}
    binding = params.get("binding")
    return dict(binding) if isinstance(binding, Mapping) else {}


def read_outcome(
    ctx: LoopContext,
    approval_row_id: str,
    *,
    binding: Mapping[str, Any] | None = None,
    now_iso: str | None = None,
) -> ApprovalOutcome:
    """Classify one approval row. **Writes nothing.** Fails closed on everything.

    This is the function the execution seam trusts, and the reason it can be
    trusted is that there is no path through it that turns an absence into a
    permission. Five refusals, in this order, and the order matters:

    1. **No row.** A read must never mint one, so a missing row is a refusal and
       not an opportunity to create what was expected.
    2. **Binding drift.** When *binding* is supplied — the seam always supplies
       it — the identity recorded when the row was created must still match the
       effect about to run. The refusal names the keys that differ, because "this
       approval is for a different action" is useless to an operator who cannot
       see which part moved.
    3. **Expiry, checked before state.** A row past its deadline is expired even
       when it reads ``approved``. A late approval on a dead request is not
       resume authority.
    4. **A non-human decider.** A loop approval is always-human. The loop's own
       actor is ``loop:<instance>``, so it has no string it could write here that
       would satisfy this check — self-approval is structurally impossible rather
       than merely against the rules.
    5. **Anything else.** A state this package does not recognise is not an
       approval. Absence of a verdict is never the most favourable outcome.

    Those five checks are written out **inline**, deliberately, rather than
    delegated to a pluggable "does this approval satisfy the gate?" hook. A
    substitutable implementation of this predicate is a substitutable weakening
    of it: the whole value of the rule is that there is exactly one of it, in one
    place, that a reviewer can read end to end and a counterfeit mutation can be
    aimed at.
    """
    moment = now_iso or _stamp(ctx)
    row = ctx.approvals.get(approval_row_id)
    if row is None:
        return ApprovalOutcome(
            approval_id=approval_row_id,
            state="missing",
            approved=False,
            terminal=True,
            decided_by="",
            reason="no approval row exists for this id",
        )

    state = str(row.get("state") or "")
    decided_by = str(row.get("decided_by") or "")

    if binding is not None:
        recorded = stored_binding(row)
        wanted = dict(binding)
        if recorded != wanted:
            differing = sorted(
                field
                for field in set(recorded) | set(wanted)
                if recorded.get(field) != wanted.get(field)
            )
            return ApprovalOutcome(
                approval_id=approval_row_id,
                state=state,
                approved=False,
                terminal=True,
                decided_by=decided_by,
                reason=(
                    "the approval is bound to a different action; it differs on "
                    f"{differing}. A human approves an action, not a slot."
                ),
            )

    if _is_expired(row, moment):
        return ApprovalOutcome(
            approval_id=approval_row_id,
            state=ApprovalState.EXPIRED.value,
            approved=False,
            terminal=True,
            decided_by=decided_by,
            reason=f"expired at {row.get('expires_at')!r}; a decision is not a standing permission",
        )

    if state == ApprovalState.APPROVED.value:
        if _is_automation_identity(decided_by):
            return ApprovalOutcome(
                approval_id=approval_row_id,
                state=state,
                approved=False,
                terminal=True,
                decided_by=decided_by,
                reason=(
                    f"decided by the non-human identity {decided_by!r}; a loop approval "
                    "must be decided by a person"
                ),
            )
        return ApprovalOutcome(
            approval_id=approval_row_id,
            state=state,
            approved=True,
            terminal=True,
            decided_by=decided_by,
            reason="approved by a human",
        )

    if state in (ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value):
        return ApprovalOutcome(
            approval_id=approval_row_id,
            state=state,
            approved=False,
            terminal=True,
            decided_by=decided_by,
            reason=f"a human {state} this request",
        )

    if state == ApprovalState.PENDING.value:
        return ApprovalOutcome(
            approval_id=approval_row_id,
            state=state,
            approved=False,
            terminal=False,
            decided_by=decided_by,
            reason="awaiting a human",
        )

    return ApprovalOutcome(
        approval_id=approval_row_id,
        state=state or "unknown",
        approved=False,
        terminal=True,
        decided_by=decided_by,
        reason=(
            f"the row carries a state this package does not recognise ({state!r}); "
            "refusing to read it as authority"
        ),
    )


def resolve_for_resume(ctx: LoopContext, approval_row_id: str) -> ApprovalOutcome:
    """Read the row and CLOSE it if its deadline has passed. Expiry is an abort.

    The one function here that writes, and it writes exactly one thing: a pending
    row whose deadline has passed is moved to ``expired`` so an operator's inbox
    stops showing a request nobody can usefully answer. The transition is a
    compare-and-set from ``pending`` inside the store, so it cannot overwrite a
    decision that landed a moment earlier.

    The reading is unchanged by the write: :func:`read_outcome` had already
    settled this row as expired, and moving the state merely makes the store
    agree with what every reader already saw.
    """
    outcome = read_outcome(ctx, approval_row_id)
    if outcome.state != ApprovalState.EXPIRED.value:
        return outcome

    row = ctx.approvals.get(approval_row_id) or {}
    closed = ctx.approvals.decide(
        approval_row_id,
        state=ApprovalState.EXPIRED.value,
        by=ctx.actor,
        note="expired without a human decision — the loop effect was abandoned (fail closed)",
        at=_stamp(ctx),
    )
    if closed:
        _record_decision(
            ctx,
            approval_row_id,
            action="approval_expired",
            decision=ApprovalState.EXPIRED.value,
            subject=str(stored_binding(row).get("tool") or ""),
            reason="deadline passed with no human decision",
        )
    return outcome


def _expires_at(ctx: LoopContext, ttl_hours: int | None) -> str:
    """The deadline for a decision made now, from the context's own clock.

    Read from the clock rather than from ``datetime.now`` so a caller who pinned
    the clock — a backfill, a replay test — gets a deadline consistent with the
    stamps on everything else it writes.

    Refuses rather than guessing when the clock's output cannot be parsed. A row
    with a meaningless deadline is worse than no row: every reader treats an
    unreadable deadline as expired, so the effect would park and then be
    abandoned on the very next tick, forever, with nothing in the record
    explaining why.
    """
    hours = ttl_hours
    if hours is None:
        declared = getattr(ctx.policy, "approval_expiry_hours", DEFAULT_TTL_HOURS)
        hours = int(declared or DEFAULT_TTL_HOURS)
    stamp = _stamp(ctx)
    now = _parse_iso(stamp)
    if now is None:
        raise LoopError(
            f"cannot compute an approval deadline: the clock returned {stamp!r}, which is "
            "not a parseable ISO-8601 stamp. Refusing to write an approval whose deadline "
            "every reader would treat as already passed."
        )
    return (now + timedelta(hours=max(1, hours))).isoformat()


def deep_link(ctx: LoopContext, approval_row_id: str) -> str:
    """A URL a human can click, or ``""`` when no dashboard origin was configured.

    Empty rather than a bare path, because a link to a dashboard that does not
    exist is worse than no link: an operator clicks it before they read the id,
    gets nothing, and now distrusts the page.
    """
    origin = ctx.dashboard_origin.rstrip("/")
    if not origin:
        return ""
    return f"{origin}/approvals?approval={approval_row_id}"


def ensure_approval(
    ctx: LoopContext,
    *,
    node: str,
    tool: LoopTool,
    args: Mapping[str, Any],
    business_key: str,
    verdict: GateVerdict,
    ttl_hours: int | None = None,
) -> Mapping[str, Any]:
    """Create — once — the pending approval row for a parked effect.

    Idempotent by construction. A second call for the same action derives the
    same id, the store's insert-if-absent returns False, and this returns the
    existing row without paging anybody. One row, one page, ever. That is what
    makes it safe on the replay path of a durable executor, which re-runs
    everything before the park each time the thread resumes.

    The row records the effect's full binding (see
    :func:`selfloop.tools.effect_binding`) so the seam can later prove the human
    approved THIS action, and a redacted copy of the arguments so whoever opens
    the approvals page can see what they are agreeing to without the page
    becoming a place credentials are published.

    **When arguments change**, the id changes, so this mints a new pending row
    and the effect parks again. That is the intended behaviour and it is why the
    argument digest is in the id preimage: the alternative, returning the row
    that was minted for the old arguments, produces a row that can never satisfy
    the seam's binding check and an effect that can never run.

    **When something else in the binding drifts** — an action class re-declared
    on the tool, say — the id is unchanged but the stored binding no longer
    matches, and this refuses rather than handing back a row that could never
    authorise the effect. A refusal reaches an operator; a stale row would have
    parked forever in silence.
    """
    # Imported here rather than at module scope, and the direction is the reason.
    # ``selfloop.tools`` imports this module at import time, because the seam is
    # the thing that needs the approval machinery; making the edge symmetric at
    # module scope would be a genuine cycle. Deferring the one call this module
    # makes back into the seam keeps the dependency one-directional on paper and
    # resolvable in practice.
    from selfloop.tools import effect_binding

    binding = effect_binding(ctx, node, tool, args)
    row_id = approval_id(
        ctx.instance_id, ctx.template, node, tool.name, business_key, args_digest(args)
    )

    existing = ctx.approvals.get(row_id)
    if existing is not None:
        recorded = stored_binding(existing)
        if recorded != binding:
            differing = sorted(
                field
                for field in set(recorded) | set(binding)
                if recorded.get(field) != binding.get(field)
            )
            raise LoopError(
                f"approval {row_id} already exists but is bound to a different action "
                f"(differs on {differing}). Refusing to reuse it: the execution seam would "
                "reject it at every future tick, so the effect would park forever with "
                "nothing in the record explaining why."
            )
        return existing

    params: dict[str, Any] = {
        "instance": ctx.instance_id,
        "template": ctx.template,
        "node": node,
        "tool": tool.name,
        "tier": verdict.tier.name,
        "business_key": business_key,
        "binding": binding,
        "args": redact_args(args),
    }
    row: dict[str, Any] = {
        # ``approval_id`` and not ``id``: one spelling, which both shipped
        # adapters read, because a join key spelled two ways creates a second
        # silent namespace where every write succeeds and every read returns
        # nothing.
        "approval_id": row_id,
        "kind": LOOP_APPROVAL_KIND,
        "action_class": verdict.action_class.value,
        "proposed_action": f"{ctx.instance_id}/{node}: {tool.description or tool.name}",
        "evidence": verdict.reason,
        "params": params,
        "state": ApprovalState.PENDING.value,
        "expires_at": _expires_at(ctx, ttl_hours),
        "created_at": _stamp(ctx),
        "requested_by": ctx.actor,
    }

    created = bool(ctx.approvals.create(row))
    if created:
        _record_decision(
            ctx,
            row_id,
            action="approval_requested",
            decision=ApprovalState.PENDING.value,
            subject=tool.name,
            reason=verdict.reason,
            node=node,
            tier=int(verdict.tier),
            detail=params,
        )
        page(ctx, row_id, tool=tool, node=node, verdict=verdict)

    return ctx.approvals.get(row_id) or row


def page(
    ctx: LoopContext,
    approval_row_id: str,
    *,
    tool: LoopTool,
    node: str,
    verdict: GateVerdict,
) -> bool:
    """Tell a human that something is parked. **Never raises.**

    Returns whether delivery was CONFIRMED. The hardest operational lesson behind
    this function is that a request nobody sees is a request nobody answers, and
    under fail-closed expiry that silently means the loop never acts — an audit
    of one deployment found fifteen of sixteen approvals expiring undecided.

    A delivery event is recorded **only on a confirmed send**. Recording one on a
    failed send would turn a single transient outage into a permanently unpaged
    approval, because that same recorded event is the dedupe key that stops the
    next tick from paging again. A failure is recorded too, under a different
    action, so it is visible without poisoning the dedupe.
    """
    link = deep_link(ctx, approval_row_id)
    summary = (
        f"{ctx.instance_id}/{node}: {tool.description or tool.name} needs approval "
        f"(tool {tool.name}, tier {verdict.tier.name}, {verdict.action_class.value}). "
        f"{verdict.reason}"
    )
    try:
        delivered = bool(
            ctx.notifier.page(approval_id=approval_row_id, summary=summary, deep_link=link)
        )
        detail = ""
    except Exception as exc:  # noqa: BLE001 - paging must never fail a tick
        delivered, detail = False, f"notifier raised {type(exc).__name__}: {exc}"

    emit(
        ctx,
        APPROVAL_EVENT_KIND,
        "approval_paged" if delivered else "approval_page_failed",
        {"approval_id": approval_row_id, "delivered": delivered, "detail": detail},
        node=node,
    )
    return delivered


def _record_decision(
    ctx: LoopContext,
    approval_row_id: str,
    *,
    action: str,
    decision: str,
    subject: str,
    reason: str,
    node: str = "",
    tier: int | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Write one :class:`~selfloop.ledger.DecisionRecord` and emit its event.

    HISTORY, with a deterministic id: a replayed tick re-derives the same id and
    the insert-if-absent refuses, so the record of "we asked a human" exists
    exactly once however many times the code around it runs.
    """
    record = DecisionRecord(
        id=f"dec_{digest_key(action, approval_row_id)[:20]}",
        at=_stamp(ctx),
        instance_id=ctx.instance_id,
        template=ctx.template,
        node=node,
        subject=subject,
        decision=decision,
        by=ctx.actor,
        reason=reason,
        tier=tier,
        approval_id=approval_row_id,
        detail=dict(detail or {}),
    )
    write_history(ctx, RecordKind.DECISION, record.id, record.as_dict())
    emit(
        ctx,
        APPROVAL_EVENT_KIND,
        action,
        {"approval_id": approval_row_id, "decision": decision, "subject": subject},
        node=node,
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: Key-name fragments whose VALUE is a credential whatever shape it has.
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
#: string whose key is innocuous, such as a shell command with a header in it.
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

#: Depth past which a payload is summarised rather than walked. It doubles as the
#: cycle guard: a self-referential structure terminates here instead of
#: recursing until the interpreter gives up.
_REDACT_MAX_DEPTH = 6

_REDACTED = "<redacted>"

#: Longest string kept intact. Beyond it the value is truncated, because an
#: approvals page is for a human to read and a 40KB blob is not read, it is
#: scrolled past.
_MAX_VALUE_CHARS = 400


def _is_secret_key(key: Any) -> bool:
    upper = str(key).upper()
    return any(marker in upper for marker in _SECRET_KEY_MARKERS)


def _redact_value(value: Any, depth: int) -> Any:
    """Redact one node of an argument tree.

    Recursive on purpose, and that is the fix rather than the feature. Inspecting
    only TOP-LEVEL keys meant ``{"headers": {"Authorization": "Bearer …"}}`` — the
    single most common way a credential appears in a connector call — was
    stringified whole and written verbatim into the row, which the approvals page
    then renders to whoever opens it.

    Two independent tests, because either alone leaks. Every key is matched by
    NAME, and every leaf is additionally matched by VALUE SHAPE, since the key
    that carries a token is not always called one.
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
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"


def redact_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments for the approvals page, with credential shapes removed at any depth.

    Note what this is NOT: it is not a sanitiser for the arguments the effect will
    run with. The tool is invoked with the real values; only the human-readable
    copy on the row is redacted. The argument digest on the binding is computed
    from the real arguments too, so redaction cannot change what a human's
    approval is bound to.
    """
    result = _redact_value(args, 0)
    return result if isinstance(result, dict) else {}


__all__ = [
    "APPROVAL_EVENT_KIND",
    "AUTOMATION_IDENTITY_PREFIXES",
    "DEFAULT_TTL_HOURS",
    "LOOP_APPROVAL_KIND",
    "ApprovalOutcome",
    "approval_id",
    "deep_link",
    "ensure_approval",
    "page",
    "read_outcome",
    "redact_args",
    "resolve_for_resume",
    "stored_binding",
]
