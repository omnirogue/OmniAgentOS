"""Template: draft -> approve -> send.

The outbound shape, and the strictest one. ``send`` is a T2+ effect, so it
ALWAYS parks: there is no params flag, no tier argument and no config key that
makes this template send without a human decision. The counterfeit for this
template deletes the gate; the send count must still be zero, which holds
because ``tools.execute_effect`` re-reads the approvals row itself.

Instance contract:
  draft T0  (subject, context) -> dict   the message to be sent
  send  T2+ (draft) -> Any               the outbound effect
"""

from __future__ import annotations

from typing import Any

from omniagentos_loops.contracts import LoopState, LoopStatus
from omniagentos_loops.runtime import LoopTemplate
from omniagentos_loops.templates.common import PARK, add_effect, add_read, stopped
from omniagentos_loops.tools import digest_key

NAME = "draft_approve_send"


def _draft(state: LoopState) -> dict[str, Any]:
    return dict((state.get("data") or {}).get("draft") or {})


def _send_key(state: LoopState) -> str:
    """Business key = the recipient + the content digest.

    Content-bound on purpose: re-sending the SAME message to the same address is
    a duplicate (blocked by the receipt), while a revised message is a genuinely
    new effect that needs its own approval.
    """
    draft = _draft(state)
    return digest_key(draft.get("to"), draft.get("subject"), draft.get("body"))


def _merge(state: LoopState, **values: Any) -> dict[str, Any]:
    return {"data": {**(state.get("data") or {}), **values}}


def build(ctx: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LoopState)

    add_read(
        graph,
        ctx,
        name="draft",
        tool="draft",
        args_fn=lambda s: {
            "subject": (s.get("params") or {}).get("subject", ""),
            "context": (s.get("data") or {}).get("context")
            or (s.get("params") or {}).get("context")
            or {},
        },
        on_result=lambda s, r: _merge(s, draft=dict(r or {})),
    )
    gate = add_effect(
        graph,
        ctx,
        name="send",
        tool="send",
        args_fn=lambda s: {"draft": _draft(s)},
        key_fn=_send_key,
        ttl_hours=None,
        on_result=lambda s, r: {**_merge(s, sent=r), "status": LoopStatus.COMPLETED.value},
    )

    def after_draft(state: LoopState) -> str:
        if stopped(state):
            return PARK
        return gate if _draft(state) else "nothing_to_send"

    def nothing_to_send(state: LoopState) -> dict[str, Any]:
        return {"status": LoopStatus.IDLE.value, "log": ["no draft produced"]}

    graph.add_node("nothing_to_send", nothing_to_send)
    graph.add_edge(START, "draft")
    graph.add_conditional_edges(
        "draft", after_draft, {gate: gate, "nothing_to_send": "nothing_to_send", PARK: PARK}
    )
    graph.add_edge("send", END)
    graph.add_edge("nothing_to_send", END)
    return graph


TEMPLATE = LoopTemplate(
    name=NAME,
    family="outbound",
    required_tools=("draft", "send"),
    build=build,
    description="draft -> human approval -> send (every send is T2+)",
)
