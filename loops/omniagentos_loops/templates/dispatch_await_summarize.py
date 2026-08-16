"""Template: dispatch -> await -> summarize. Wraps the swarm, never replaces it.

MIGRATION_ARCHITECTURE.md: *a loop that needs heavy coding work does not become
a graph of coders — its dispatch node POSTs to intake and a later poll node
watches the board card. The swarm remains the muscle; loops are the nervous
system.*

Awaiting is done ACROSS ticks, not by blocking one: an unfinished card ends the
tick IDLE, and the next tick re-enters ``dispatch``, where the idempotency
receipt replays the original dispatch (returning the same card ref) instead of
submitting a second job. That is the receipt machinery earning its keep on the
happy path, not only after a crash.

Instance contract:
  dispatch  T2  (spec) -> dict   must return a stable {"ref": ...}
  poll_card T0  (ref) -> dict    {"done": bool, "outcome": ...}
  summarize T1+ (ref, outcome) -> Any
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
)
from omniagentos_loops.tools import digest_key

NAME = "dispatch_await_summarize"
DEFAULT_MAX_WAIT_TICKS = 48


def _memo(state: LoopState) -> dict[str, Any]:
    return dict(state.get("memo") or {})


def _spec(state: LoopState) -> dict[str, Any]:
    return dict((state.get("params") or {}).get("spec") or {})


def _ref(state: LoopState) -> Any:
    return _memo(state).get("ref")


def _dispatch_key(state: LoopState) -> str:
    """One dispatch per (instance, spec). Re-entering on a later tick replays."""
    return digest_key(_spec(state))


def build(ctx: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LoopState)

    def _record_dispatch(state: LoopState, result: Any) -> dict[str, Any]:
        payload = dict(result or {}) if isinstance(result, dict) else {"ref": result}
        memo = _memo(state)
        return {
            "memo": {
                **memo,
                "ref": payload.get("ref", payload),
                "waited": int(memo.get("waited") or 0) + 1,
            }
        }

    gate = add_effect(
        graph,
        ctx,
        name="dispatch",
        tool="dispatch",
        args_fn=lambda s: {"spec": _spec(s)},
        key_fn=_dispatch_key,
        on_result=_record_dispatch,
    )
    add_read(
        graph,
        ctx,
        name="await_card",
        tool="poll_card",
        args_fn=lambda s: {"ref": _ref(s)},
        on_result=lambda s, r: {"data": {**(s.get("data") or {}), "card": dict(r or {})}},
    )
    summarize_gate = add_effect(
        graph,
        ctx,
        name="summarize",
        tool="summarize",
        args_fn=lambda s: {
            "ref": _ref(s),
            "outcome": ((s.get("data") or {}).get("card") or {}).get("outcome"),
        },
        key_fn=lambda s: digest_key(_ref(s), "summary"),
        on_result=lambda s, r: {
            "data": {**(s.get("data") or {}), "summary": r},
            "memo": {},  # the job is closed: release the cross-tick handle
            "status": LoopStatus.COMPLETED.value,
        },
    )

    def after_await(state: LoopState) -> str:
        if stopped(state):
            return PARK
        card = (state.get("data") or {}).get("card") or {}
        if card.get("done"):
            return summarize_gate
        max_ticks = int((state.get("params") or {}).get("max_wait_ticks", DEFAULT_MAX_WAIT_TICKS))
        return "gave_up" if int(_memo(state).get("waited") or 0) >= max_ticks else "waiting"

    def waiting(state: LoopState) -> dict[str, Any]:
        return {
            "status": LoopStatus.IDLE.value,
            "log": [f"awaiting {_ref(state)} (tick {_memo(state).get('waited')})"],
        }

    def gave_up(state: LoopState) -> dict[str, Any]:
        return {
            "status": LoopStatus.ABORTED.value,
            "error": f"dispatched work {_ref(state)!r} did not finish within the wait budget",
            "memo": {},
        }

    graph.add_node("waiting", waiting)
    graph.add_node("gave_up", gave_up)
    graph.add_edge(START, gate)
    add_status_route(graph, "dispatch", "await_card")
    graph.add_conditional_edges(
        "await_card",
        after_await,
        {
            summarize_gate: summarize_gate,
            "waiting": "waiting",
            "gave_up": "gave_up",
            PARK: PARK,
        },
    )
    graph.add_edge("summarize", END)
    graph.add_edge("waiting", END)
    graph.add_edge("gave_up", END)
    return graph


TEMPLATE = LoopTemplate(
    name=NAME,
    family="dispatch",
    required_tools=("dispatch", "poll_card", "summarize"),
    build=build,
    description="dispatch to the swarm -> await the board card across ticks -> summarize",
)
