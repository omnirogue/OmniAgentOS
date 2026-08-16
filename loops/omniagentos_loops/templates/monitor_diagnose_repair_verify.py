"""Template: monitor -> diagnose -> repair -> verify.

The self-heal shape. Its safety property is the remediation allowlist: only
diagnoses whose remedy is a member of ``params["allowed_remedies"]`` may reach
the repair effect at T1 (auto). Anything else is escalated to a T3 effect, which
by construction parks for a human. The allowlist is data supplied per instance,
but the *rule* — unknown remedy never auto-executes — is code here.

Instance contract:
  monitor  T0  (params) -> dict                health snapshot
  diagnose T0  (snapshot) -> dict              the DIAGNOSIS (see below)
  repair   T1  (remedy, diagnosis) -> Any      allowlisted auto-remediation
  escalate T3  (remedy, diagnosis) -> Any      everything else (always parks)
  verify   T0  (remedy, result) -> dict        post-conditions; MUST return
                                               ``{"verified": bool, ...}``

The diagnosis is the ONE object the effect nodes act on, and two rules make that
work. Both were learned from live failures on this template:

1. **The effect gets the diagnosis, not the snapshot.** ``repair`` was wired
   ``{"remedy", "snapshot"}`` while W3's repair tool read the label out of
   ``snapshot["diagnosis"]`` — a key the monitor never produces — so every
   allowlisted repair failed the allowlist check with ``label None``,
   unconditionally, forever. A tool must be handed what it consumes; there is
   no shape here for a reader to guess at.
2. **The effect's arguments must be a FUNCTION OF ITS BUSINESS KEY.** The args
   digest is written into the approvals row at creation and re-checked by the
   execution seam when the human's decision comes back a tick later
   (``approvals.read_outcome(binding=...)``). A monitor snapshot carries
   ``timestamp`` and log tails that change every tick, so passing it made the
   digest drift between the tick that ASKED and the tick that RESUMED: the row
   is keyed by (remedy, incident) and cannot be re-minted, so an approved
   escalation was refused as "bound to a different action" — permanently.
   Diagnoses must therefore be stable for as long as their incident id is.
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

NAME = "monitor_diagnose_repair_verify"


def _snapshot(state: LoopState) -> dict[str, Any]:
    return dict((state.get("data") or {}).get("snapshot") or {})


def _diagnosis(state: LoopState) -> dict[str, Any]:
    return dict((state.get("data") or {}).get("diagnosis") or {})


def _remedy(state: LoopState) -> str:
    return str(_diagnosis(state).get("remedy") or "")


def _allowed(state: LoopState) -> frozenset[str]:
    return frozenset(str(x) for x in (state.get("params") or {}).get("allowed_remedies") or ())


def _repair_key(state: LoopState) -> str:
    """Business key = (remedy, incident). Two ticks of the SAME incident are one
    repair; a new incident id is a new receipt and may act again."""
    diagnosis = _diagnosis(state)
    return f"{_remedy(state)}:{diagnosis.get('incident') or diagnosis.get('since') or 'now'}"


def _merge(state: LoopState, **values: Any) -> dict[str, Any]:
    return {"data": {**(state.get("data") or {}), **values}}


def build(ctx: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LoopState)

    add_read(
        graph,
        ctx,
        name="monitor",
        tool="monitor",
        args_fn=lambda s: {"params": s.get("params") or {}},
        on_result=lambda s, r: _merge(s, snapshot=dict(r or {})),
    )
    add_read(
        graph,
        ctx,
        name="diagnose",
        tool="diagnose",
        args_fn=lambda s: {"snapshot": _snapshot(s)},
        on_result=lambda s, r: _merge(s, diagnosis=dict(r or {})),
    )
    repair_gate = add_effect(
        graph,
        ctx,
        name="repair",
        tool="repair",
        args_fn=lambda s: {"remedy": _remedy(s), "diagnosis": _diagnosis(s)},
        key_fn=_repair_key,
        on_result=lambda s, r: _merge(s, repair_result=r),
        # A repair that did not take effect still routes to ``verify``: that node
        # reads the repair result and is the thing that knows what "it did not
        # work" means here (repair_failed vs still_failing). Parking at the
        # effect instead would skip the tick's own verdict. ``escalate`` below
        # takes the default — nothing downstream judges it, so a failed
        # escalation parks rather than rendering its neutral IDLE.
        judged_by="verify",
    )
    # An executed escalation is a NON-RESULT: the tick behaved — it recorded and
    # paged a failure it is not allowed to fix — and nothing in the world got
    # better. So it is IDLE, which the scheduler's taxonomy classes as NEUTRAL
    # (``loop_jobs.classify_loop_status``: NEUTRAL_STATUSES = {parked, idle},
    # excluded from the acceptance denominator entirely). COMPLETED would re-
    # inflate the very metric that taxonomy exists to deflate — a fleet that
    # stays broken while the loop reports a healthy rate. PARKED is not the
    # right neutral either: it means an interrupt is still outstanding and
    # carries an approval id, which stops being true the moment the effect runs.
    #
    # Before this, the node stamped no status at all and the tick fell off the
    # end of the graph carrying the state's initial ``running`` — which that
    # same classifier reads as ``loop_status_unrecognized``, i.e. ADVERSE.
    escalate_gate = add_effect(
        graph,
        ctx,
        name="escalate",
        tool="escalate",
        args_fn=lambda s: {"remedy": _remedy(s), "diagnosis": _diagnosis(s)},
        key_fn=_repair_key,
        on_result=lambda s, r: {
            **_merge(s, escalation=r),
            "status": LoopStatus.IDLE.value,
        },
    )
    add_read(
        graph,
        ctx,
        name="verify",
        tool="verify",
        args_fn=lambda s: {
            "remedy": _remedy(s),
            "result": (s.get("data") or {}).get("repair_result"),
        },
        on_result=lambda s, r: {**_merge(s, verification=r), **verification_outcome(r, state=s)},
    )

    def after_diagnose(state: LoopState) -> str:
        if stopped(state):
            return PARK
        remedy = _remedy(state)
        if not remedy:
            return "healthy"
        return repair_gate if remedy in _allowed(state) else escalate_gate

    def healthy(state: LoopState) -> dict[str, Any]:
        return {"status": LoopStatus.IDLE.value, "log": ["healthy: no remedy needed"]}

    graph.add_node("healthy", healthy)
    graph.add_edge(START, "monitor")
    add_status_route(graph, "monitor", "diagnose")
    graph.add_conditional_edges(
        "diagnose",
        after_diagnose,
        {repair_gate: repair_gate, escalate_gate: escalate_gate, "healthy": "healthy", PARK: PARK},
    )
    add_status_route(graph, "repair", "verify")
    graph.add_edge("escalate", END)
    graph.add_edge("verify", END)
    graph.add_edge("healthy", END)
    return graph


TEMPLATE = LoopTemplate(
    name=NAME,
    family="monitor",
    required_tools=("monitor", "diagnose", "repair", "escalate", "verify"),
    build=build,
    description="monitor -> diagnose -> repair (allowlist) | escalate -> verify",
)
