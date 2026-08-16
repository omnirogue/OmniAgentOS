"""Template: generate -> evaluate -> improve (bounded), then publish.

The refinement cycle, with the hard stop that makes it safe to run unattended:
``params["max_rounds"]`` (default 3). LangGraph will happily cycle forever; the
bound is the loop's own hard cap, mirroring the routines engine's
``hard_cap_type``. When the bound is hit without reaching the score threshold
the tick ends ABORTED — it does not publish a draft that never passed its gate.

Instance contract:
  generate T0  (brief, previous, feedback) -> dict  candidate
  evaluate T0  (candidate, brief) -> dict           must contain "score" (0..1)
  publish  T1+ (candidate, evaluation) -> Any       the effect
"""

from __future__ import annotations

from typing import Any

from omniagentos_loops.contracts import LoopState, LoopStatus
from omniagentos_loops.runtime import LoopTemplate
from omniagentos_loops.templates.common import PARK, add_effect, add_read, stopped
from omniagentos_loops.tools import digest_key

NAME = "generate_evaluate_improve"
DEFAULT_MAX_ROUNDS = 3
DEFAULT_THRESHOLD = 0.8


def _data(state: LoopState) -> dict[str, Any]:
    return dict(state.get("data") or {})


def _merge(state: LoopState, **values: Any) -> dict[str, Any]:
    return {"data": {**_data(state), **values}}


def _score(state: LoopState) -> float:
    try:
        return float((_data(state).get("evaluation") or {}).get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _publish_key(state: LoopState) -> str:
    return digest_key((state.get("params") or {}).get("brief"), _data(state).get("candidate"))


def build(ctx: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LoopState)

    add_read(
        graph,
        ctx,
        name="generate",
        tool="generate",
        args_fn=lambda s: {
            "brief": (s.get("params") or {}).get("brief", ""),
            "previous": _data(s).get("candidate"),
            "feedback": (_data(s).get("evaluation") or {}).get("feedback", ""),
        },
        on_result=lambda s, r: _merge(s, candidate=r, rounds=int(_data(s).get("rounds") or 0) + 1),
    )
    add_read(
        graph,
        ctx,
        name="evaluate",
        tool="evaluate",
        args_fn=lambda s: {
            "candidate": _data(s).get("candidate"),
            "brief": (s.get("params") or {}).get("brief", ""),
        },
        on_result=lambda s, r: _merge(s, evaluation=dict(r or {})),
    )
    gate = add_effect(
        graph,
        ctx,
        name="publish",
        tool="publish",
        args_fn=lambda s: {
            "candidate": _data(s).get("candidate"),
            "evaluation": _data(s).get("evaluation") or {},
        },
        key_fn=_publish_key,
        on_result=lambda s, r: {**_merge(s, published=r), "status": LoopStatus.COMPLETED.value},
    )

    def after_evaluate(state: LoopState) -> str:
        if stopped(state):
            return PARK
        params = state.get("params") or {}
        threshold = float(params.get("score_threshold", DEFAULT_THRESHOLD))
        max_rounds = int(params.get("max_rounds", DEFAULT_MAX_ROUNDS))
        if _score(state) >= threshold:
            return gate
        if int(_data(state).get("rounds") or 0) >= max_rounds:
            return "exhausted"
        return "generate"

    def exhausted(state: LoopState) -> dict[str, Any]:
        return {
            "status": LoopStatus.ABORTED.value,
            "error": (
                f"score {_score(state):.3f} still below threshold after "
                f"{_data(state).get('rounds')} rounds — not publishing"
            ),
            "log": ["hard cap reached"],
        }

    graph.add_node("exhausted", exhausted)
    graph.add_edge(START, "generate")
    graph.add_conditional_edges(
        "generate",
        lambda s: PARK if stopped(s) else "evaluate",
        {"evaluate": "evaluate", PARK: PARK},
    )
    graph.add_conditional_edges(
        "evaluate",
        after_evaluate,
        {gate: gate, "generate": "generate", "exhausted": "exhausted", PARK: PARK},
    )
    graph.add_edge("publish", END)
    graph.add_edge("exhausted", END)
    return graph


TEMPLATE = LoopTemplate(
    name=NAME,
    family="refine",
    required_tools=("generate", "evaluate", "publish"),
    build=build,
    description="generate -> evaluate -> improve, bounded by max_rounds, then publish",
)
