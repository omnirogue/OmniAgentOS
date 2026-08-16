"""Template: propose -> evaluate -> improve (bounded) -> promote. The learning shape.

A refinement cycle with the hard stop that makes it safe to run unattended:
``params["max_rounds"]``. When the bound is reached without the evaluator's score
clearing ``params["score_threshold"]``, the tick ends ``ABORTED`` and **refuses
to publish** a candidate that never passed its own gate. Refusing is the point.
A refinement loop that publishes its best attempt when it runs out of attempts
has no gate; it has a preference.

The instance contract:

===========  =====  ===========================================================
tool         tier   signature
===========  =====  ===========================================================
``propose``  T0     ``(brief, history, feedback, round_index) -> dict``
``evaluate`` T0     ``(candidate, spec, history) -> dict`` — must carry
                    ``"score"`` in ``[0, 1]``; SHOULD carry ``"feedback"``
``promote``  T1+    ``(candidate, evaluation) -> Any`` — the effect; T2+ parks
===========  =====  ===========================================================

Plain-Python defaults for ``propose`` and ``evaluate`` ship in this module (see
:func:`default_tools`), so the quickstart and the end-to-end liveness test run
with no API key, no ``ModelPort`` and no network.

The evaluator is an INDEPENDENT scorer, structurally
-----------------------------------------------------

The scorer is never handed the proposer's prompt. Its arguments are the
CANDIDATE, the caller's SPEC, and the trajectory — and deliberately not
``brief``, which is the field that carries the injected lesson block. The
scorer therefore *cannot* score on "does the prompt mention the lesson", which
is the collusion that would green this package's headline liveness test while
proving nothing: a lesson would raise the score by being present, the tick would
settle favourable, the lesson's counters would rise, and nothing in the world
would have changed.

What is left to score on is a property of the produced ARTIFACT. The shipped
default scores the candidate text against ``spec["must_include"]`` — terms the
caller declared a good artifact must contain — and nothing else.

Structural is not the same as total, and the honest limit is worth stating: a
caller can still register the same callable under both names. What the template
can guarantee is the information separation, and it does.

Four fixes over the refinement template this was ported from
------------------------------------------------------------

1. **The trajectory lives in ``memo``, not ``data``.** ``data`` is per-tick
   scratch and is replaced on every fresh tick, so the previous template began
   every tick from zero: round 1 of tick 9 had never seen round 3 of tick 8, and
   an unattended refiner therefore could not improve across process invocations
   at all. ``memo`` is the cross-tick channel and survives.
2. **The whole history is passed, not only the last evaluation.** A proposer
   handed one line of feedback re-makes the mistake it fixed two rounds ago,
   which is how these loops oscillate.
3. **Every round is persisted as a durable record**, so a
   :class:`~selfloop.ports.SignalSource` can mine the trajectory after the fact.
   Learning from a refinement loop's own rounds is otherwise impossible: the
   rounds only ever existed in a checkpoint that the next tick overwrites.
4. **``max_steps`` is derived from ``max_rounds``**, so a cycle that fails to
   converge dies against a ceiling computed from this template's own declared
   bound rather than against a generic one. The router's ``exhausted`` branch is
   still the primary stop and the one that renders a legible ``ABORTED``; the
   derived ceiling is the backstop for the case where the round counter itself is
   wrong, and it names the cycle it was going round when it stopped.

There is no ``learn`` node here, and that is deliberate. The learning pass is
owned by ``runtime.run_once`` and runs after settlement, always. Mounting it as a
graph node as well gave the extract/stage/promote cycle two owners — double
mining, a racing cursor, and a promotion parking outside the executor's own
park/resume protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import LoopState, LoopStatus, LoopTool, RiskTier, digest_key
from selfloop.engine import END, CompiledGraph, Graph
from selfloop.kit import (
    FAILURE_TAG,
    LOOP_EVENT_KIND,
    PARK,
    RUN_ID,
    KeyFn,
    add_effect,
    add_read,
    add_step,
    ensure_park,
    inject_lessons,
    merge_data,
    run_id_of,
    scope_of,
    stopped,
)
from selfloop.ledger import emit, write_history
from selfloop.templates import LoopTemplate

NAME = "propose_evaluate_promote"

RECALL = "recall"
PROPOSE = "propose"
EVALUATE = "evaluate"
ROUND = "record_round"
PROMOTE = "promote"
EXHAUSTED = "exhausted"

#: How many refinement rounds one tick may run before it gives up and refuses to
#: publish. Three, because the marginal round of a bounded refiner is worth less
#: than the tick it costs, and an instance that needs more says so in its params.
DEFAULT_MAX_ROUNDS = 3

#: Score at or above which a candidate may be promoted.
DEFAULT_SCORE_THRESHOLD = 0.8

#: ``state["memo"]`` key holding the cross-tick trajectory: one entry per
#: evaluated round, oldest first.
TRAJECTORY = "trajectory"

#: Newest-N cap on the trajectory. A loop instance is ticked forever, and ``memo``
#: rides in the durable checkpoint, so an uncapped list grows the checkpoint row
#: without limit until it is too large to read — the same failure the state's
#: ``log`` and ``effects`` channels are capped against.
TRAJECTORY_CAP = 20

#: :class:`~selfloop.ports.RecordStore` kind for one evaluated round.
#:
#: A template-owned kind rather than a member of
#: :class:`~selfloop.contracts.RecordKind`, and that is legitimate: the store is
#: kind-generic on purpose, and ``RecordKind`` enumerates the spellings that two
#: or more modules of the PACKAGE share, which is where a silently divergent
#: spelling does its damage. This one is written and read inside this file.
TRAJECTORY_KIND = "proposal_round"

#: Failure tag stamped when the cycle exhausts its rounds below the threshold.
#: Clustering partitions by ``(scope, failure_tag)`` before it compares a single
#: token, so an adverse tick without one of these yields no signal at all and the
#: loop learns nothing from its own refusal to publish.
TAG_BELOW_THRESHOLD = "proposal_below_threshold"

#: The marker the shipped default proposer obeys. Lowercase and matched exactly:
#: a stub whose parsing rules are guessable is a stub whose behaviour a reader can
#: predict without running it.
DIRECTIVE_MARKER = "include:"


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _data(state: LoopState) -> dict[str, Any]:
    return dict(state.get("data") or {})


def _brief(state: LoopState) -> str:
    """The proposer's prompt for this tick — lesson block already prepended."""
    return str(_data(state).get("brief") or "")


def _candidate(state: LoopState) -> dict[str, Any]:
    return _mapping(_data(state).get("candidate"))


def _evaluation(state: LoopState) -> dict[str, Any]:
    return _mapping(_data(state).get("evaluation"))


def _rounds(state: LoopState) -> int:
    """Rounds completed in THIS tick. Per-tick, because ``max_rounds`` bounds a tick."""
    try:
        return int(_data(state).get("rounds") or 0)
    except (TypeError, ValueError):
        return 0


def _history(state: LoopState) -> list[dict[str, Any]]:
    """The whole cross-tick trajectory, oldest first. See fix 1 and fix 2 above."""
    entries = (state.get("memo") or {}).get(TRAJECTORY) or []
    return [_mapping(entry) for entry in entries]


def _score(state: LoopState) -> float:
    """This round's score, or ``0.0``.

    Fails CLOSED on every unreadable shape. A missing key, a string, ``None``, a
    dict where a number belongs — all of them are an evaluator that returned no
    verdict, and an absent verdict must never render as the score that promotes.
    """
    try:
        return float(_evaluation(state).get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _feedback(state: LoopState) -> str:
    """The most recent feedback available: this tick's, else the trajectory's last.

    The fallback is what lets tick N+1 open where tick N stopped instead of
    re-proposing the draft that was already rejected once.
    """
    current = str(_evaluation(state).get("feedback") or "")
    if current:
        return current
    history = _history(state)
    return str(history[-1].get("feedback") or "") if history else ""


def _promote_key(instance_id: str) -> KeyFn:
    """A ``key_fn`` closing over the instance: the business key is the CONTENT.

    "This candidate published" is the identity of the act, so two ticks that
    converge on the same artifact are one effect and the second is short-circuited
    by the first one's receipt. Keying on the round, the tick or a timestamp would
    make every tick a fresh effect and the receipt would protect nothing.
    """

    def key_fn(state: LoopState) -> str:
        return digest_key(NAME, instance_id, _candidate(state))

    return key_fn


# ---------------------------------------------------------------------------
# The shipped defaults: plain Python, no model, no network
# ---------------------------------------------------------------------------


def _directives(text: str) -> list[str]:
    """Every comma-separated term following an ``include:`` marker in *text*."""
    terms: list[str] = []
    for line in str(text).splitlines():
        marker = line.find(DIRECTIVE_MARKER)
        if marker < 0:
            continue
        for raw in line[marker + len(DIRECTIVE_MARKER) :].split(","):
            term = raw.strip().strip(".;")
            if term and term not in terms:
                terms.append(term)
    return terms


def default_propose(
    *,
    brief: str,
    history: Sequence[Mapping[str, Any]],
    feedback: str,
    round_index: int,
) -> dict[str, Any]:
    """The shipped ``propose`` tool: a deterministic drafter with one behaviour.

    It obeys explicit ``include:`` directives, wherever they appear — in the
    brief, in the evaluator's feedback, or in a lesson block that the loop
    promoted and injected. That is the whole of its intelligence, and it is
    enough: it makes the effect of an injected lesson visible in the ARTIFACT,
    which is the only place this template's evaluator ever looks.

    **The candidate never contains the brief.** It is built from the extracted
    terms and a fixed header, and nothing else. That is a deliberate constraint
    rather than a simplification: if the prompt's text could reach the artifact
    verbatim, a lesson could raise the score merely by being long, and the loop
    would be grading its own prompt again — the exact self-grading hole this
    template is shaped to close.

    Replace it with anything you like. It exists so that ``run_once`` works
    before you have written a tool, not because a drafter should be this dumb.
    """
    terms: list[str] = []
    sources = [str(brief), str(feedback)]
    sources.extend(str(_mapping(entry).get("feedback") or "") for entry in history or ())
    for source in sources:
        for term in _directives(source):
            if term not in terms:
                terms.append(term)

    lines = [f"draft (round {int(round_index)})", *(f"- {term}" for term in terms)]
    return {"text": "\n".join(lines), "terms": terms, "round": int(round_index)}


def default_evaluate(
    *,
    candidate: Mapping[str, Any],
    spec: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The shipped ``evaluate`` tool: score the ARTIFACT against the caller's spec.

    ``score = |required terms present in the candidate| / |required terms|``,
    where the required terms come from ``spec["must_include"]`` — a statement the
    caller made about what a good artifact contains. Nothing about the prompt is
    reachable from here, so nothing about the prompt can be scored.

    **An empty spec scores 0.0, not 1.0.** An evaluator with nothing to check has
    checked nothing, and a vacuous pass is strictly worse than no evaluator at
    all: with no evaluator this template refuses to promote and is visibly
    ungated, while a vacuous 1.0 promotes everything and is invisibly ungated.
    That is the same rule ``GateReceipt.is_vacuous`` states one layer up, and it
    is worth stating twice.

    *history* is accepted and ignored. It is in the signature so a caller's
    replacement can see the trajectory without a signature change; the default
    ignores it because a scorer that can see its own previous scores can drift
    towards them, and the whole value of this node is that it does not.
    """
    del history  # see the docstring: available to a replacement, unused here

    required = [str(term).strip() for term in (spec or {}).get("must_include") or ()]
    required = [term for term in required if term]
    text = str(_mapping(candidate).get("text") or "")

    if not required:
        return {
            "score": 0.0,
            "feedback": "",
            "missing": [],
            "present": [],
            "detail": (
                "the spec declared no must_include terms, so there was nothing to check — "
                "scoring 0.0 rather than passing vacuously"
            ),
        }

    lowered = text.lower()
    present = [term for term in required if term.lower() in lowered]
    missing = [term for term in required if term not in present]
    return {
        "score": len(present) / len(required),
        # Feedback is itself a directive, so the next round can act on it without
        # the proposer having to understand this evaluator.
        "feedback": f"{DIRECTIVE_MARKER} {', '.join(missing)}" if missing else "",
        "missing": missing,
        "present": present,
        "detail": f"{len(present)}/{len(required)} required term(s) present in the candidate",
    }


def default_tools() -> tuple[LoopTool, LoopTool]:
    """The ``propose`` and ``evaluate`` tools, ready to register. Both T0.

    T0 because neither leaves the process: they are pure functions of their
    arguments, they are mounted as read nodes, and a read is exempt from the
    receipt but never from the policy verdict.

    ``promote`` is NOT shipped. It is the tool that puts something in the world,
    so it is the tool whose tier, verifier and idempotency the caller has to think
    about — and a default for it would be a default for the only decision in this
    template that matters.
    """
    return (
        LoopTool(
            name=PROPOSE,
            tier=RiskTier.T0,
            call=default_propose,
            description="draft a candidate that obeys the include: directives it was given",
        ),
        LoopTool(
            name=EVALUATE,
            tier=RiskTier.T0,
            call=default_evaluate,
            description="score a candidate against the caller's declared must_include spec",
        ),
    )


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def max_steps_for(max_rounds: int) -> int:
    """Node budget for one tick of this template, derived from *max_rounds*.

    ``recall`` (1) + ``max_rounds`` x (``propose``, ``evaluate``,
    ``record_round``) + ``promote_gate`` (1) + ``promote`` (1) + one terminal (1),
    plus two nodes of slack so that an off-by-one in this arithmetic is not
    itself the thing that ends a tick.

    The router's ``exhausted`` branch is what normally stops the cycle, and it
    renders a legible ``ABORTED`` naming the score and the round count. This
    ceiling is the backstop for a cycle whose round counter is wrong, and it
    matters because the alternative is the context's generic bound: a template
    that fails to converge would then run to 50 nodes and report a path nobody can
    read, instead of stopping at a number derived from its own declared bound.
    """
    return 3 * max(1, int(max_rounds)) + 6


def build(ctx: LoopContext) -> CompiledGraph:
    """Assemble and compile this template's graph for one loop instance.

    ``max_rounds`` and ``score_threshold`` are read ONCE here, from
    ``ctx.params``, and closed over by both the router and the compiled ceiling.
    Reading them again from the tick's own params would allow the two bounds to
    disagree, and a cycle with two bounds honours neither.
    """
    params = ctx.params
    max_rounds = max(1, int(params.get("max_rounds", DEFAULT_MAX_ROUNDS)))
    threshold = float(params.get("score_threshold", DEFAULT_SCORE_THRESHOLD))

    graph = Graph()
    ensure_park(graph, ctx)

    def _recall(state: LoopState) -> dict[str, Any]:
        """Render the brief for this tick, with the loop's own promoted lessons on top.

        This is the node the whole package exists to make safe. What it prepends
        was admitted by the promotion gate on evidence from distinct settled
        runs — never on anything a previous run asserted about itself — and the
        prepending happens in exactly one place, :func:`selfloop.kit.inject_lessons`.
        """
        brief = str((state.get("params") or {}).get("brief") or "")
        run_id = run_id_of(state)
        prompt = inject_lessons(ctx, brief, scope_of(state), run_id=run_id)
        injected = prompt != brief
        update = merge_data(state, brief=prompt, lessons_injected=injected, rounds=0)
        if injected and not run_id:
            # Loud, and deliberately not fatal. Without a run id the injection
            # still happens but its USE is not recorded, so attribution can never
            # grade these lessons: their counters stay at zero, ranking never
            # learns and regression retirement never fires. That is the silent
            # starvation this package exists to refuse, so it goes in the durable
            # log and on the event stream instead of nowhere. The runtime stamps
            # the id into params; a graph driven by hand has to pass it.
            complaint = (
                f"{RECALL}: lessons were injected with no run id in "
                f"params[{RUN_ID!r}] — their use is NOT recorded and attribution "
                "cannot grade them"
            )
            emit(
                ctx,
                LOOP_EVENT_KIND,
                "lesson_use_unrecorded",
                {"scope": scope_of(state)},
                node=RECALL,
            )
            update["log"] = [complaint]
        return update

    add_step(graph, ctx, name=RECALL, fn=_recall)

    add_read(
        graph,
        ctx,
        name=PROPOSE,
        tool=PROPOSE,
        args_fn=lambda s: {
            "brief": _brief(s),
            "history": _history(s),
            "feedback": _feedback(s),
            "round_index": _rounds(s) + 1,
        },
        on_result=lambda s, r: merge_data(s, candidate=_mapping(r), rounds=_rounds(s) + 1),
    )
    add_read(
        graph,
        ctx,
        name=EVALUATE,
        tool=EVALUATE,
        # No ``brief``. See the module docstring: the scorer cannot score on the
        # prompt because the prompt is not reachable from here.
        args_fn=lambda s: {
            "candidate": _candidate(s),
            "spec": _mapping((s.get("params") or {}).get("spec")),
            "history": _history(s),
        },
        on_result=lambda s, r: merge_data(s, evaluation=_mapping(r)),
    )

    def _record_round(state: LoopState) -> dict[str, Any]:
        """Append this round to the cross-tick trajectory and to the durable record.

        Two writes, for two different readers. ``memo`` is what the next round and
        the next TICK read, so the cycle can improve across process invocations.
        The record and the event are what a :class:`~selfloop.ports.SignalSource`
        reads after the fact, so a refinement loop's own rounds can become
        evidence — which they could not in the template this was ported from,
        where a round existed only in a checkpoint the next tick overwrote.

        The record id is derived from ``(instance, run, tick, round)``, so a
        mid-run resume that re-enters this node writes nothing new: ``put_once``
        returns False and the first account of the round stays authoritative.
        """
        run_id = run_id_of(state)
        tick = int(state.get("tick") or 0)
        completed = _rounds(state)
        entry: dict[str, Any] = {
            "id": digest_key(TRAJECTORY_KIND, ctx.instance_id, run_id, tick, completed),
            "instance_id": ctx.instance_id,
            "template": NAME,
            "scope": scope_of(state),
            "run_id": run_id,
            "tick": tick,
            "round": completed,
            "at": _stamp(ctx),
            "candidate": _candidate(state),
            "score": _score(state),
            "feedback": str(_evaluation(state).get("feedback") or ""),
            "missing": list(_evaluation(state).get("missing") or ()),
        }
        write_history(ctx, TRAJECTORY_KIND, str(entry["id"]), entry)
        emit(
            ctx,
            TRAJECTORY_KIND,
            "round_evaluated",
            {"round": completed, "score": entry["score"], "record": entry["id"]},
            run_id=run_id,
            node=ROUND,
        )
        memo = dict(state.get("memo") or {})
        memo[TRAJECTORY] = [*(memo.get(TRAJECTORY) or []), entry][-TRAJECTORY_CAP:]
        return {
            "memo": memo,
            "log": [f"round {completed}: scored {entry['score']:.3f}"],
        }

    add_step(graph, ctx, name=ROUND, fn=_record_round)

    gate = add_effect(
        graph,
        ctx,
        name=PROMOTE,
        tool=PROMOTE,
        args_fn=lambda s: {"candidate": _candidate(s), "evaluation": _evaluation(s)},
        key_fn=_promote_key(ctx.instance_id),
        on_result=lambda s, r: {
            **merge_data(s, promoted=r),
            "status": LoopStatus.COMPLETED.value,
        },
        # No judge. There is no verify node downstream, so an effect the seam
        # reports as not having taken effect stamps FAILED here rather than
        # routing on to a node that would have to invent a verdict.
        judged_by=None,
    )

    def _exhausted(state: LoopState) -> dict[str, Any]:
        score = _score(state)
        detail = (
            f"score {score:.3f} is still below the threshold {threshold:.3f} after "
            f"{_rounds(state)} round(s) — refusing to promote a candidate that never "
            "passed its own gate"
        )
        return {
            "status": LoopStatus.ABORTED.value,
            "error": detail,
            "data": {**_data(state), FAILURE_TAG: TAG_BELOW_THRESHOLD},
            "log": ["round budget exhausted"],
        }

    add_step(graph, ctx, name=EXHAUSTED, fn=_exhausted)

    def after_round(state: LoopState) -> str:
        if stopped(state):
            return PARK
        if _score(state) >= threshold:
            return gate
        if _rounds(state) >= max_rounds:
            return EXHAUSTED
        return PROPOSE

    graph.set_entry(RECALL)
    graph.add_conditional_edges(
        RECALL, lambda s: PARK if stopped(s) else PROPOSE, {PROPOSE: PROPOSE, PARK: PARK}
    )
    graph.add_conditional_edges(
        PROPOSE, lambda s: PARK if stopped(s) else EVALUATE, {EVALUATE: EVALUATE, PARK: PARK}
    )
    graph.add_conditional_edges(
        EVALUATE, lambda s: PARK if stopped(s) else ROUND, {ROUND: ROUND, PARK: PARK}
    )
    graph.add_conditional_edges(
        ROUND,
        after_round,
        {gate: gate, PROPOSE: PROPOSE, EXHAUSTED: EXHAUSTED, PARK: PARK},
    )
    graph.add_edge(PROMOTE, END)
    graph.add_edge(EXHAUSTED, END)
    return graph.compile(max_steps=max_steps_for(max_rounds))


def _stamp(ctx: LoopContext) -> str:
    """``Clock.now_iso``, or ``""``. A broken clock must not fail a durable write."""
    try:
        return ctx.clock.now_iso()
    except Exception:  # noqa: BLE001 - a record stamp is not worth losing the record for
        return ""


TEMPLATE = LoopTemplate(
    name=NAME,
    family="refine",
    required_tools=(PROPOSE, EVALUATE, PROMOTE),
    build=build,
    description=(
        "propose -> evaluate -> improve, bounded by max_rounds, then promote; "
        "the evaluator never sees the proposer's prompt"
    ),
)

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_SCORE_THRESHOLD",
    "DIRECTIVE_MARKER",
    "EVALUATE",
    "EXHAUSTED",
    "NAME",
    "PROMOTE",
    "PROPOSE",
    "RECALL",
    "ROUND",
    "TAG_BELOW_THRESHOLD",
    "TEMPLATE",
    "TRAJECTORY",
    "TRAJECTORY_CAP",
    "TRAJECTORY_KIND",
    "build",
    "default_evaluate",
    "default_propose",
    "default_tools",
    "max_steps_for",
]
