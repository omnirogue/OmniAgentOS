"""Template: observe -> decide -> act -> verify. The safety workhorse.

The shape for "something arrived; work out what it is; do one thing about it;
confirm it happened". **One subject per tick, by design.** A tick that acts on
exactly one business object has exactly one approval, one receipt and one
verifiable outcome, which is what makes the kill drill and the counterfeit
assertions mean anything: "did this effect happen exactly once?" is a question
you can answer about one effect and cannot answer about a batch.

Read this template as the reference for the gate/receipt/park spine. The graph is
five nodes and two terminals, and every safety property in the package is visible
in it: the act node is unreachable except through ``act_gate``; the gate parks
for a human at T2+ whatever the policy adapter said; the act node's receipt makes
a crash-resume unable to duplicate the effect; and the verify node's verdict —
not the fact that it ran — is the tick's status.

The instance contract, i.e. the tools this template's instance must grant:

===========  =====  ===========================================================
tool         tier   signature
===========  =====  ===========================================================
``observe``  T0     ``(params) -> list[dict]`` — subjects awaiting handling
``decide``   T0     ``(subject) -> dict`` — must carry ``"action"``
``act``      T1+    ``(subject, decision) -> Any`` — the effect; T2+ parks
``verify``   T0     ``(subject, decision, result) -> dict`` — must carry
                    ``"verified": bool``; its verdict IS the tick's status
===========  =====  ===========================================================

OBSERVE FAULTS — say WHICH kind, because they score differently
---------------------------------------------------------------

* **Transient** (network blip, 5xx, rate limit, lock contention): return ``[]``
  or raise :class:`~selfloop.contracts.TransientLoopError`. The tick routes to
  ``idle``, which is NEUTRAL — out of the acceptance denominator entirely, and
  incapable of pausing the loop.
* **Persistent authorization failure** (401/403, revoked grant, dead refresh
  token): raise ``BlockedLoopError(detail, cause="authorization")``. The runtime
  settles the tick as ``BLOCKED``, which is ADVERSE and trips the acceptance
  floor.

**A dead credential must raise, never return ``[]``.** This is the rule the
source system's own loop instances violated, and it is why
:class:`~selfloop.contracts.BlockedLoopError` exists in the vocabulary rather
than in a template. An empty list renders identically to an empty inbox, so a
loop whose credentials were revoked idles green forever, reports nothing wrong,
and nobody is told — for as long as it takes somebody to notice by hand that a
queue stopped draining. ``BlockedLoopError`` is deliberately absent from the
executor's transient-retry allowlist, so it propagates on the first attempt
instead of burning three retries on a credential that will never work.

The same distinction applies at the other end of the tick. An
:class:`~selfloop.contracts.EffectUnavailable` from ``act`` — provably nothing
left the process — stands the tick down as ``IDLE`` rather than failing it, and
:func:`~selfloop.kit.add_status_route` diverts it away from ``verify``, because a
verify tool asked to grade an effect that was never attempted will correctly find
nothing and incorrectly call it a failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import LoopState, LoopStatus, digest_key
from selfloop.engine import END, CompiledGraph, Graph
from selfloop.kit import (
    PARK,
    add_effect,
    add_read,
    add_status_route,
    add_step,
    ensure_park,
    merge_data,
    stopped,
    verification_outcome,
)
from selfloop.templates import LoopTemplate

NAME = "observe_decide_act_verify"

OBSERVE = "observe"
DECIDE = "decide"
ACT = "act"
VERIFY = "verify"
IDLE = "idle"

#: Actions that mean "there is nothing to do about this subject". Matched
#: case-insensitively. The empty string is here on purpose: a ``decide`` tool
#: that returns ``{}`` has not chosen an action, and treating an absent choice as
#: a licence to act is the fail-open direction.
SKIP_ACTIONS = frozenset({"skip", "ignore", "none", ""})


def _mapping(value: Any) -> dict[str, Any]:
    """A tool result as a dict, or an empty one. Never raises on a surprising shape."""
    return dict(value) if isinstance(value, Mapping) else {}


def _subject(state: LoopState) -> dict[str, Any]:
    """The one subject this tick handles: the head of what ``observe`` returned."""
    subjects = (state.get("data") or {}).get("subjects") or []
    return _mapping(subjects[0]) if subjects else {}


def _decision(state: LoopState) -> dict[str, Any]:
    return _mapping((state.get("data") or {}).get("decision"))


def _subject_key(state: LoopState) -> str:
    """The business key of the act: the stable identity of the subject.

    The subject's own id when it has one, and otherwise a digest of its content.
    The kit this was ported from fell back to the literal string ``"item"``,
    which is worse than it looks: every subject with no id field shares one
    business key, so the second one is refused by the first one's receipt and the
    loop silently stops handling anything unidentified. A content digest is
    stable for the same subject and distinct for a different one, which is what a
    business key has to be.

    Note what is NOT in the key: the decision. The identity of the act is "this
    subject was acted on", so re-deciding a subject differently must not buy a
    second attempt. Changed arguments still re-park, because the approval id
    digests the arguments — a human approves an action, never a slot.
    """
    subject = _subject(state)
    explicit = subject.get("id") or subject.get("key")
    return str(explicit) if explicit else digest_key(NAME, subject)


def _verdict(state: LoopState, result: Any) -> dict[str, Any]:
    """Render ``verify``'s answer as the tick's status, and keep the answer itself.

    :func:`~selfloop.kit.verification_outcome` owns the verdict — including the
    veto that a non-empty ``FAILED_EFFECTS`` list applies to a claimed success —
    and returns its own ``data`` update carrying the failure tag. This adds the
    raw verification result on top of that mapping rather than beside it, because
    ``data`` has replace semantics in the executor and returning two ``data``
    keys means one of them is silently discarded.
    """
    verdict = dict(verification_outcome(result, node=VERIFY, state=state))
    carried = verdict.get("data") or state.get("data") or {}
    verdict["data"] = {**carried, "verification": _mapping(result)}
    return verdict


def build(ctx: LoopContext) -> CompiledGraph:
    """Assemble and compile this template's graph for one loop instance."""
    graph = Graph()
    ensure_park(graph, ctx)

    add_read(
        graph,
        ctx,
        name=OBSERVE,
        tool=OBSERVE,
        args_fn=lambda s: {"params": dict(s.get("params") or {})},
        on_result=lambda s, r: merge_data(s, subjects=list(r or [])),
    )
    add_read(
        graph,
        ctx,
        name=DECIDE,
        tool=DECIDE,
        args_fn=lambda s: {"subject": _subject(s)},
        on_result=lambda s, r: merge_data(s, decision=_mapping(r)),
    )
    gate = add_effect(
        graph,
        ctx,
        name=ACT,
        tool=ACT,
        args_fn=lambda s: {"subject": _subject(s), "decision": _decision(s)},
        key_fn=_subject_key,
        on_result=lambda s, r: merge_data(s, act_result=r),
        # ``verify`` reads ``act_result`` and words this tick's verdict, so an act
        # that did not take effect must still REACH it rather than stopping at
        # the effect node. The delegation is safe because verification_outcome
        # refuses to render favourable while data[FAILED_EFFECTS] is non-empty:
        # a verify tool that ignores the act's result cannot launder it.
        judged_by=VERIFY,
    )
    add_read(
        graph,
        ctx,
        name=VERIFY,
        tool=VERIFY,
        args_fn=lambda s: {
            "subject": _subject(s),
            "decision": _decision(s),
            "result": (s.get("data") or {}).get("act_result"),
        },
        on_result=_verdict,
    )

    def _idle(state: LoopState) -> dict[str, Any]:
        return {"status": LoopStatus.IDLE.value, "log": ["nothing to do this tick"]}

    add_step(graph, ctx, name=IDLE, fn=_idle)

    def after_observe(state: LoopState) -> str:
        if stopped(state):
            return PARK
        return DECIDE if _subject(state) else IDLE

    def after_decide(state: LoopState) -> str:
        if stopped(state):
            return PARK
        action = str(_decision(state).get("action", "")).strip().lower()
        return IDLE if action in SKIP_ACTIONS else gate

    graph.set_entry(OBSERVE)
    graph.add_conditional_edges(
        OBSERVE, after_observe, {DECIDE: DECIDE, IDLE: IDLE, PARK: PARK}
    )
    graph.add_conditional_edges(DECIDE, after_decide, {gate: gate, IDLE: IDLE, PARK: PARK})
    add_status_route(graph, ACT, VERIFY)
    graph.add_edge(VERIFY, END)
    graph.add_edge(IDLE, END)
    # No per-template step ceiling: this graph is acyclic, so it terminates in at
    # most seven nodes and the context's own max_steps is already a bound on it.
    # A ceiling here would be a second number to keep in step with the graph for
    # no gain.
    return graph.compile()


TEMPLATE = LoopTemplate(
    name=NAME,
    family="observe_act",
    required_tools=(OBSERVE, DECIDE, ACT, VERIFY),
    build=build,
    description="observe -> decide -> act -> verify, one subject per tick",
)

__all__ = [
    "ACT",
    "DECIDE",
    "IDLE",
    "NAME",
    "OBSERVE",
    "SKIP_ACTIONS",
    "TEMPLATE",
    "VERIFY",
    "build",
]
