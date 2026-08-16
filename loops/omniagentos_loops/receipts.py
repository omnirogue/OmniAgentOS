"""Idempotency receipts — the guard that makes at-least-once nodes safe.

PROTOTYPE_RESULTS P1/P7 measured the hazard directly: LangGraph replays a node
whose superstep never committed, so an external side effect performed inside it
happens **twice** on resume (``external_merge_count_for_sha: 2``). The guarded
variant — check a receipt before acting, complete it after — produced exactly
one. This module is that guard, backed by the ``idempotency`` table from
migration 001, which until now had no real producer.

What a receipt records
----------------------

Until 2026-08-01 a receipt bound *identity* and never *outcome*: ``guarded``
completed the receipt with whatever the tool returned, including W3's
``{"success": False, "returncode": 1}``. A failed ``launchctl`` repair was
therefore filed as a done effect, and — because the business key is
component+signature+day — that receipt then suppressed every retry for the rest
of the incident window. The service stayed down; the loop reported the incident
handled. Kimi's phrasing: *a receipt binds identity, never outcome*.

A receipt now records the OUTCOME of one ATTEMPT, in three terminal-ish states:

===============  ================================  ===========================
row              meaning                           next tick does
===============  ================================  ===========================
absent           never attempted                   claim it and act
claimed (no      the effect's state is UNKNOWN —   FAIL CLOSED
``result_json``) it may have happened              (``EffectStateUnknown``)
``succeeded``    it took effect                    replay the recorded result
``failed``       it ran and did NOT take effect    attempt the NEXT slot
===============  ================================  ===========================

Only ``succeeded`` short-circuits a replay. That is the whole fix.

A failed attempt RETURNS (``ReceiptOutcome.succeeded is False``) rather than
raising. The tick's status belongs to the template — its verify node is the
thing that knows what "it did not work" means and can say so precisely (see
``templates.common.verification_outcome``) — and a seam that raised would skip
that node for every template at once. What this module owes a caller is an
honest answer about the world, not a verdict about the tick.

Why attempt-keyed rows rather than a re-openable one
----------------------------------------------------

The alternative — reset one row from ``failed`` back to ``claimed`` and re-run
under the same key — quietly weakens the guarantee this module exists for. The
crash window that must keep failing closed is "claimed, then died before the
result was recorded", and a row that oscillates back into ``claimed`` is
indistinguishable from that. So each attempt gets its OWN row
(``<key>``, ``<key>#a2``, ``<key>#a3``) and each row lives exactly the lifecycle
it always had: claim -> act -> complete, once, forever. A crash between claim
and act leaves that attempt's row claimed-uncompleted, and the next tick still
aborts with :class:`EffectStateUnknown` — unchanged, untouched, still proved by
the kill drill. The retry budget is then structural (how many rows exist), not a
counter someone has to keep consistent across a crash.

What "succeeded" means
----------------------

Not "the tool said so". A receipt is marked succeeded only when BOTH hold:

1. the result does not declare failure (``success``/``ok`` false, a non-zero
   ``returncode``, a bare ``error``) — see :func:`declared_failure`; and
2. the tool's :data:`~omniagentos_loops.tools.VerifyFn` predicate, if it
   declares one, looked at the world afterwards and said yes.

The conjunction is deliberate and monotone: adding a verifier can only make
success harder to claim, so a weak or captured verifier cannot launder a
self-declared failure into a completed receipt. Where a mechanical external
signal exists it should be declared — for ``launchctl kickstart`` the honest
predicate is "the service is running afterwards", not "the subprocess exited 0".

Bounded escalation
------------------

``LoopTool.max_attempts`` (default 3 below the approval floor, 1 at T2+, hard
ceiling 10) bounds the attempts for ONE business key. When every slot holds a
recorded failure the guard raises :class:`EffectAttemptsExhausted` *without
reaching the tool*, which parks the tick for a human. A permanently-failing
effect escalates; it never hammers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from omniagentos_loops.contracts import (
    EffectAttemptsExhausted,
    EffectStateUnknown,
    EffectUnavailable,
)
from omniagentos_loops.observability import LoopEvents, emit
from omniagentos_loops.tools import (
    MAX_ATTEMPTS_CEILING,
    LoopTool,
    Verification,
    coerce_verification,
)

#: Receipt states. ``CLAIMED`` is the absence of a result, not a stored value.
CLAIMED = "claimed"
SUCCEEDED = "succeeded"
FAILED = "failed"

#: Marker + version of the outcome envelope stored in ``result_json``. A row
#: without it predates this module's outcome tracking (or was written by the
#: runner) and is read as SUCCEEDED — the old writer only ever completed a
#: receipt after the effect ran, so treating those as "already done" preserves
#: at-most-once for every receipt already in the production table.
ENVELOPE = "__loop_receipt__"
ENVELOPE_VERSION = 1


def receipt_key(
    instance_id: str, template: str, node: str, tool_name: str, business_key: str
) -> str:
    """``loop:<template>:<instance>:<node>:<tool>:<business-key>``.

    Namespaced by template as well as instance so two loops cannot collide in
    the shared ``idempotency`` table — a collision there does not merely confuse
    a report, it makes one loop's completed receipt suppress another loop's
    effect entirely (a silent, permanent no-op).

    The trailing ``business_key`` is deliberately NOT an argument digest. Only
    the tool knows which part of its arguments is the effect's identity: a
    health snapshot carries timestamps, and digesting the whole payload would
    mint a fresh key every tick and re-run the repair forever. The digest that
    *is* enforced lives on the approvals row (see ``tools.effect_binding``),
    where changing arguments must invalidate a human's decision rather than
    duplicate an effect.
    """
    return f"loop:{template}:{instance_id}:{node}:{tool_name}:{business_key}"


def attempt_key(key: str, attempt: int) -> str:
    """Row key for the *attempt*-th try at *key*.

    Attempt 1 keeps the base key unchanged, so every receipt written before
    attempts existed still names the effect it named. ``#a<n>`` is the suffix
    because ``#`` cannot appear in a template, instance, node or tool name
    (``paths.require_safe_name``), which leaves the business key as the only
    place it could occur — and a business key ending in a literal ``#a2`` would
    have to be minted by a template deliberately.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return key if attempt == 1 else f"{key}#a{attempt}"


@dataclass(frozen=True)
class ReceiptOutcome:
    """What :func:`guarded` returns: the recorded outcome of ONE attempt."""

    result: Any
    replayed: bool
    #: The row that authorises this outcome — the attempt key, not the base key,
    #: so the audit trail names a row that actually exists.
    key: str
    attempt: int = 1
    #: FALSE when the effect ran and did not take effect. The receipt says
    #: ``failed``, the next tick may attempt again, and the tick that produced
    #: it must not render as a success — which is the template's call to make,
    #: through its verify node, not the seam's.
    succeeded: bool = True
    #: ``True``/``False`` when the tool declared a verification predicate,
    #: ``None`` when it did not. ``False`` never reaches a caller (it is a
    #: failure), but it is recorded on the row for the operator.
    verified: bool | None = None
    detail: str = ""


def _decode(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def declared_failure(result: Any) -> str:
    """The reason *result* declares its own failure, or ``""``.

    A deliberately short, documented ladder over the conventions the tools in
    this repo actually use. It is the WEAK half of the success test (see the
    module docstring): it can only ever veto a success, never grant one, so a
    tool whose result says nothing is judged by its verification predicate, or —
    if it declares none — trusted, exactly as before.
    """
    if not isinstance(result, Mapping):
        return ""
    reasons: list[str] = []
    explicit_ok = False
    for flag in ("success", "ok"):
        if flag in result:
            if result[flag]:
                explicit_ok = True
            else:
                reasons.append(f"the tool returned {flag}={result[flag]!r}")
    if "returncode" in result and result["returncode"] not in (0, None):
        reasons.append(f"the tool returned returncode={result['returncode']!r}")
    if not reasons and not explicit_ok and result.get("error"):
        reasons.append(f"the tool returned error={str(result['error'])[:200]!r}")
    return "; ".join(reasons)


def _judge(
    tool: LoopTool, result: Any, args: Mapping[str, Any]
) -> tuple[Verification, bool | None]:
    """Decide whether the effect took effect. Both signals must be non-adverse."""
    reasons = [reason for reason in (declared_failure(result),) if reason]
    verified: bool | None = None

    if tool.verify is not None:
        try:
            verdict = coerce_verification(tool.verify(result, dict(args)))
        except Exception as exc:  # noqa: BLE001 — re-raised as UNKNOWN, see below
            # The effect RAN and we could not establish its outcome. That is the
            # unknown state, not a failure verdict: leave the row claimed so the
            # next tick fails closed instead of re-running a possibly-done effect.
            raise EffectStateUnknown(
                f"{tool.name}: the effect ran but its verification predicate raised "
                f"{type(exc).__name__}: {exc} — outcome unknown, refusing to record it"
            ) from exc
        verified = verdict.ok
        if not verdict.ok:
            reasons.append(f"verification failed: {verdict.detail or '<no detail>'}")

    return Verification(ok=not reasons, detail="; ".join(reasons)), verified


def _envelope(state: str, *, attempt: int, result: Any, verified: bool | None, detail: str) -> str:
    return json.dumps(
        {
            ENVELOPE: ENVELOPE_VERSION,
            "state": state,
            "attempt": attempt,
            "verified": verified,
            "detail": detail,
            "result": result,
        },
        default=str,
    )


def _recorded(raw: Any) -> tuple[str, Any, str]:
    """``(state, result, detail)`` for a completed row's ``result_json``."""
    decoded = _decode(raw)
    if isinstance(decoded, Mapping) and ENVELOPE in decoded:
        state = str(decoded.get("state") or SUCCEEDED)
        if state not in (SUCCEEDED, FAILED):
            # An unrecognised state is not a licence to act: treat it as done.
            state = SUCCEEDED
        return state, decoded.get("result"), str(decoded.get("detail") or "")
    return SUCCEEDED, decoded, ""


def guarded(
    ctx: Any,
    *,
    key: str,
    node: str,
    tool: LoopTool,
    execute: Callable[[], Any],
    args: Mapping[str, Any] | None = None,
) -> ReceiptOutcome:
    """Run *execute* at most once PER SUCCESS for *key*.

    Returns a :class:`ReceiptOutcome`. ``outcome.succeeded`` is the answer this
    module exists to give, and it is FALSE when the attempt ran and did not take
    effect: the row is recorded ``failed``, and a later tick gets the next
    attempt slot instead of replaying the failure forever.

    A failed attempt returns rather than raises on purpose. The tick's STATUS is
    a template decision — the verify node exists to make it (see
    ``templates.common.verification_outcome``) — and a seam that raised would
    take that decision away from every template at once, skipping the very node
    whose job is to say what went wrong. What the seam owes the template is an
    honest answer, not a verdict.

    It raises in exactly three cases, none of which is a success:

    * :class:`EffectStateUnknown` — a previous attempt was claimed and never
      completed (crash between claim and act), or a verification predicate
      raised. Never re-run; fail closed.
    * :class:`EffectAttemptsExhausted` — the budget for this business key is
      spent. The tool is NOT reached; the tick parks for a human.
    * :class:`EffectUnavailable` — the effect's authority was never REACHED, so
      the effect did not happen. This attempt's claim is RELEASED and the tick
      settles neutral and loud (absence is never failure — see the exception's
      own docstring). It is the one case where this module removes a row, and
      the removal cannot touch a completed one.
    """
    budget = tool.resolved_max_attempts()
    for attempt in range(1, budget + 1):
        outcome = _attempt(
            ctx,
            base_key=key,
            attempt=attempt,
            budget=budget,
            node=node,
            tool=tool,
            execute=execute,
            args=args or {},
        )
        if outcome is not None:
            return outcome

    emit(
        ctx,
        LoopEvents.EFFECT,
        "loop.effect.exhausted",
        {"node": node, "tool": tool.name, "receipt": key, "attempts": budget},
    )
    raise EffectAttemptsExhausted(
        f"{node}/{tool.name}: all {budget} attempts at receipt {key} are recorded "
        "failures — escalating instead of retrying"
    )


def _attempt(
    ctx: Any,
    *,
    base_key: str,
    attempt: int,
    budget: int,
    node: str,
    tool: LoopTool,
    execute: Callable[[], Any],
    args: Mapping[str, Any],
) -> ReceiptOutcome | None:
    """One attempt slot. ``None`` means "this slot already failed; try the next"."""
    store = ctx.store
    key = attempt_key(base_key, attempt)
    existing = store.idem_get(key)

    if existing is not None:
        state, recorded, detail = _recorded(existing.get("result_json"))
        if existing.get("result_json") is not None:
            if state == FAILED:
                return None
            emit(
                ctx,
                LoopEvents.EFFECT,
                "loop.effect.replayed",
                {"node": node, "tool": tool.name, "receipt": key, "attempt": attempt},
            )
            return ReceiptOutcome(
                result=recorded, replayed=True, key=key, attempt=attempt, detail=detail
            )
        if not tool.replay_on_unknown:
            # Fails closed FOREVER, by design and with a known cost: nothing in
            # this process can ever learn whether that effect ran, so there is
            # no state transition it would be honest to make on its own. A TTL
            # here would be the double-billing bug with a delay in front of it —
            # a timer observes nothing, so releasing on one converts every
            # UNKNOWN into an ABSENCE and lets the next tick re-issue a paid
            # call. The missing evidence lives at the provider, so the recovery
            # path is an operator who goes and looks; name it here, because a
            # loud failure that does not say how to clear it is only half loud.
            raise EffectStateUnknown(
                f"{node}/{tool.name}: receipt {key} was claimed but never completed — "
                "the external effect may already have happened; refusing to re-run. "
                "This will fail every tick until a human reconciles it: check the "
                "provider, then "
                "`python -m omniagentos.scheduler.loop_receipts_admin show --key "
                f"{key}`"
            )

    claimed = store.idem_insert(key, ctx.instance_id, node)
    if not claimed and existing is None:
        # Another worker claimed this key between our read and our insert.
        # Two processes racing one irreversible effect is exactly the case the
        # receipt exists to stop, so the loser fails closed rather than guessing.
        raise EffectStateUnknown(
            f"{node}/{tool.name}: receipt {key} was claimed concurrently; refusing to run"
        )

    try:
        result = execute()
    except EffectUnavailable as exc:
        # ABSENCE, not failure. The effect's authority was never reached, so
        # nothing happened — and a receipt for something that did not happen is
        # worse than no receipt: it either fails the next tick closed or spends
        # an attempt slot on someone else's outage. Release the claim, say so
        # loudly, and let the template render a NEUTRAL non-result.
        #
        # This is the only path in this module that removes a row, and
        # ``store.idem_release`` refuses to touch a COMPLETED one, so the
        # crash-window guarantee (claimed-then-died still fails closed) is
        # unchanged: a crash raises nothing, so it never gets here.
        released = False
        try:
            released = bool(store.idem_release(key))
        except Exception:  # noqa: BLE001 — the raise below is the real answer
            released = False
        emit(
            ctx,
            LoopEvents.EFFECT,
            "loop.effect.unavailable",
            {
                "node": node,
                "tool": tool.name,
                "receipt": key,
                "attempt": attempt,
                "reason": getattr(exc, "reason", "unavailable"),
                "detail": str(getattr(exc, "detail", "") or exc)[:500],
                "claim_released": released,
            },
        )
        raise
    verdict, verified = _judge(tool, result, args)

    if verdict.ok:
        store.idem_complete(
            key,
            _envelope(SUCCEEDED, attempt=attempt, result=result, verified=verified, detail=""),
        )
        emit(
            ctx,
            LoopEvents.EFFECT,
            "loop.effect.executed",
            {
                "node": node,
                "tool": tool.name,
                "receipt": key,
                "tier": tool.tier.name,
                "attempt": attempt,
                "verified": verified,
            },
        )
        return ReceiptOutcome(
            result=result, replayed=False, key=key, attempt=attempt, verified=verified
        )

    # The effect ran and did not take effect. Record THAT — the defect this
    # module was rewritten to close was filing exactly this as a done effect.
    store.idem_complete(
        key,
        _envelope(FAILED, attempt=attempt, result=result, verified=verified, detail=verdict.detail),
    )
    emit(
        ctx,
        LoopEvents.EFFECT,
        "loop.effect.failed",
        {
            "node": node,
            "tool": tool.name,
            "receipt": key,
            "tier": tool.tier.name,
            "attempt": attempt,
            "attempts_allowed": budget,
            "verified": verified,
            "detail": verdict.detail,
        },
    )
    remaining = budget - attempt
    return ReceiptOutcome(
        result=result,
        replayed=False,
        key=key,
        attempt=attempt,
        succeeded=False,
        verified=verified,
        detail=(
            f"attempt {attempt}/{budget} did not take effect ({verdict.detail}); "
            f"{remaining} attempt(s) left for receipt {base_key}"
        ),
    )


def receipt_state(ctx: Any, key: str, attempt: int = 1) -> str:
    """``absent`` | ``claimed`` | ``succeeded`` | ``failed`` for one attempt row."""
    row = ctx.store.idem_get(attempt_key(key, attempt))
    if row is None:
        return "absent"
    if row.get("result_json") is None:
        return CLAIMED
    return _recorded(row.get("result_json"))[0]


def receipt_exists(ctx: Any, key: str) -> bool:
    """True when a SUCCEEDED receipt exists for *key* (used by tests + drills).

    A failed attempt is emphatically not "a receipt exists": that reading is
    what suppressed W3's retries for a whole incident window.
    """
    return any(
        receipt_state(ctx, key, attempt) == SUCCEEDED
        for attempt in range(1, MAX_ATTEMPTS_CEILING + 1)
    )


__all__ = [
    "CLAIMED",
    "ENVELOPE",
    "ENVELOPE_VERSION",
    "FAILED",
    "SUCCEEDED",
    "ReceiptOutcome",
    "attempt_key",
    "declared_failure",
    "guarded",
    "receipt_exists",
    "receipt_key",
    "receipt_state",
]
