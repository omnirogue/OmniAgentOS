"""Template: poll -> classify -> act -> verify.

The workhorse shape for "something arrived; decide what it is; do one thing
about it; confirm it happened". One item per tick by design: a tick that acts on
exactly one business object has exactly one approval, one receipt and one
verifiable outcome, which is what makes the kill-drill and the counterfeit
assertions meaningful.

Instance contract (tools the loop instance must register):
  poll     T0  () -> list[dict]        items awaiting handling, newest last
  classify T0  (item) -> dict          must contain "action"; "skip" means no-op
  act      T1+ (item, decision) -> Any the effect; T2+ parks for a human
  verify   T0  (item, decision, result) -> dict  post-conditions

POLL FAULTS — say WHICH kind, because they score differently:

* Transient (network blip, 5xx, rate limit): return ``[]`` or raise
  ``TransientLoopError``. The tick routes to ``idle``, which is NEUTRAL — it is
  excluded from the acceptance denominator and cannot pause the routine.
* Persistent authorization failure (401/403, revoked grant, dead refresh
  token): raise ``BlockedLoopError(detail, cause="authorization")``. The
  runtime reports ``BLOCKED``, which is ADVERSE and trips the acceptance floor.

Returning ``[]`` for a dead credential is the failure this distinction exists to
prevent: it renders identically to an empty inbox, so a loop with revoked
credentials idles green forever and nobody is told. ``BlockedLoopError`` is
deliberately NOT in ``retry.TRANSIENT_EXCEPTIONS``, so it propagates on the
first attempt rather than burning three retries on a credential that will never
work.
  verify   T0  (item, decision, result) -> dict  post-conditions; MUST return
                                      ``{"verified": bool, ...}`` — its verdict
                                      IS the tick's status (see
                                      ``common.verification_outcome``)
"""

from __future__ import annotations

from typing import Any

from omniagentos_loops.contracts import LoopState, LoopStatus
from omniagentos_loops.runtime import LoopTemplate
from omniagentos_loops.templates.common import (
    PARK,
    add_effect,
    add_read,
    add_status_route,
    stopped,
    verification_outcome,
)

NAME = "poll_classify_act_verify"
SKIP_ACTIONS = frozenset({"skip", "ignore", "none", ""})


def _head(state: LoopState) -> dict[str, Any]:
    items = (state.get("data") or {}).get("items") or []
    return dict(items[0]) if items else {}


def _decision(state: LoopState) -> dict[str, Any]:
    return dict((state.get("data") or {}).get("decision") or {})


def _item_key(state: LoopState) -> str:
    item = _head(state)
    return str(item.get("id") or item.get("key") or "item")


def _merge(state: LoopState, **values: Any) -> dict[str, Any]:
    return {"data": {**(state.get("data") or {}), **values}}


def build(ctx: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LoopState)

    add_read(
        graph,
        ctx,
        name="poll",
        tool="poll",
        args_fn=lambda s: {"params": s.get("params") or {}},
        on_result=lambda s, r: _merge(s, items=list(r or [])),
    )
    add_read(
        graph,
        ctx,
        name="classify",
        tool="classify",
        args_fn=lambda s: {"item": _head(s)},
        on_result=lambda s, r: _merge(s, decision=dict(r or {})),
    )
    gate = add_effect(
        graph,
        ctx,
        name="act",
        tool="act",
        args_fn=lambda s: {"item": _head(s), "decision": _decision(s)},
        key_fn=_item_key,
        on_result=lambda s, r: _merge(s, act_result=r),
        # ``verify`` reads ``act_result`` and renders this tick's verdict, so an
        # act that did not take effect must still reach it rather than parking
        # at the effect node.
        judged_by="verify",
    )
    add_read(
        graph,
        ctx,
        name="verify",
        tool="verify",
        args_fn=lambda s: {
            "item": _head(s),
            "decision": _decision(s),
            "result": (s.get("data") or {}).get("act_result"),
        },
        on_result=lambda s, r: {**_merge(s, verification=r), **verification_outcome(r, state=s)},
    )

    def after_poll(state: LoopState) -> str:
        if stopped(state):
            return PARK
        return "classify" if _head(state) else "idle"

    def after_classify(state: LoopState) -> str:
        if stopped(state):
            return PARK
        return "idle" if str(_decision(state).get("action", "")).lower() in SKIP_ACTIONS else gate

    def idle(state: LoopState) -> dict[str, Any]:
        return {"status": LoopStatus.IDLE.value, "log": ["nothing to do"]}

    graph.add_node("idle", idle)
    graph.add_edge(START, "poll")
    graph.add_conditional_edges(
        "poll", after_poll, {"classify": "classify", "idle": "idle", PARK: PARK}
    )
    graph.add_conditional_edges(
        "classify", after_classify, {gate: gate, "idle": "idle", PARK: PARK}
    )
    add_status_route(graph, "act", "verify")
    graph.add_edge("verify", END)
    graph.add_edge("idle", END)
    return graph


TEMPLATE = LoopTemplate(
    name=NAME,
    family="poll_act",
    required_tools=("poll", "classify", "act", "verify"),
    build=build,
    description="poll -> classify -> act -> verify (one item per tick)",
)
