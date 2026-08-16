"""The node kit every template is assembled from.

The single most important thing here is :func:`add_effect`: it is impossible to
attach an effect node to a graph without also attaching its ``policy_gate``
node, because ``add_effect`` adds both and wires the only path into the effect
through the gate. Requirement 5 ("policy_gate before EVERY effect node") is
therefore a structural property of the builder, not a convention templates are
trusted to follow — and the execution seam re-checks anyway (tools.execute_effect).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omniagentos_loops.approvals import deep_link, ensure_approval, read_outcome
from omniagentos_loops.contracts import (
    EffectDenied,
    EffectNotApproved,
    EffectStateUnknown,
    EffectUnavailable,
    LoopState,
    LoopStatus,
    UnknownToolError,
)
from omniagentos_loops.observability import LoopEvents, emit, observed
from omniagentos_loops.policy_gate import preview
from omniagentos_loops.retry import retry_kwargs
from omniagentos_loops.tools import effect_binding, execute_effect

#: Terminal node every template shares: nothing ran, or something refused.
PARK = "park"

#: ``state["data"]`` key listing the effect nodes that RAN this tick and did not
#: take effect. Written by :func:`add_effect` when the seam reports
#: ``succeeded=False`` and a judge node was named, and read by
#: :func:`verification_outcome`, so delegating the verdict to a verify node
#: cannot lose the failure: a judge that ignores the effect's result still
#: cannot render the tick favourable.
FAILED_EFFECTS = "failed_effects"

ArgsFn = Callable[[LoopState], Mapping[str, Any]]
KeyFn = Callable[[LoopState], str]
ResultFn = Callable[[LoopState, Any], dict[str, Any]]

#: Statuses that must divert a template away from its happy path.
_STOP_STATUSES = frozenset({LoopStatus.ABORTED.value, LoopStatus.FAILED.value})

#: ``state["data"]`` key naming the effect nodes whose authority could not be
#: REACHED this tick. Distinct from :data:`FAILED_EFFECTS` on purpose: a failed
#: effect was judged, an unavailable one was not, and collapsing the two is the
#: absence-scored-as-failure defect.
UNAVAILABLE_EFFECTS = "unavailable_effects"

#: Statuses that divert to ``park`` WITHOUT being adverse. ``IDLE`` is the
#: taxonomy's neutral non-result; an effect whose authority was unreachable
#: stamps it and must not continue into a verify node that would then grade the
#: missing effect as a failure. Kept separate from :data:`_STOP_STATUSES` so
#: nothing reads "diverted" as "adverse".
_DIVERT_STATUSES = _STOP_STATUSES | {LoopStatus.IDLE.value}


def ensure_park(graph: Any, ctx: Any) -> str:
    """Add the shared terminal ``park`` node once per graph."""
    from langgraph.graph import END

    if PARK in getattr(graph, "nodes", {}):
        return PARK

    def _park(state: LoopState) -> dict[str, Any]:
        emit(
            ctx,
            LoopEvents.STATUS,
            "loop.park",
            {"status": state.get("status"), "error": state.get("error")},
        )
        return {}

    graph.add_node(PARK, observed(ctx, PARK, _park))
    graph.add_edge(PARK, END)
    return PARK


def add_effect(
    graph: Any,
    ctx: Any,
    *,
    name: str,
    tool: str,
    args_fn: ArgsFn,
    key_fn: KeyFn,
    on_result: ResultFn | None = None,
    ttl_hours: int | None = None,
    retries: int = 1,
    judged_by: str | None = None,
) -> str:
    """Add ``<name>_gate`` -> ``<name>`` and return the GATE node's name.

    Callers wire *into* the returned gate name and out of ``name``; there is no
    edge that reaches ``name`` without traversing the gate.

    **A failed effect never continues down the happy path.** The execution seam
    returns ``succeeded=False`` when the effect RAN and did not take effect (its
    own result declared failure, or its ``verify=`` predicate looked at the world
    and said no). Ignoring that — which this builder did — means a template with
    no verify node routes a failed effect straight into its terminal node and
    stamps whatever status lives there.

    *judged_by* is how a template says "a node downstream renders the verdict for
    this effect": name it, and routing continues so that node can (its result is
    in ``data`` for it to read). The DEFAULT is ``None``, which parks the tick —
    loudness is the behaviour a template gets for not thinking about it, because
    the next loop someone writes will not have a verify node.
    """
    ensure_park(graph, ctx)
    gate_name = f"{name}_gate"

    registered = ctx.tools.tools.get(tool)
    if retries > 1 and registered is not None and not registered.replay_on_unknown:
        raise ValueError(
            f"effect {name!r}: retries>1 requires tool {tool!r} to declare "
            "replay_on_unknown — a claimed-but-uncompleted receipt must never be "
            "blind-retried (P7)"
        )

    def _gate(state: LoopState) -> dict[str, Any]:
        args = args_fn(state)
        business_key = key_fn(state)
        verdict = preview(ctx, tool, args)
        gates = dict(state.get("gates") or {})

        if verdict.decision == "deny":
            gates[name] = {"decision": "deny", "business_key": business_key}
            return {
                "gates": gates,
                "status": LoopStatus.ABORTED.value,
                "error": f"policy denied {name}/{tool}: {verdict.reason}",
                "log": [f"{name}: denied"],
            }

        if verdict.decision == "allow":
            gates[name] = {"decision": "allow", "business_key": business_key}
            return {"gates": gates, "log": [f"{name}: allowed ({verdict.tier.name})"]}

        # T2+ / policy-mandated approval: one durable row, then park.
        # Everything above this line re-executes when the thread resumes
        # (P2 caveat), which is exactly why ensure_approval is idempotent.
        from langgraph.types import interrupt

        tool_obj = ctx.tools.get(tool)
        row = ensure_approval(
            ctx,
            node=name,
            tool=tool_obj,
            args=args,
            business_key=business_key,
            verdict=verdict,
            ttl_hours=ttl_hours,
        )
        # A LATER tick re-enters this node from START with the row already
        # decided (or expired). Parking again would leave an approved effect
        # stuck forever, so the durable row — not the interrupt — is the
        # authority; interrupt() is only reached while a human still owes an
        # answer.
        settled = read_outcome(
            ctx, row["id"], binding=effect_binding(ctx, node=name, tool=tool_obj, args=args)
        )
        if settled.terminal:
            outcome: Any = settled.as_resume()
        else:
            outcome = interrupt(
                {
                    "approval_id": row["id"],
                    "loop_instance": ctx.instance_id,
                    "node": name,
                    "tool": tool,
                    "tier": verdict.tier.name,
                    "action_class": verdict.action_class.value,
                    "business_key": business_key,
                    "proposed_action": row.get("proposed_action"),
                    "expires_at": row.get("expires_at"),
                    "deep_link": deep_link(ctx, row["id"]),
                }
            )
        decided = outcome if isinstance(outcome, Mapping) else {}
        if not decided.get("approved"):
            gates[name] = {"decision": "refused", "business_key": business_key}
            return {
                "gates": gates,
                "status": LoopStatus.ABORTED.value,
                "error": (
                    f"{name}/{tool}: approval {row['id']} "
                    f"{decided.get('state', 'undecided')} — {decided.get('reason', 'no decision')}"
                ),
                "log": [f"{name}: approval not granted"],
            }
        gates[name] = {
            "decision": "approved",
            "approval_id": row["id"],
            "business_key": business_key,
            "decided_by": decided.get("decided_by", ""),
        }
        return {"gates": gates, "log": [f"{name}: approved by {decided.get('decided_by', '')}"]}

    def _effect(state: LoopState) -> dict[str, Any]:
        args = args_fn(state)
        business_key = key_fn(state)
        gates = state.get("gates") or {}
        try:
            tool_obj = ctx.tools.get(tool)
            outcome = execute_effect(
                ctx,
                node=name,
                tool=tool_obj,
                args=args,
                business_key=business_key,
                gate_token=gates.get(name),
            )
        except EffectUnavailable as exc:
            # ABSENCE, and the one branch here that is NOT adverse. The effect's
            # authority was never reached, so nothing was attempted and nothing
            # about this loop was judged. Scoring it against the acceptance
            # floor is the 2026-07-31 defect ("we could not ask" read as "the
            # answer was no"), so the tick stands down as IDLE — NEUTRAL, out
            # of the denominator — and says so loudly, in the event stream, in
            # the run's detail, and in ``data[UNAVAILABLE_EFFECTS]``.
            emit(
                ctx,
                LoopEvents.EFFECT,
                "loop.effect.unavailable",
                {
                    "node": name,
                    "tool": tool,
                    "business_key": business_key,
                    "reason": getattr(exc, "reason", "unavailable"),
                    "detail": str(getattr(exc, "detail", "") or exc)[:500],
                },
            )
            data = dict(state.get("data") or {})
            data[UNAVAILABLE_EFFECTS] = [*(data.get(UNAVAILABLE_EFFECTS) or []), name]
            return {
                "status": LoopStatus.IDLE.value,
                # Carried on ``error`` because that is what ``runtime._report``
                # turns into the tick's detail and the routine row's notes. A
                # neutral non-result still has to be READABLE, or "loud" is a
                # word in a docstring rather than a property.
                "error": (
                    f"unavailable: {name}/{tool} could not reach its authority "
                    f"({getattr(exc, 'reason', 'unavailable')}) — nothing was attempted"
                ),
                "data": data,
                "log": [f"{name}: unavailable — authority not reached"],
            }
        except (
            EffectDenied,
            EffectNotApproved,
            EffectStateUnknown,
            UnknownToolError,
        ) as exc:
            emit(
                ctx,
                LoopEvents.EFFECT,
                "loop.effect.refused",
                {"node": name, "tool": tool, "error": f"{type(exc).__name__}: {exc}"},
            )
            return {
                "status": LoopStatus.ABORTED.value,
                "error": f"{type(exc).__name__}: {exc}",
                "log": [f"{name}: refused at the execution seam"],
            }

        # ``succeeded`` is absent on a runtime whose seam does not report an
        # outcome yet; absent is "no information", not "it failed".
        succeeded = outcome.get("succeeded", True)
        detail = str(outcome.get("detail") or "")
        update: dict[str, Any] = {
            "effects": [
                {
                    "node": name,
                    "tool": tool,
                    "business_key": business_key,
                    "receipt": outcome["receipt"],
                    "replayed": outcome["replayed"],
                    "succeeded": bool(succeeded),
                }
            ],
            "log": [f"{name}: {'replayed' if outcome['replayed'] else 'executed'}"],
        }
        if on_result is not None:
            update.update(on_result(state, outcome["result"]))
        if not succeeded:
            # AFTER on_result deliberately: a terminal status stamped by the
            # happy path (COMPLETED on a send, IDLE on an escalation) must not
            # outrank evidence that the effect did not take effect.
            update["log"] = [f"{name}: ran and did NOT take effect — {detail or 'no detail'}"]
            if judged_by is None:
                update["status"] = LoopStatus.FAILED.value
                update["error"] = f"{name}/{tool}: {detail or 'the effect did not take effect'}"
            else:
                # Delegation, not amnesty: the named judge decides HOW to say it,
                # and verification_outcome refuses to render favourable while
                # this list is non-empty.
                data = dict(update.get("data") or state.get("data") or {})
                data[FAILED_EFFECTS] = [*(data.get(FAILED_EFFECTS) or []), name]
                update["data"] = data
        return update

    graph.add_node(gate_name, observed(ctx, gate_name, _gate))
    graph.add_node(name, observed(ctx, name, _effect), **retry_kwargs(retries))
    graph.add_conditional_edges(
        gate_name,
        _gate_router(name),
        {name: name, PARK: PARK},
    )
    return gate_name


def _gate_router(name: str) -> Callable[[LoopState], str]:
    def route(state: LoopState) -> str:
        gate = (state.get("gates") or {}).get(name) or {}
        return name if gate.get("decision") in ("allow", "approved") else PARK

    return route


def add_status_route(graph: Any, node: str, ok: str) -> None:
    """Continue to *ok* unless the node stopped or stood down; then divert to park.

    ``IDLE`` diverts as well as ``ABORTED``/``FAILED`` (see
    :data:`_DIVERT_STATUSES`). The two reasons are different and both matter: a
    stopped tick must not run its happy path, and a tick that stood down because
    an effect's authority was unreachable must not walk into a verify node that
    would grade the never-attempted effect as a failure — which would convert
    absence into an adverse verdict at the last step.
    """
    ensure_park_exists(graph)

    def route(state: LoopState) -> str:
        return PARK if str(state.get("status")) in _DIVERT_STATUSES else ok

    graph.add_conditional_edges(node, route, {ok: ok, PARK: PARK})


def ensure_park_exists(graph: Any) -> None:
    if PARK not in getattr(graph, "nodes", {}):
        raise ValueError("add_effect()/ensure_park() must run before add_status_route()")


def add_step(
    graph: Any,
    ctx: Any,
    *,
    name: str,
    fn: Callable[[LoopState], dict[str, Any]],
    retries: int = 3,
) -> str:
    """Add a NON-effect node (pure computation) with transient retries."""
    graph.add_node(name, observed(ctx, name, fn), **retry_kwargs(retries))
    return name


def add_read(
    graph: Any,
    ctx: Any,
    *,
    name: str,
    tool: str,
    args_fn: ArgsFn,
    on_result: ResultFn | None = None,
    retries: int = 3,
) -> str:
    """Add a READ node: policy-checked and registry-resolved, but not receipted.

    A read is not an effect, so it gets no idempotency receipt (a poll MUST
    re-read every tick — a receipt would make the second tick a no-op). It still
    goes through ``execute_effect`` in ``mode="read"``, so policy is derived and
    the registry consulted on exactly the same code path an effect takes: an
    ungranted, denied or mis-tiered tool is never called. This is the P4
    property applied to the cheap half of a loop, and it is why there is no
    second, policy-free door into the tool plane.
    """

    def _read(state: LoopState) -> dict[str, Any]:
        args = args_fn(state)
        try:
            tool_obj = ctx.tools.get(tool)
            outcome = execute_effect(
                ctx,
                node=name,
                tool=tool_obj,
                args=args,
                business_key="",
                gate_token=None,
                mode="read",
            )
        except (EffectDenied, UnknownToolError) as exc:
            emit(
                ctx,
                LoopEvents.EFFECT,
                "loop.read.refused",
                {"node": name, "tool": tool, "error": f"{type(exc).__name__}: {exc}"},
            )
            return {
                "status": LoopStatus.ABORTED.value,
                "error": f"read {name}/{tool} refused: {exc}",
                "log": [f"{name}: refused"],
            }
        update: dict[str, Any] = {"log": [f"{name}: read"]}
        if on_result is not None:
            update.update(on_result(state, outcome["result"]))
        return update

    graph.add_node(name, observed(ctx, name, _read), **retry_kwargs(retries))
    return name


def stopped(state: LoopState) -> bool:
    """True when a node has already decided this tick must not continue."""
    return str(state.get("status")) in _STOP_STATUSES


def _is_verified(result: Any) -> bool:
    """Only an explicit ``verified: true`` counts. See :func:`verification_outcome`."""
    return isinstance(result, Mapping) and result.get("verified") is True


def verification_outcome(
    result: Any, *, node: str = "verify", state: LoopState | None = None
) -> dict[str, Any]:
    """Turn a verify tool's verdict into the TICK's status. Fail-closed.

    A verify node exists for one reason: to prove the effect this tick performed
    actually happened. Its verdict is therefore the tick's status — the fact
    that the node *ran* is not. Stamping ``COMPLETED`` unconditionally (what
    both verify-bearing templates did until now) is why a live W3 tick whose
    repair returned ``{"verified": false, "state": "repair_failed"}`` still
    settled as ``completed, accepted=1, gate_passed=1``: the loop detected its
    own failure and graded it a pass.

    The contract is deliberately strict and one-directional: **only an explicit
    ``verified: true`` is a success.** A missing key, a non-mapping, an error
    dict, an enum drift — every one of those is an ABSENCE of a verdict, and an
    absent verdict must never render as the most favourable outcome (R7).

    *state* carries the second, independent veto: an effect that the seam
    reported as ``succeeded=False`` (:data:`FAILED_EFFECTS`). A template that
    routes a failed effect here rather than parking has DELEGATED the wording,
    not the verdict — a verify tool that ignores the effect's result cannot
    launder it, which is what keeps the failed-effect guarantee structural
    instead of per-instance.
    """
    unresolved = sorted(set(((state or {}).get("data") or {}).get(FAILED_EFFECTS) or ()))
    verified = _is_verified(result)

    if verified and unresolved:
        # The contradiction, named: the seam has evidence the effect did not
        # take, and the node asked to render that reported success anyway.
        return {
            "status": LoopStatus.FAILED.value,
            "error": (
                f"{node}: effect(s) {unresolved} ran without taking effect "
                f"({node} claimed verified)"
            ),
        }
    if verified:
        return {"status": LoopStatus.COMPLETED.value}

    if isinstance(result, Mapping):
        detail = str(result.get("state") or result.get("error") or "verified is not true")
    else:
        detail = f"verify returned {type(result).__name__}, not a verdict mapping"
    if unresolved:
        # Both signals agree. Keep the judge's WORDS — "repair_failed" and
        # "still_failing" are different incidents to an operator — and add the
        # seam's fact rather than replacing one with the other.
        detail = f"{detail} (effect(s) {unresolved} did not take effect)"
    return {
        "status": LoopStatus.FAILED.value,
        "error": f"{node}: unverified — {detail}",
    }


__all__ = [
    "FAILED_EFFECTS",
    "PARK",
    "UNAVAILABLE_EFFECTS",
    "add_effect",
    "add_read",
    "add_status_route",
    "add_step",
    "ensure_park",
    "ensure_park_exists",
    "stopped",
    "verification_outcome",
]
