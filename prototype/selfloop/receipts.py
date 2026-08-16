"""Idempotency receipts — the guard that makes an at-least-once node safe.

A durable executor replays a node whose step never committed. That is not a bug
in the executor; it is the price of durability, and it means an external side
effect performed inside such a node happens **twice** on resume. Measured
directly on the system this package was extracted from: one merge, replayed,
produced two merges. The guarded variant — check a receipt before acting,
complete it after — produced exactly one. This module is that guard.

What a receipt records
----------------------

A receipt binds *identity* and *outcome*. Binding only identity is the failure
this module was rewritten to close, and it is worth stating concretely because
it reads as harmless: a repair tool returned ``{"success": false,
"returncode": 1}``, the guard completed the receipt with whatever the tool
returned, and a *failed* repair was therefore filed as a done effect. Because
the business key was stable for the whole incident window, that receipt then
suppressed every subsequent retry. The service stayed down; the loop reported
the incident handled.

So a receipt records the OUTCOME of one ATTEMPT, and only one of its values
short-circuits a replay:

======================  ================================  ==========================
row                     meaning                           the next tick does
======================  ================================  ==========================
absent                  never attempted                   claim it and act
claimed, no result      state is UNKNOWN — it MAY         FAIL CLOSED forever
                        have happened                     (``EffectStateUnknown``)
``succeeded``           it took effect                    replay the recorded result
``failed``              it ran and did NOT take effect    attempt the NEXT slot
``unavailable``         its authority was never reached,  attempt the NEXT slot,
                        so provably nothing happened      spending no budget
======================  ================================  ==========================

Why a TTL on the unknown state would be a bug, not a convenience
----------------------------------------------------------------

The obvious "fix" for a permanently-stuck unknown is to release it after N
minutes. It is the double-billing bug with a delay in front of it. **A timer
observes nothing.** Releasing on one converts every UNKNOWN into an ABSENCE and
lets the next tick re-issue a paid call, and it does so precisely in the case
where the first call *did* go through — because that is what "unknown" means.
The missing evidence lives at the external system, so the only honest recovery
is somebody who goes and looks: see :func:`reconcile`.

Why attempt-keyed rows rather than one re-openable row
------------------------------------------------------

The alternative — reset one row from ``failed`` back to ``claimed`` and re-run
under the same key — quietly weakens the guarantee this module exists for. The
crash window that must keep failing closed is "claimed, then died before the
result was recorded", and a row that oscillates back into ``claimed`` is
*indistinguishable* from it. So each attempt gets its OWN row (``<key>``,
``<key>#a2``, ``<key>#a3``) and each row lives exactly the lifecycle it always
had: claim -> act -> complete, once, forever. The retry budget is then
structural — how many rows exist — rather than a counter somebody has to keep
consistent across a crash.

What "succeeded" means
----------------------

Not "the tool said so". A receipt is marked succeeded only when BOTH hold:

1. the result does not declare its own failure (see :func:`declared_failure`), and
2. the tool's :data:`~selfloop.contracts.VerifyFn`, if it declares one, looked
   at the world afterwards and said yes.

The conjunction is deliberate and it is monotone: adding a verifier can only
make success *harder* to claim, so a weak or captured verifier cannot launder a
self-declared failure into a completed receipt. And a verify predicate that
RAISES is not a failure verdict — the effect ran and its outcome could not be
established, which is :class:`~selfloop.contracts.EffectStateUnknown`.

Bounded escalation
------------------

``LoopTool.max_attempts`` (3 below the approval floor, exactly 1 at T2+, hard
ceiling 10) bounds the recorded failures for ONE business key. When the budget is
spent the guard raises :class:`~selfloop.contracts.EffectAttemptsExhausted`
*without reaching the tool*. A permanently-failing effect escalates to a human;
it never hammers an external system once per tick forever.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import (
    MAX_ATTEMPTS_CEILING,
    EffectAttemptsExhausted,
    EffectStateUnknown,
    EffectUnavailable,
    EvidenceGrade,
    LoopError,
    LoopTool,
    RecordKind,
    Verification,
    coerce_verification,
    digest_key,
)
from selfloop.ledger import (
    RECEIPT_FAILED,
    RECEIPT_SUCCEEDED,
    RECEIPT_UNAVAILABLE,
    RECEIPT_UNKNOWN,
    ReceiptRecord,
    ReconciliationRecord,
    emit,
    write_cache,
    write_history,
)

#: The state of a row that has been claimed and carries no result. It is the
#: ABSENCE of a stored value, never a stored value itself — which is what makes
#: it survivable across a crash: nothing had to be written for the row to mean
#: "the fate of this effect was never established".
CLAIMED = "claimed"

#: Event channel for everything this module records. See
#: :class:`~selfloop.ledger.EventRecord`.
EFFECT_EVENT_KIND = "effect"

#: Marker and version of the outcome envelope stored in a row's ``result_json``.
#:
#: A completed row whose payload does not carry this marker was not written by
#: this module, and it is read as UNKNOWN rather than as a success. That is a
#: deliberate divergence from the system this was ported from, which read an
#: unmarked payload as ``succeeded`` because it had a production table full of
#: rows predating the envelope. This package ships with no such table: every row
#: in it was written here, so a payload that is not ours is corruption or a
#: foreign writer, and neither is a licence to report that an effect took effect.
ENVELOPE = "__selfloop_receipt__"
ENVELOPE_VERSION = 1

#: The ASCII unit separator, used to join the parts of a receipt key. It is
#: chosen because it cannot occur in a template, instance, node or tool name and
#: is vanishingly unlikely inside a business key — see :func:`receipt_key` for
#: what is enforced and why joining with an ordinary character was a collision.
_SEP = "\x1f"

#: An attempt suffix, for the one shape a business key may not end with. See
#: :func:`receipt_key`.
_ATTEMPT_SUFFIX_RE = re.compile(r"#a\d+$")


def receipt_key(
    instance_id: str, template: str, node: str, tool_name: str, business_key: str
) -> str:
    """The stable identity of one effect: template, instance, node, tool, key.

    Namespaced by template *as well as* instance, because two loops that collide
    in a shared receipt table do not merely confuse a report — one loop's
    completed receipt suppresses the other loop's effect entirely, which is a
    silent, permanent no-op that looks exactly like a loop with nothing to do.

    **The parts are joined with the ASCII unit separator, and that is a
    correctness fix rather than a formatting choice.** Joined with an ordinary
    character such as ``:``, a business key is free to contain the separator,
    and two different effects can compose the same key: node ``send`` with key
    ``a:b`` and node ``send:a`` with key ``b`` produce one string. The one that
    gets written first then suppresses the other, forever. Structural parts are
    refused if they contain the separator, so every boundary is unambiguous and
    the encoding is injective for any business key at all.

    The trailing *business_key* is deliberately NOT an argument digest. Only the
    caller knows which part of its arguments is the effect's identity: a health
    snapshot carries a timestamp, and digesting the whole payload would mint a
    fresh key every tick and re-run the effect forever. The digest that *is*
    enforced lives on the approval row (see
    :func:`selfloop.tools.effect_binding`), where changing an argument must
    invalidate a human's decision rather than duplicate an effect.

    One ergonomic consequence, stated so nobody has to discover it: the
    separator does not print. Every message in this module therefore renders a
    key with ``repr`` so the boundaries are visible as ``\\x1f`` escapes rather
    than as nothing at all, and an operator tool should let a human name a row
    by its PARTS — node, tool, business key, attempt — rather than by asking
    them to retype the composed string.
    """
    parts = {
        "instance_id": instance_id,
        "template": template,
        "node": node,
        "tool_name": tool_name,
    }
    for name, value in parts.items():
        if _SEP in value:
            raise ValueError(
                f"{name}={value!r} contains the unit separator that composes a receipt key; "
                "refusing to build a key whose boundaries are ambiguous, because two "
                "different effects could then compose the same key and one would "
                "permanently suppress the other"
            )
    if _ATTEMPT_SUFFIX_RE.search(business_key):
        raise ValueError(
            f"business_key={business_key!r} ends in an attempt suffix (#a<n>), which is "
            "how this module names retry rows; a key in that shape would collide with "
            f"another effect's second attempt. Rename it (for example {business_key}-x)."
        )
    return _SEP.join(("loop", template, instance_id, node, tool_name, business_key))


def attempt_key(key: str, attempt: int) -> str:
    """The row key for the *attempt*-th try at *key*.

    Attempt 1 keeps the base key unchanged, so a receipt names the effect and not
    a slot number, and ``#a<n>`` marks each retry. :func:`receipt_key` refuses a
    business key ending in that shape, which is what stops attempt 2 of one
    effect from being attempt 1 of another.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return key if attempt == 1 else f"{key}#a{attempt}"


@dataclass(frozen=True)
class ReceiptOutcome:
    """What :func:`guarded` returns: the recorded outcome of ONE attempt."""

    result: Any
    replayed: bool
    #: The row that authorises this outcome — the ATTEMPT key, not the base key,
    #: so the audit trail names a row that actually exists and attempt 2 of an
    #: incident is legible as such.
    key: str
    attempt: int = 1
    #: FALSE when the effect ran and did not take effect. The row says ``failed``
    #: and a later tick gets the next attempt slot. The tick that produced it
    #: must not render as a success — but that is the caller's decision to make,
    #: not this module's. See :func:`guarded`.
    succeeded: bool = True
    #: ``True``/``False`` when the tool declared a verification predicate,
    #: ``None`` when it did not. ``None`` is never to be read as "fine": an
    #: effect whose independent check did not happen has not been checked, and
    #: the learning pass mines exactly the disagreement between what a tool
    #: declared and what a verifier ruled.
    verified: bool | None = None
    detail: str = ""


@dataclass(frozen=True)
class _Recorded:
    """A completed row's envelope, decoded. See :func:`_recorded`."""

    state: str
    result: Any
    detail: str
    verified: bool | None
    attempt: int


def _stamp(ctx: LoopContext) -> str:
    """``Clock.now_iso`` for a record stamp, or ``""`` when the clock is unusable.

    A stamp on a receipt is documentation: no freshness or anti-forgery check
    reads it. Guarding it here means a clock that raises can never be the reason
    a completed effect fails to record its completion, which would turn a
    successful send into a permanently unknown one.
    """
    try:
        return ctx.clock.now_iso()
    except Exception:  # noqa: BLE001 - a stamp is documentation; see the docstring
        return ""


def _decode(raw: Any) -> Any:
    """Parse a stored ``result_json``. Junk comes back as itself, never as None."""
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def declared_failure(result: Any) -> str:
    """The reason *result* declares its OWN failure, or ``""``.

    A short, documented ladder over the conventions tools actually use. It is the
    WEAK half of the success test: it can only ever veto a success, never grant
    one, so a tool whose result says nothing about itself is judged by its
    verification predicate — or, if it declares none, trusted.

    That asymmetry is the whole reason this is a separate function with a
    separate name. Read it as "did the tool admit to failing?", never as "did the
    tool succeed?".
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


def _declared_success(result: Any) -> bool | None:
    """What the TOOL said about itself: ``True``, ``False``, or ``None`` for silence.

    ``None`` is a real answer and must not collapse into ``False``. A tool that
    returned a string, or a mapping with no status convention in it, made no
    claim at all — and a ledger that records silence as a denial invents evidence
    the learning pass will later mine.
    """
    if declared_failure(result):
        return False
    if not isinstance(result, Mapping):
        return None
    for flag in ("success", "ok"):
        if flag in result:
            return bool(result[flag])
    if result.get("returncode") == 0:
        return True
    return None


def _judge(
    tool: LoopTool, result: Any, args: Mapping[str, Any]
) -> tuple[Verification, bool | None]:
    """Did the effect take effect? The CONJUNCTION of both available signals.

    Monotone by construction: every signal can only add a reason to refuse, so
    declaring a verifier can only make success harder and can never make it
    easier. That property is what stops a weak verifier from laundering a
    self-declared failure into a completed receipt.

    A predicate that RAISES does not produce a failure verdict. The effect ran
    and its outcome could not be established, which is
    :class:`~selfloop.contracts.EffectStateUnknown` — the row is left claimed so
    the next tick fails closed, exactly as a crash between claim and completion
    does. Treating the raise as "it failed" would be the double-billing bug: a
    verifier whose own dependency is down would free the effect to run again.
    """
    reasons = [reason for reason in (declared_failure(result),) if reason]
    verified: bool | None = None

    if tool.verify is not None:
        try:
            verdict = coerce_verification(tool.verify(result, dict(args)))
        except Exception as exc:  # noqa: BLE001 - re-raised as UNKNOWN, see the docstring
            raise EffectStateUnknown(
                f"{tool.name}: the effect ran but its verification predicate raised "
                f"{type(exc).__name__}: {exc} — the outcome is unknown, so it will not "
                "be recorded as either a success or a failure"
            ) from exc
        verified = verdict.ok
        if not verdict.ok:
            reasons.append(f"verification failed: {verdict.detail or '<no detail>'}")

    return Verification(ok=not reasons, detail="; ".join(reasons)), verified


def _envelope(
    state: str, *, attempt: int, result: Any, verified: bool | None, detail: str
) -> str:
    """Serialise one attempt's terminal outcome. ``default=str`` never raises."""
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


def _recorded(raw: Any) -> _Recorded:
    """Decode a completed row. Anything unrecognisable is UNKNOWN, never success.

    Three ways a payload can fail to be one of ours — no envelope marker, an
    envelope carrying a state this version does not know, a value that is not
    JSON at all — and all three land on ``unknown``. It is tempting to read an
    unrecognised row as "somebody completed it, so it is done", and that reading
    is how a corrupt row becomes a reported success. Unknown is not success, and
    the way out of unknown is a human (:func:`reconcile`), not a guess.
    """
    decoded = _decode(raw)
    if isinstance(decoded, Mapping) and ENVELOPE in decoded:
        state = str(decoded.get("state") or "")
        if state not in (RECEIPT_SUCCEEDED, RECEIPT_FAILED, RECEIPT_UNAVAILABLE):
            state = RECEIPT_UNKNOWN
        verified = decoded.get("verified")
        return _Recorded(
            state=state,
            result=decoded.get("result"),
            detail=str(decoded.get("detail") or ""),
            verified=None if verified is None else bool(verified),
            attempt=int(decoded.get("attempt", 1) or 1),
        )
    return _Recorded(
        state=RECEIPT_UNKNOWN,
        result=decoded,
        detail="the receipt row carries a payload this package did not write",
        verified=None,
        attempt=1,
    )


def _mirror(
    ctx: LoopContext,
    *,
    key: str,
    node: str,
    tool: LoopTool,
    business_key: str,
    attempt: int,
    outcome: str,
    declared_success: bool | None,
    verified: bool | None,
    args: Mapping[str, Any],
    detail: str,
) -> None:
    """Copy one terminal envelope into the readable ledger. Never fails the effect.

    The receipt store is keyed by attempt key and exists to arbitrate a race; the
    ledger exists to be read, by reporting, by the CLI and by the learning pass.
    Mirroring means none of those readers can disturb the exactly-once
    bookkeeping just by looking at it.

    A mirror is a CACHE write, not history: it is a copy of a durable row that
    already exists elsewhere, so a fresher copy superseding a stale one is
    correct. And a failure to write it is folded into the event log rather than
    raised, because the effect has already happened and its authoritative record
    is already durable — turning a completed effect into an exception because a
    reporting copy did not land would report a failure that did not occur.
    """
    try:
        record = ReceiptRecord(
            id=key,
            at=_stamp(ctx),
            instance_id=ctx.instance_id,
            template=ctx.template,
            node=node,
            tool=tool.name,
            business_key=business_key,
            attempt=attempt,
            outcome=outcome,
            declared_success=declared_success,
            verified=verified,
            # The weakest grade that is certainly true. A declared verifier looked
            # at something the tool's return value did not decide; the seam cannot
            # see WHICH channel it consulted, so it does not claim a stronger one.
            evidence_grade=(
                EvidenceGrade.ACTOR_NARRATIVE if verified is None else EvidenceGrade.LOCAL_ARTIFACT
            ),
            args_digest=digest_key(dict(args)),
            tier=tool.tier,
            detail=detail,
        )
        write_cache(ctx, RecordKind.RECEIPT, key, record.as_dict())
    except Exception as exc:  # noqa: BLE001 - see the docstring
        emit(
            ctx,
            EFFECT_EVENT_KIND,
            "effect_mirror_failed",
            {"receipt": key, "error": f"{type(exc).__name__}: {exc}"},
            node=node,
        )


def guarded(
    ctx: LoopContext,
    *,
    key: str,
    node: str,
    tool: LoopTool,
    execute: Callable[[], Any],
    args: Mapping[str, Any] | None = None,
    business_key: str = "",
) -> ReceiptOutcome:
    """Run *execute* at most once PER SUCCESS for *key*.

    ``outcome.succeeded`` is the answer this module exists to give, and it is
    FALSE when the attempt ran and did not take effect: the row is recorded
    ``failed``, and a later tick gets the next attempt slot instead of replaying
    a failure forever.

    **A failed attempt returns rather than raises, on purpose.** The tick's
    STATUS is the caller's decision — the template's verify node is the thing
    that knows what "it did not work" means for its domain and can say so
    precisely — and a seam that raised would take that decision away from every
    template at once, skipping the very node whose job is to explain the failure.
    What this module owes a caller is an honest answer about the world, not a
    verdict about the tick.

    It raises in exactly three cases, none of which is a success:

    * :class:`~selfloop.contracts.EffectStateUnknown` — an earlier attempt was
      claimed and never completed (a crash between claim and act), a verification
      predicate raised, or a row carries a payload this package cannot read.
      Never re-run; fail closed. :func:`reconcile` is the way out.
    * :class:`~selfloop.contracts.EffectAttemptsExhausted` — every slot in this
      business key's budget holds a recorded failure. The tool is NOT reached and
      the tick escalates.
    * :class:`~selfloop.contracts.EffectUnavailable` — the effect's authority was
      never reached, so provably nothing happened. It propagates so the caller
      can settle the tick as a NEUTRAL non-result: absence is not failure.

    **Two ceilings, and they count different things.** ``tool.max_attempts``
    bounds recorded FAILURES, because a failure is evidence that repeating the
    call is unlikely to help. :data:`~selfloop.contracts.MAX_ATTEMPTS_CEILING`
    bounds ROWS, because an ``unavailable`` slot spends no budget and something
    has to stop a month-long outage from writing one row per tick forever. When
    the rows run out and none of them is a failure, this raises
    :class:`~selfloop.contracts.EffectUnavailable` rather than
    ``EffectAttemptsExhausted`` — manufacturing a failure verdict out of ten
    proofs that nothing happened is the mistake the whole taxonomy exists to
    prevent. A caller that wants an operator paged for a dependency that has been
    unreachable all day should raise
    :class:`~selfloop.contracts.BlockedLoopError`, which is adverse and reaches
    the acceptance floor; that judgement belongs to whoever knows what persistent
    absence means for the domain, not to this seam.
    """
    budget = tool.resolved_max_attempts()
    payload = dict(args or {})
    failures = 0
    absences = 0

    for attempt in range(1, MAX_ATTEMPTS_CEILING + 1):
        if failures >= budget:
            break
        outcome, spent = _attempt(
            ctx,
            base_key=key,
            attempt=attempt,
            budget=budget,
            node=node,
            tool=tool,
            execute=execute,
            args=payload,
            business_key=business_key,
        )
        if outcome is not None:
            return outcome
        if spent == RECEIPT_FAILED:
            failures += 1
        else:
            absences += 1

    if failures >= budget:
        emit(
            ctx,
            EFFECT_EVENT_KIND,
            "effect_exhausted",
            {"tool": tool.name, "receipt": key, "failures": failures, "budget": budget},
            node=node,
        )
        raise EffectAttemptsExhausted(
            f"{node}/{tool.name}: {failures} of {budget} allowed attempts at receipt {key!r} "
            "are recorded failures — escalating to a human instead of retrying"
        )

    emit(
        ctx,
        EFFECT_EVENT_KIND,
        "effect_slots_exhausted",
        {"tool": tool.name, "receipt": key, "absences": absences, "slots": MAX_ATTEMPTS_CEILING},
        node=node,
    )
    raise EffectUnavailable(
        "attempt_slots_exhausted",
        f"{node}/{tool.name}: all {MAX_ATTEMPTS_CEILING} attempt rows for receipt {key!r} "
        f"record that the effect's authority was never reached ({absences} absences). "
        "Nothing has happened and nothing has failed; the dependency has been "
        "unreachable for as long as this key has existed",
    )


def _attempt(
    ctx: LoopContext,
    *,
    base_key: str,
    attempt: int,
    budget: int,
    node: str,
    tool: LoopTool,
    execute: Callable[[], Any],
    args: Mapping[str, Any],
    business_key: str,
) -> tuple[ReceiptOutcome | None, str]:
    """One attempt slot.

    Returns ``(outcome, "")`` when this slot answered, or ``(None, spent)`` when
    the slot is already spent and the caller should move to the next one. *spent*
    is :data:`~selfloop.ledger.RECEIPT_FAILED` when the slot consumed a unit of
    the retry budget, or :data:`~selfloop.ledger.RECEIPT_UNAVAILABLE` when it did
    not — an attempt that provably never reached the effect's authority did not
    test anything, so charging it to the budget would let somebody else's outage
    spend this key's retries.
    """
    key = attempt_key(base_key, attempt)
    existing = ctx.receipts.get(key)

    if existing is not None:
        if existing.get("result_json") is not None:
            recorded = _recorded(existing.get("result_json"))
            if recorded.state == RECEIPT_FAILED:
                return None, RECEIPT_FAILED
            if recorded.state == RECEIPT_UNAVAILABLE:
                return None, RECEIPT_UNAVAILABLE
            if recorded.state == RECEIPT_SUCCEEDED:
                emit(
                    ctx,
                    EFFECT_EVENT_KIND,
                    "effect_replayed",
                    {"tool": tool.name, "receipt": key, "attempt": attempt},
                    node=node,
                )
                return (
                    ReceiptOutcome(
                        result=recorded.result,
                        replayed=True,
                        key=key,
                        attempt=attempt,
                        verified=recorded.verified,
                        detail=recorded.detail,
                    ),
                    "",
                )
            raise EffectStateUnknown(
                f"{node}/{tool.name}: receipt {key!r} is completed with a payload this "
                f"package cannot read ({recorded.detail}) — the effect may or may not "
                f"have taken effect, so it will not be re-run. To clear it: "
                f"{ctx.reconcile_hint}"
            )
        if not tool.replay_on_unknown:
            # Fails closed FOREVER, by design and with a known cost: nothing in
            # this process can ever learn whether that effect ran, so there is no
            # state transition it would be honest to make on its own. See the
            # module docstring on why a TTL here would be the double-billing bug
            # with a delay in front of it. The missing evidence lives at the
            # external system, so the recovery path is a human — and it is named
            # here, because a loud failure that does not say how to clear it is
            # only half loud.
            raise EffectStateUnknown(
                f"{node}/{tool.name}: receipt {key!r} was claimed but never completed — "
                "the external effect may already have happened, so it will not be "
                "re-run. This fails every tick until a human reconciles it: "
                f"{ctx.reconcile_hint}"
            )

    claimed = ctx.receipts.claim(key, instance_id=ctx.instance_id, node=node, at=_stamp(ctx))
    if not claimed and existing is None:
        # Another worker claimed this key between our read and our insert. Two
        # processes racing one irreversible effect is exactly the case a receipt
        # exists to stop, so the loser fails closed rather than guessing which of
        # them is about to act.
        raise EffectStateUnknown(
            f"{node}/{tool.name}: receipt {key!r} was claimed concurrently by another "
            "worker; refusing to run an effect a peer may be running right now"
        )

    try:
        result = execute()
    except EffectUnavailable as exc:
        _record_unavailable(
            ctx,
            key=key,
            node=node,
            tool=tool,
            business_key=business_key,
            attempt=attempt,
            args=args,
            exc=exc,
        )
        raise

    verdict, verified = _judge(tool, result, args)
    state = RECEIPT_SUCCEEDED if verdict.ok else RECEIPT_FAILED
    ctx.receipts.complete(
        key,
        envelope_json=_envelope(
            state, attempt=attempt, result=result, verified=verified, detail=verdict.detail
        ),
        at=_stamp(ctx),
    )
    _mirror(
        ctx,
        key=key,
        node=node,
        tool=tool,
        business_key=business_key,
        attempt=attempt,
        outcome=state,
        declared_success=_declared_success(result),
        verified=verified,
        args=args,
        detail=verdict.detail,
    )

    if verdict.ok:
        emit(
            ctx,
            EFFECT_EVENT_KIND,
            "effect_executed",
            {
                "tool": tool.name,
                "receipt": key,
                "tier": tool.tier.name,
                "attempt": attempt,
                "verified": verified,
            },
            node=node,
        )
        return (
            ReceiptOutcome(
                result=result, replayed=False, key=key, attempt=attempt, verified=verified
            ),
            "",
        )

    # The effect ran and did not take effect. Record THAT. Filing exactly this as
    # a done effect is the defect this module was rewritten to close.
    emit(
        ctx,
        EFFECT_EVENT_KIND,
        "effect_failed",
        {
            "tool": tool.name,
            "receipt": key,
            "tier": tool.tier.name,
            "attempt": attempt,
            "attempts_allowed": budget,
            "verified": verified,
            "detail": verdict.detail,
        },
        node=node,
    )
    remaining = max(0, budget - attempt)
    return (
        ReceiptOutcome(
            result=result,
            replayed=False,
            key=key,
            attempt=attempt,
            succeeded=False,
            verified=verified,
            detail=(
                f"attempt {attempt}/{budget} did not take effect ({verdict.detail}); "
                f"{remaining} attempt(s) left for receipt {base_key!r}"
            ),
        ),
        "",
    )


def _record_unavailable(
    ctx: LoopContext,
    *,
    key: str,
    node: str,
    tool: LoopTool,
    business_key: str,
    attempt: int,
    args: Mapping[str, Any],
    exc: EffectUnavailable,
) -> None:
    """Complete a claim with a terminal ``unavailable`` envelope. ONE durable write.

    This diverges from the design this module was ported from, and the divergence
    is the point. The original RELEASED the claim — deleted the row — so that an
    outage would not spend the business key's retry budget. But releasing is a
    **second** store call, and a process that dies between the claim and the
    release leaves the row claimed with no result, which is exactly the state
    that bricks the business key with
    :class:`~selfloop.contracts.EffectStateUnknown` forever. The release existed
    to make an outage harmless and it opened a window in which an outage was
    permanently harmful.

    So the row is COMPLETED instead, with a terminal ``unavailable`` outcome. One
    write, no window. The attempt loop knows that an ``unavailable`` slot frees
    the next slot without consuming budget, so the property the release was for
    is preserved — an outage costs a row, not a retry.

    If the completion itself fails, the original exception still propagates and
    the row stays claimed. That is the honest outcome: nothing left this process,
    but this process also failed to write down that nothing left it, so the next
    tick will fail closed and ask a human. It is recorded loudly rather than
    swallowed.
    """
    reason = getattr(exc, "reason", "unavailable")
    detail = str(getattr(exc, "detail", "") or exc)[:500]
    recorded = True
    try:
        ctx.receipts.complete(
            key,
            envelope_json=_envelope(
                RECEIPT_UNAVAILABLE,
                attempt=attempt,
                result=None,
                verified=None,
                detail=f"{reason}: {detail}",
            ),
            at=_stamp(ctx),
        )
    except Exception:  # noqa: BLE001 - the original EffectUnavailable is the real answer
        recorded = False
    else:
        _mirror(
            ctx,
            key=key,
            node=node,
            tool=tool,
            business_key=business_key,
            attempt=attempt,
            outcome=RECEIPT_UNAVAILABLE,
            declared_success=None,
            verified=None,
            args=args,
            detail=f"{reason}: {detail}",
        )
    emit(
        ctx,
        EFFECT_EVENT_KIND,
        "effect_unavailable",
        {
            "tool": tool.name,
            "receipt": key,
            "attempt": attempt,
            "reason": reason,
            "detail": detail,
            "outcome_recorded": recorded,
        },
        node=node,
    )


def receipt_state(ctx: LoopContext, key: str, attempt: int = 1) -> str:
    """``absent`` / ``claimed`` / ``succeeded`` / ``failed`` / ``unavailable`` / ``unknown``.

    A read, for a CLI, a drill or a test. It writes nothing and it never repairs
    anything it finds.
    """
    row = ctx.receipts.get(attempt_key(key, attempt))
    if row is None:
        return "absent"
    if row.get("result_json") is None:
        return CLAIMED
    return _recorded(row.get("result_json")).state


def receipt_exists(ctx: LoopContext, key: str) -> bool:
    """True when a SUCCEEDED receipt exists for *key* in any attempt slot.

    A failed attempt is emphatically not "a receipt exists". That reading — any
    row means done — is what suppressed a whole incident window's worth of
    retries in the system this was ported from.
    """
    return any(
        receipt_state(ctx, key, attempt) == RECEIPT_SUCCEEDED
        for attempt in range(1, MAX_ATTEMPTS_CEILING + 1)
    )


def reconcile(
    ctx: LoopContext,
    key: str,
    *,
    outcome: str,
    by: str,
    note: str = "",
) -> ReconciliationRecord:
    """The audited way out of a fail-closed unknown. A human declares what happened.

    :class:`~selfloop.contracts.EffectStateUnknown` is permanent by design: the
    runtime will not re-execute an effect that may already have happened, and no
    timer changes that, because a timer observes nothing. The only thing that
    changes it is a person who went and looked at the external system. This
    function is that person's statement, and it does two things in a deliberate
    order:

    1. writes a :class:`~selfloop.ledger.ReconciliationRecord` as HISTORY, so the
       escape hatch is itself audited — an unknown that was cleared should be as
       visible as the unknown that caused it; then
    2. completes the stuck receipt row with the declared outcome, which is what
       actually unsticks the business key.

    Record first, because a crash between the two leaves an audit record for an
    escape that did not take — recoverable by re-running this, and loud. The
    other order leaves an unaudited escape, which is not recoverable at all.

    *key* is the ATTEMPT key, exactly as it appears in the ``EffectStateUnknown``
    message. *outcome* must be ``succeeded`` or ``failed``: "probably fine" is
    the state we are already in, and a third value would only launder an unknown
    into a settled row. *by* must be a human — the record refuses an automation
    identity, and because the loop can only ever write ``loop:<instance>``, it
    cannot clear its own unknowns.

    Refuses when the row is absent (there is nothing to reconcile) and when the
    row is already completed (a reconciliation may not overwrite a durable
    outcome; that is rewriting history, not establishing it).

    The arguments are validated before the row is looked at, so a caller who
    passed an undecided outcome or an automation identity is told *that* rather
    than being told about the row. The two refusals are about different mistakes
    and the message should name the one the caller actually made.
    """
    record = ReconciliationRecord(
        id=f"rec_{digest_key('reconciliation', key)[:20]}",
        at=_stamp(ctx),
        receipt_key=key,
        instance_id=ctx.instance_id,
        template=ctx.template,
        outcome=outcome,
        by=by,
        note=note,
    )

    row = ctx.receipts.get(key)
    if row is None:
        raise LoopError(
            f"cannot reconcile receipt {key!r}: no such row. A reconciliation settles a "
            "claim that exists; inventing one would assert that an effect was attempted "
            "when nothing recorded that it was."
        )
    if row.get("result_json") is not None:
        state = _recorded(row.get("result_json")).state
        raise LoopError(
            f"cannot reconcile receipt {key!r}: it already records {state!r}. A "
            "reconciliation establishes an outcome that was never established; it does "
            "not overwrite one that was."
        )

    fresh = write_history(ctx, RecordKind.RECONCILIATION, record.id, record.as_dict())
    if not fresh:
        # A previous reconciliation of this row was recorded and its completion
        # did not land — the exact crash this ordering exists to make
        # recoverable. The existing record stays authoritative and is not
        # rewritten; what is retried is the completion below.
        emit(
            ctx,
            EFFECT_EVENT_KIND,
            "effect_reconcile_retried",
            {"receipt": key, "record": record.id},
        )

    ctx.receipts.complete(
        key,
        envelope_json=_envelope(
            outcome,
            attempt=1,
            result=None,
            verified=None,
            detail=f"reconciled by {by}: {note}" if note else f"reconciled by {by}",
        ),
        at=_stamp(ctx),
    )
    emit(
        ctx,
        EFFECT_EVENT_KIND,
        "effect_reconciled",
        {"receipt": key, "outcome": outcome, "by": by, "record": record.id},
    )
    return record


__all__ = [
    "CLAIMED",
    "EFFECT_EVENT_KIND",
    "ENVELOPE",
    "ENVELOPE_VERSION",
    "ReceiptOutcome",
    "attempt_key",
    "declared_failure",
    "guarded",
    "receipt_exists",
    "receipt_key",
    "receipt_state",
    "reconcile",
]
