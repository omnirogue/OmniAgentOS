"""The node kit: the builder that makes "a gate precedes every effect" structural.

Every template in this package is assembled out of these seven functions, and
the single most important property of the whole file is :func:`add_effect`. It
adds ``<name>_gate`` **and** ``<name>`` together, returns the *gate's* name, and
wires the only conditional edge into the effect out of its gate. A template
author therefore cannot attach an ungated effect to a graph: not by forgetting,
not by refactoring, not by copying a neighbouring template that happened to be
wrong. "A policy gate precedes every effect" stops being a convention that a
reviewer has to re-check on every pull request and becomes a shape the builder
can only produce.

That is a real distinction and not a stylistic one. The system this package was
extracted from stated the same rule in a docstring and enforced it by review;
the review caught it every time it was violated except the times it did not.

Three other rules live here because they are the ones a template author gets
wrong under time pressure, and each is enforced rather than documented:

* **A failed effect never continues down the happy path.** The execution seam
  reports ``succeeded=False`` when an effect RAN and did not take effect — its
  own result declared failure, or its ``verify=`` predicate went and looked and
  said no. With no ``judged_by`` this stamps ``FAILED``; with one it records the
  node in ``data[FAILED_EFFECTS]`` and lets the named judge choose the wording.
  That is delegation, not amnesty: :func:`verification_outcome` refuses to render
  a tick favourable while that list is non-empty, so a judge that ignores the
  effect's result still cannot launder it.
* **Absence is not failure.** An effect whose authority was never reached
  (:class:`~selfloop.contracts.EffectUnavailable`) stands the tick down as
  ``IDLE``, which is neutral, and :func:`add_status_route` diverts on ``IDLE`` as
  well as on the adverse statuses — so a never-attempted effect is never walked
  into a verify node that would grade it as a failure.
* **An absent verdict is not a success.** :func:`verification_outcome` renders
  ``COMPLETED`` only for an explicit ``verified is True``.

This module is also the one place in the package that raises
:class:`~selfloop.engine.ParkRequested` and the one place that calls
:func:`selfloop.learn.lesson_block`. Both are pinned by AST tests, and both have
a counterfeit entry that removes them and requires a named test to go red. A
second raise site would be a second park protocol nobody wrote down; a second
injection site would mean the feedback edge is no longer auditable in one line.

``learn`` never imports this module. The dependency runs one way only —
``kit -> learn`` — which is what keeps the learning pass owned by
``runtime.run_once`` and out of the graph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from selfloop.approvals import deep_link, ensure_approval, read_outcome
from selfloop.context import LoopContext
from selfloop.contracts import (
    ADVERSE_STATUSES,
    EffectDenied,
    EffectNotApproved,
    EffectStateUnknown,
    EffectUnavailable,
    LoopError,
    LoopState,
    LoopStatus,
    UnknownToolError,
)
from selfloop.engine import END, Graph, ParkRequested
from selfloop.learn import lesson_block
from selfloop.ledger import emit
from selfloop.policy import preview
from selfloop.receipts import EFFECT_EVENT_KIND
from selfloop.tools import effect_binding, execute_effect

# ---------------------------------------------------------------------------
# Node names and state keys shared by every template
# ---------------------------------------------------------------------------

#: The shared terminal node: where a tick that stopped, stood down, or was
#: refused goes to end legibly.
#:
#: **It is not** :attr:`~selfloop.contracts.LoopStatus.PARKED`. That status means
#: "waiting for a human" and it is produced by :class:`ParkRequested`, which
#: never reaches a router at all — the executor catches it and returns. A tick
#: that arrives at this node has already stamped its own terminal status
#: (``ABORTED``, ``FAILED``, ``BLOCKED`` or ``IDLE``); the node itself stamps
#: nothing, so a template that routes here without deciding anything fails closed
#: to ``FAILED`` in the executor rather than inheriting a status from the kit.
PARK = "park"

#: ``state["data"]`` key listing the effect nodes that RAN this tick and did not
#: take effect. Written by :func:`add_effect` when the seam reports
#: ``succeeded=False`` *and* a judge node was named; read by
#: :func:`verification_outcome`, which will not render a favourable verdict while
#: it is non-empty.
FAILED_EFFECTS = "failed_effects"

#: ``state["data"]`` key naming the effect nodes whose authority could not be
#: REACHED this tick. Kept distinct from :data:`FAILED_EFFECTS` on purpose: a
#: failed effect was judged, an unavailable one was never attempted, and
#: collapsing the two is the absence-scored-as-failure defect that auto-paused
#: four production routines in one night.
UNAVAILABLE_EFFECTS = "unavailable_effects"

#: ``state["data"]`` / ``state["params"]`` key naming this tick's LEARNING SCOPE
#: — the partition that clustering, promotion tiers and attribution all work
#: within. Read by the runtime when it composes the run's outcome record.
SCOPE = "scope"

#: ``state["data"]`` key carrying a structured tag for WHAT went wrong.
#:
#: This is not decoration. Clustering partitions by ``(scope, failure_tag)``
#: *before* it compares a single token, so an adverse run with no tag yields no
#: signal at all — the loop notices its own failure, records it honestly, and
#: learns nothing from it. Every adverse branch in this module stamps one.
FAILURE_TAG = "failure_tag"

#: ``state["params"]`` key carrying the id of the invocation that is running.
#:
#: Minted by the runtime before the tick and stamped into params, because
#: :func:`inject_lessons` has to record "this lesson was in this run" BEFORE the
#: run produces an outcome — which is the whole reason attribution is an
#: attribution and not a correlation over history.
RUN_ID = "run_id"

# ---------------------------------------------------------------------------
# Run-level failure tags stamped by this module
# ---------------------------------------------------------------------------

#: The seam refused: policy denied, no valid approval, the attempt budget was
#: spent, the state of a previous attempt is unknown, or the tool is not granted.
#: Nothing was executed.
TAG_EFFECT_REFUSED = "effect_refused"

#: The effect ran and did not take effect, and no judge node was named.
TAG_EFFECT_DID_NOT_TAKE = "effect_did_not_take"

#: A read was refused before its tool was reached.
TAG_READ_REFUSED = "read_refused"

#: A verify node did not return an explicit ``verified: true``.
TAG_UNVERIFIED = "effect_unverified"

#: Event channel for the kit's own graph-level events, kept off
#: :data:`~selfloop.receipts.EFFECT_EVENT_KIND` on purpose. The effect channel is
#: where an operator looks for tool activity, and "this tick stood down" is not
#: tool activity — filing it there means every search for what a loop DID has to
#: filter out the times it did nothing.
LOOP_EVENT_KIND = "loop"

# ---------------------------------------------------------------------------
# Routing sets
# ---------------------------------------------------------------------------

#: Statuses meaning "a node has already decided this tick must not continue".
#:
#: Derived from :data:`~selfloop.contracts.ADVERSE_STATUSES` rather than written
#: out, so it cannot drift from the taxonomy the acceptance floor uses. That
#: matters most for ``BLOCKED``, which a hand-maintained list would forget: a
#: tick that cannot proceed because a credential was revoked must not walk into
#: the rest of its happy path.
_STOP_STATUSES: frozenset[str] = frozenset(status.value for status in ADVERSE_STATUSES)

#: Statuses that divert to :data:`PARK`. ``IDLE`` is here and is NOT adverse: it
#: is the taxonomy's neutral non-result, and an effect whose authority was
#: unreachable stamps it. Diverting it keeps a never-attempted effect out of a
#: verify node that would otherwise grade the missing effect as a failure, which
#: would convert an absence into an adverse verdict at the very last step.
_DIVERT_STATUSES: frozenset[str] = _STOP_STATUSES | {LoopStatus.IDLE.value}

#: ``(state) -> arguments for this node's tool``.
ArgsFn = Callable[[LoopState], Mapping[str, Any]]
#: ``(state) -> the business key of the effect about to happen``. The stable
#: identity of the act ("message 42 answered"), never a random id.
KeyFn = Callable[[LoopState], str]
#: ``(state, result) -> channel updates``. Called with what the tool returned.
ResultFn = Callable[[LoopState, Any], Mapping[str, Any]]
#: A plain computation node.
StepFn = Callable[[LoopState], Mapping[str, Any] | None]


# ---------------------------------------------------------------------------
# Reading and writing the shared state
# ---------------------------------------------------------------------------


def merge_data(state: LoopState, **values: Any) -> dict[str, Any]:
    """Channel update that ADDS *values* to ``data`` without dropping what is there.

    ``data`` has replace semantics in the executor's reducer table — a node's
    update wins outright — which is correct for per-tick scratch and is a trap
    for a node that means to add one key. Returning ``{"data": {"items": ...}}``
    from the second read node of a template silently deletes what the first one
    put there, and the symptom appears three nodes later as a missing key.
    """
    return {"data": {**(state.get("data") or {}), **values}}


def _tagged(state: LoopState, tag: str) -> dict[str, Any]:
    """The ``data`` mapping with :data:`FAILURE_TAG` stamped. See :data:`FAILURE_TAG`."""
    return {**(state.get("data") or {}), FAILURE_TAG: tag}


def scope_of(state: LoopState) -> str:
    """This tick's learning scope: ``data``, then ``params``, then the template name.

    A node may narrow the scope it is working in, an instance may declare one in
    its params, and a template that does neither is scoped by its own name. The
    last fallback is deliberately not the empty string: an unscoped run
    contributes to no scope's baseline and can never produce a promotable
    lesson, so defaulting to "unscoped" would be a silent opt-out of the entire
    learning loop. Defaulting to the template name is safe in the other
    direction too, because an undeclared scope takes
    :attr:`~selfloop.context.LoopContext.default_scope_tier`, which parks.
    """
    data = state.get("data") or {}
    if data.get(SCOPE):
        return str(data[SCOPE])
    params = state.get("params") or {}
    if params.get(SCOPE):
        return str(params[SCOPE])
    return str(state.get("template") or "")


def failure_tag_of(state: LoopState) -> str:
    """The structured tag for what went wrong this tick, or ``""``. See :data:`FAILURE_TAG`."""
    return str((state.get("data") or {}).get(FAILURE_TAG) or "")


def run_id_of(state: LoopState) -> str:
    """The id of the invocation running this tick, or ``""``. See :data:`RUN_ID`."""
    return str((state.get("params") or {}).get(RUN_ID) or "")


def stopped(state: LoopState) -> bool:
    """True when a node has already decided this tick must not continue."""
    return str(state.get("status") or "") in _STOP_STATUSES


# ---------------------------------------------------------------------------
# The shared terminal, plain steps, and reads
# ---------------------------------------------------------------------------


def ensure_park(graph: Graph, ctx: LoopContext) -> str:
    """Add the shared terminal :data:`PARK` node once per graph, and return its name.

    Idempotent, because every :func:`add_effect` call needs the node to exist and
    a template usually has more than one effect. The node writes one event and
    changes nothing: whichever node diverted here has already stamped the tick's
    status, and a terminal that helpfully stamped one of its own would overwrite
    the account of the node that actually knew what happened.
    """
    if PARK in graph.nodes:
        return PARK

    def _park(state: LoopState) -> dict[str, Any]:
        emit(
            ctx,
            LOOP_EVENT_KIND,
            "loop_stood_down",
            {
                "status": state.get("status"),
                "error": state.get("error"),
                "failure_tag": failure_tag_of(state),
            },
            run_id=run_id_of(state),
            node=PARK,
        )
        return {}

    graph.add_node(PARK, _park)
    graph.add_edge(PARK, END)
    return PARK


def _require_park(graph: Graph) -> None:
    if PARK not in graph.nodes:
        raise ValueError(
            "call ensure_park(graph, ctx) — or add_effect(), which calls it — before "
            "add_status_route(): the divert branch has nowhere to land"
        )


def add_step(
    graph: Graph,
    ctx: LoopContext,
    *,
    name: str,
    fn: StepFn,
    retries: int = 3,
) -> str:
    """Add a NON-effect node — pure computation — and return its name.

    *ctx* is accepted and not used, and that is worth one sentence rather than a
    lint suppression: every other builder here takes it, and a kit whose seven
    functions disagree about their first two arguments is a kit an author has to
    keep looking up. It also means this signature does not have to change the
    day a step node needs to read the context.

    **Terminal decision nodes go through here**, which is an improvement over the
    template kit this was ported from. There, only the shared ``park`` node was
    wrapped for observability and every template's ``idle``/``exhausted`` node
    was added raw — so the ledger had no enter/exit event for the most common
    non-result endings a loop has, and "why did this instance stop producing?"
    could not be answered from the event log alone. The executor now wraps every
    node it runs, so routing terminals through this builder also gives them the
    transient-retry budget the rest of the graph has.
    """
    del ctx  # see the docstring: uniformity, and room for the day it is needed
    graph.add_node(name, fn, retries)
    return name


def add_read(
    graph: Graph,
    ctx: LoopContext,
    *,
    name: str,
    tool: str,
    args_fn: ArgsFn,
    on_result: ResultFn | None = None,
    retries: int = 3,
) -> str:
    """Add a READ node: policy-checked and registry-resolved, but **not receipted**.

    A read is not an effect, so it gets no idempotency receipt, and that omission
    is deliberate rather than an economy. A poll MUST re-read the world every
    tick; a receipt keyed on the read would make the second tick a no-op and the
    loop would be looking at a snapshot of the day it started.

    It still goes through :func:`selfloop.tools.execute_effect` in ``mode="read"``,
    on exactly the code path an effect takes, so an ungranted, denied or
    mis-tiered tool is never reached. The kit this was ported from had a read
    helper that called the tool directly, which was a second, policy-free door
    into the tool plane sitting next to the locked one.

    **A dead credential must not be reported as an empty world.** A read tool
    that returns ``[]`` when its grant was revoked renders identically to an
    empty inbox, so the loop idles green forever and nobody is told. Raise
    :class:`~selfloop.contracts.BlockedLoopError` with ``cause="authorization"``
    instead: it is deliberately absent from the executor's transient allowlist,
    so it propagates on the first attempt rather than burning three retries on a
    credential that will never work, and the runtime settles the tick as
    ``BLOCKED``, which is adverse and reaches an operator. It is **not** caught
    here for that reason — swallowing it into a status update is how the
    distinction gets lost.
    """

    def _read(state: LoopState) -> dict[str, Any]:
        args = dict(args_fn(state))
        try:
            outcome = execute_effect(
                ctx,
                node=name,
                tool=ctx.tools.get(tool),
                args=args,
                business_key="",
                gate_token=None,
                mode="read",
            )
        except (EffectDenied, UnknownToolError) as exc:
            emit(
                ctx,
                EFFECT_EVENT_KIND,
                "read_refused",
                {"tool": tool, "error": f"{type(exc).__name__}: {exc}"},
                run_id=run_id_of(state),
                node=name,
            )
            return {
                "status": LoopStatus.ABORTED.value,
                "error": f"read {name}/{tool} refused: {exc}",
                "data": _tagged(state, TAG_READ_REFUSED),
                "log": [f"{name}: refused"],
            }
        update: dict[str, Any] = {"log": [f"{name}: read"]}
        if on_result is not None:
            update.update(on_result(state, outcome["result"]))
        return update

    graph.add_node(name, _read, retries)
    return name


# ---------------------------------------------------------------------------
# The effect builder — the reason this module exists
# ---------------------------------------------------------------------------


def add_effect(
    graph: Graph,
    ctx: LoopContext,
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
    """Add ``<name>_gate`` -> ``<name>`` and return the **GATE's** name.

    Callers wire *into* the returned gate name and *out of* ``name``. There is no
    edge that reaches ``name`` without traversing its gate, because this function
    is the only thing that adds either of them and it adds them together. That is
    the structural half of the guarantee; the execution seam re-derives the
    policy verdict and re-reads the approval row anyway, which is the half that
    survives a template losing its gate to a refactor.

    *retries* is the in-process attempt count for the effect node and defaults to
    1. Raising it is refused unless the tool declares
    :attr:`~selfloop.contracts.LoopTool.replay_on_unknown`: a claimed receipt
    with no recorded result means the external effect MAY have happened, and
    blind-retrying that is the duplicate irreversible effect the whole receipt
    layer exists to prevent. The durable retry for an effect is the next tick, in
    the next process, arbitrated by the receipt — not a loop inside this one.

    *judged_by* names a node downstream that will render the verdict for this
    effect. Naming one means a failed effect keeps routing so that node can read
    the result and word the outcome. The **default is ``None``**, which stamps
    ``FAILED`` on the spot, and loudness is what a template gets for not thinking
    about it: the next loop somebody writes will not have a verify node.

    *ttl_hours* overrides how long a human's decision on this effect remains
    authority. ``None`` takes the policy port's own answer.

    **``args_fn`` and ``key_fn`` must be pure functions of the state**, and this
    is the requirement a template author is most likely to break without
    noticing. Both are called twice — once in the gate, once in the effect — and
    the approval id digests the arguments while the receipt keys on the business
    key. An ``args_fn`` that reads a clock, a counter or a random source
    therefore mints an approval for a payload that no longer exists by the time
    the seam checks it, and the effect parks forever against a row it can never
    satisfy. Put the varying value in the state, in a node upstream of the gate,
    where the checkpoint can carry it.
    """
    ensure_park(graph, ctx)
    gate_name = f"{name}_gate"

    if retries > 1:
        # Resolved at BUILD time, in the developer's terminal, because the
        # alternative is finding out at 03:00 that a T2 send has been retried.
        try:
            registered = ctx.tools.get(tool)
        except UnknownToolError as exc:
            raise ValueError(
                f"effect {name!r}: retries={retries} needs tool {tool!r} to declare "
                "replay_on_unknown, and the tool is not granted to this instance, so "
                "the declaration cannot be checked. Refusing to build a retrying "
                "effect whose safety could not be verified."
            ) from exc
        if not registered.replay_on_unknown:
            raise ValueError(
                f"effect {name!r}: retries={retries} requires tool {tool!r} to declare "
                "replay_on_unknown — a claimed-but-uncompleted receipt must never be "
                "blind-retried, because at that point the effect may already have "
                "happened. Leave retries at 1 and let the next tick decide."
            )

    def _gate(state: LoopState) -> dict[str, Any]:
        args = dict(args_fn(state))
        business_key = str(key_fn(state))
        verdict = preview(ctx, tool, args)
        gates = dict(state.get("gates") or {})

        if verdict.decision == "deny":
            gates[name] = {"decision": "deny", "business_key": business_key}
            return {
                "gates": gates,
                "status": LoopStatus.ABORTED.value,
                "error": f"policy denied {name}/{tool}: {verdict.reason}",
                "data": _tagged(state, TAG_EFFECT_REFUSED),
                "log": [f"{name}: denied"],
            }

        if verdict.allows:
            gates[name] = {"decision": "allow", "business_key": business_key}
            return {"gates": gates, "log": [f"{name}: allowed ({verdict.tier.name})"]}

        # T2+, or a policy that demanded a human: one durable row, then stop.
        # Everything above this line re-executes when the thread resumes, which
        # is exactly why ensure_approval is idempotent by construction — one
        # row and one page, however many times this node is re-entered.
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
        approval_row_id = str(row.get("approval_id") or "")
        if not approval_row_id:
            raise LoopError(
                f"{name}/{tool}: the approvals store returned a row with no 'approval_id' "
                f"({sorted(row)}). Refusing to park on a row this loop cannot address: "
                "the seam would never be able to re-read it and the effect would wait "
                "for a decision nobody can record."
            )

        # The DURABLE ROW is the authority, never the fact that we parked. A
        # later tick re-enters this node from the top with the row already
        # decided or already expired; parking again on an approved row would
        # leave an authorised effect stuck forever, waiting for a second answer
        # to a question that was answered.
        settled = read_outcome(
            ctx,
            approval_row_id,
            binding=effect_binding(ctx, name, tool_obj, args),
        )
        if not settled.terminal:
            # THE ONLY raise site for ParkRequested in this package. A park is a
            # protocol between this gate and the executor; a second raise site
            # would be a second protocol nobody wrote down. Pinned by
            # tests/test_ast_invariants.py and by a counterfeit entry.
            raise ParkRequested(
                approval_row_id,
                {
                    "approval_id": approval_row_id,
                    "instance_id": ctx.instance_id,
                    "node": name,
                    "tool": tool,
                    "tier": verdict.tier.name,
                    "action_class": verdict.action_class.value,
                    "business_key": business_key,
                    "proposed_action": row.get("proposed_action", ""),
                    "expires_at": row.get("expires_at", ""),
                    "deep_link": deep_link(ctx, approval_row_id),
                },
            )

        if not settled.approved:
            gates[name] = {"decision": "refused", "business_key": business_key}
            return {
                "gates": gates,
                "status": LoopStatus.ABORTED.value,
                "error": (
                    f"{name}/{tool}: approval {approval_row_id} is {settled.state} "
                    f"— {settled.reason}"
                ),
                "data": _tagged(state, TAG_EFFECT_REFUSED),
                "log": [f"{name}: approval not granted"],
            }

        gates[name] = {
            "decision": "approved",
            "approval_id": approval_row_id,
            "business_key": business_key,
            "decided_by": settled.decided_by,
        }
        return {"gates": gates, "log": [f"{name}: approved by {settled.decided_by}"]}

    def _effect(state: LoopState) -> dict[str, Any]:
        args = dict(args_fn(state))
        business_key = str(key_fn(state))
        gates = state.get("gates") or {}
        try:
            outcome = execute_effect(
                ctx,
                node=name,
                tool=ctx.tools.get(tool),
                args=args,
                business_key=business_key,
                gate_token=gates.get(name),
            )
        except EffectUnavailable as exc:
            # ABSENCE, and the one branch here that is NOT adverse. The effect's
            # authority was never reached, so nothing was attempted and nothing
            # about this loop was judged. Scoring that against the acceptance
            # floor is "we could not ask" read as "the answer was no", which
            # auto-paused four healthy routines in one night — so the tick
            # stands down as IDLE, which is neutral and out of the denominator,
            # and says so loudly in three places rather than quietly in none.
            emit(
                ctx,
                EFFECT_EVENT_KIND,
                "effect_stood_down",
                {
                    "tool": tool,
                    "business_key": business_key,
                    "reason": exc.reason,
                    "detail": exc.detail[:500],
                },
                run_id=run_id_of(state),
                node=name,
            )
            data = dict(state.get("data") or {})
            data[UNAVAILABLE_EFFECTS] = [*(data.get(UNAVAILABLE_EFFECTS) or []), name]
            return {
                "status": LoopStatus.IDLE.value,
                # Carried on ``error`` because that is what the runtime renders
                # as the tick's detail. A neutral non-result still has to be
                # READABLE, or "loud" is a word in a docstring rather than a
                # property of the system.
                "error": (
                    f"unavailable: {name}/{tool} could not reach its authority "
                    f"({exc.reason}) — nothing was attempted"
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
            # All four are raised strictly BEFORE the callable is reached, so an
            # operator reading this in a ledger knows the external system was
            # not touched. EffectAttemptsExhausted arrives here too, as a
            # subclass of EffectDenied, which is the intended behaviour: a
            # permanently failing effect escalates to a human instead of
            # hammering an external system once per tick forever.
            emit(
                ctx,
                EFFECT_EVENT_KIND,
                "effect_refused",
                {"tool": tool, "error": f"{type(exc).__name__}: {exc}"},
                run_id=run_id_of(state),
                node=name,
            )
            return {
                "status": LoopStatus.ABORTED.value,
                "error": f"{type(exc).__name__}: {exc}",
                "data": _tagged(state, TAG_EFFECT_REFUSED),
                "log": [f"{name}: refused at the execution seam"],
            }

        succeeded = bool(outcome["succeeded"])
        detail = str(outcome.get("detail") or "")
        update: dict[str, Any] = {
            "effects": [
                {
                    "node": name,
                    "tool": tool,
                    "business_key": business_key,
                    "receipt": outcome["receipt"],
                    "replayed": outcome["replayed"],
                    "succeeded": succeeded,
                    "verified": outcome["verified"],
                }
            ],
            "log": [f"{name}: {'replayed' if outcome['replayed'] else 'executed'}"],
        }
        if on_result is not None:
            update.update(on_result(state, outcome["result"]))
        if not succeeded:
            # AFTER on_result, deliberately. A terminal status stamped by the
            # happy path — COMPLETED on a send, IDLE on an escalation — must not
            # outrank the seam's evidence that the effect did not take effect.
            update["log"] = [f"{name}: ran and did NOT take effect — {detail or 'no detail'}"]
            data = dict(update.get("data") or state.get("data") or {})
            if judged_by is None:
                update["status"] = LoopStatus.FAILED.value
                update["error"] = f"{name}/{tool}: {detail or 'the effect did not take effect'}"
                data[FAILURE_TAG] = TAG_EFFECT_DID_NOT_TAKE
            else:
                # Delegation, not amnesty: the named judge decides HOW to say
                # it, and verification_outcome refuses to render this tick
                # favourable while the list below is non-empty.
                data[FAILED_EFFECTS] = [*(data.get(FAILED_EFFECTS) or []), name]
            update["data"] = data
        return update

    graph.add_node(gate_name, _gate)
    graph.add_node(name, _effect, retries)
    graph.add_conditional_edges(gate_name, _gate_router(name), {name: name, PARK: PARK})
    return gate_name


def _gate_router(name: str) -> Callable[[LoopState], str]:
    """Route into the effect only on a gate token that says it may run.

    Reads the token the gate node just wrote rather than re-deriving the verdict,
    because the two must not be able to disagree: a router that re-evaluated
    policy could send a tick into an effect whose gate had denied it, if anything
    about the classification had moved in between. The token is a record of what
    the gate decided; the seam re-derives the verdict for real.
    """

    def route(state: LoopState) -> str:
        token = (state.get("gates") or {}).get(name) or {}
        return name if token.get("decision") in ("allow", "approved") else PARK

    return route


def add_status_route(graph: Graph, node: str, ok: str) -> None:
    """Continue to *ok* unless the tick stopped or stood down; then divert to :data:`PARK`.

    Two different reasons, both load-bearing. A tick that a node has already
    stopped — ``ABORTED``, ``FAILED``, ``BLOCKED`` — must not run its happy path.
    And a tick that stood down as ``IDLE`` because an effect's authority was
    unreachable must not walk into a verify node, because that node would go and
    look for the result of an effect that was never attempted, find nothing, and
    render the tick as a failure. That last step is how an absence becomes an
    adverse training label.
    """
    _require_park(graph)

    def route(state: LoopState) -> str:
        return PARK if str(state.get("status") or "") in _DIVERT_STATUSES else ok

    graph.add_conditional_edges(node, route, {ok: ok, PARK: PARK})


# ---------------------------------------------------------------------------
# Rendering a verify node's verdict as the tick's status
# ---------------------------------------------------------------------------


def _claims_verified(result: Any) -> bool:
    """Only an explicit ``verified: true``. See :func:`verification_outcome`."""
    return isinstance(result, Mapping) and result.get("verified") is True


def verification_outcome(result: Any, *, node: str, state: LoopState) -> dict[str, Any]:
    """Turn a verify tool's verdict into the TICK's status. Fail-closed.

    A verify node exists for one reason: to establish that the effect this tick
    performed actually happened. Its verdict is therefore the tick's status — the
    fact that the node *ran* is not. Both verify-bearing templates in the system
    this was ported from stamped ``COMPLETED`` unconditionally, which is why a
    live tick whose repair returned ``{"verified": false, "state":
    "repair_failed"}`` still settled as completed, accepted, gate-passed: the
    loop detected its own failure and graded it a pass.

    The contract is strict and one-directional. **Only an explicit
    ``verified is True`` renders COMPLETED.** A missing key, a non-mapping, an
    error dict, a string, ``None``, an enum that drifted after a deploy — every
    one of those is an ABSENCE of a verdict, and an absent verdict must never
    render as the most favourable outcome available.

    *state* carries the second, independent veto and is **required** for that
    reason. An effect the seam reported as ``succeeded=False`` is listed in
    ``data[FAILED_EFFECTS]``, and a non-empty list overrules a claimed
    ``verified: true`` and says so in the error. A template that routes a failed
    effect here rather than parking has delegated the WORDING, not the verdict —
    which is what keeps the failed-effect guarantee a property of the kit instead
    of a property of each verify tool somebody writes. Defaulting *state* to
    ``None`` would make forgetting it silently drop that veto, and a guard you
    can disable by omission is a guard that is eventually omitted.

    *node* is required too, for a smaller reason: the error text is what an
    operator reads, and "check_delivery: unverified" locates the problem where
    "verify: unverified" does not.
    """
    unresolved = sorted(set((state.get("data") or {}).get(FAILED_EFFECTS) or ()))
    verified = _claims_verified(result)

    if verified and unresolved:
        # The contradiction, named rather than resolved in favour of either
        # side: the seam holds evidence the effect did not take, and the node
        # asked to render that reported success anyway.
        return {
            "status": LoopStatus.FAILED.value,
            "error": (
                f"{node}: effect(s) {unresolved} ran without taking effect "
                f"({node} claimed verified)"
            ),
            "data": _tagged(state, TAG_EFFECT_DID_NOT_TAKE),
        }
    if verified:
        return {"status": LoopStatus.COMPLETED.value}

    if isinstance(result, Mapping):
        detail = str(result.get("state") or result.get("error") or "verified is not true")
    else:
        detail = f"verify returned {type(result).__name__}, not a verdict mapping"
    if unresolved:
        # Both signals agree. Keep the judge's WORDS — "repair_failed" and
        # "still_failing" are different incidents to whoever is on call — and
        # ADD the seam's fact rather than replacing one with the other.
        detail = f"{detail} (effect(s) {unresolved} did not take effect)"
    return {
        "status": LoopStatus.FAILED.value,
        "error": f"{node}: unverified — {detail}",
        "data": _tagged(state, TAG_UNVERIFIED),
    }


# ---------------------------------------------------------------------------
# The feedback edge
# ---------------------------------------------------------------------------


def inject_lessons(
    ctx: LoopContext,
    prompt: str,
    scope: str,
    query: str = "",
    *,
    run_id: str,
) -> str:
    """Prepend this scope's promoted lessons to *prompt*. **THE feedback edge.**

    One line in this package turns something the loop learned back into something
    the loop reads, and it is the ``return`` below. Everything upstream of it —
    the signals, the clusters, the staging, the promotion gate, the fingerprint
    bind — is bookkeeping; sever this line and the package is an audit trail with
    ambitions. It is pinned by ``test_lessons_are_injected_from_exactly_one_place``
    and by a counterfeit entry that deletes it and requires the end-to-end
    liveness test to go red.

    Returns *prompt* unchanged when there is nothing promoted for *scope*. An
    empty labelled section is noise in every prompt that carries it, and noise in
    a prompt is a cost paid on every tick forever.

    *query* is a relevance tie-break inside the recall ranking and never a
    filter. It defaults to the prompt itself, which is the right default: the
    text the loop is about to work on is the best available description of what
    it needs to be reminded of, and because a query can only reorder — never
    exclude — a bad guess costs nothing.

    *run_id* is required and keyword-only, exactly as it is on
    :func:`selfloop.learn.lesson_block` and for the same reason. Passing it
    records, before this run produces any outcome, the fact that these lessons
    were in it. That ordering is the entire difference between attribution and
    correlation. Forgetting it does not fail — it silently stops attribution,
    the counters stay at zero forever, and the loop's ranking never learns
    anything — so the caller has to type it. Pass ``""`` for a preview render.
    """
    block = lesson_block(ctx, scope, query or prompt, run_id=run_id)
    return f"{block}\n\n{prompt}" if block else prompt


__all__ = [
    "FAILED_EFFECTS",
    "FAILURE_TAG",
    "LOOP_EVENT_KIND",
    "PARK",
    "RUN_ID",
    "SCOPE",
    "TAG_EFFECT_DID_NOT_TAKE",
    "TAG_EFFECT_REFUSED",
    "TAG_READ_REFUSED",
    "TAG_UNVERIFIED",
    "UNAVAILABLE_EFFECTS",
    "ArgsFn",
    "KeyFn",
    "ResultFn",
    "StepFn",
    "add_effect",
    "add_read",
    "add_status_route",
    "add_step",
    "ensure_park",
    "failure_tag_of",
    "inject_lessons",
    "merge_data",
    "run_id_of",
    "scope_of",
    "stopped",
    "verification_outcome",
]
