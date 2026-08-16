"""The durable executor: three entry states, one budget, and failing closed.

These tests run against both shipped storage backends (see ``tests/conftest.py``),
because the checkpoint is the executor's entire memory and "it resumed correctly"
is a statement about what a store handed back, not about what a dict remembered.

The four properties worth reading the file for:

* **A parked thread invokes nothing.** Not the parked node, not the entry node.
  The alternative reading — "nothing is running, so start a fresh tick" — re-runs
  every pre-park node on every scheduled invocation for as long as a human takes
  to answer, re-drafting and re-charging the world once per tick while presenting
  as a well-behaved parked loop.
* **A resumed tick enters AT the parked node**, and a crashed one at the node
  that died. Earlier nodes are not replayed; their effects are already in the
  checkpoint being read from.
* **A runaway tick terminates legibly and does not brick its thread.** The
  predecessor raised a graph library's recursion error, which told an operator
  that something had misbehaved without telling them where — and the answer was
  always in the cycle it had been going round.
* **A tick that ends without a recognised terminal status is FAILED.** The
  predecessor defaulted it to ``COMPLETED``: the most favourable possible outcome
  at the exact point where the loop grades itself.

One deliberate deviation from the package's own rule: these tests raise
:class:`~selfloop.engine.ParkRequested` directly from a test node. Inside
``selfloop/`` there is exactly one raise site (``selfloop.kit``) and an AST suite
pins it, because a park is a protocol between the effect gate and the executor.
A test that wants to exercise the executor's half of that protocol has no other
way to enter it, and these files are not part of the package.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from selfloop.context import LoopContext
from selfloop.contracts import (
    LOG_CAP,
    EffectDenied,
    EffectStateUnknown,
    LoopError,
    LoopState,
    LoopStatus,
    RecursionExceeded,
    TransientLoopError,
    initial_state,
)
from selfloop.engine import (
    CHECKPOINT_VERSION,
    END,
    RESUME_CHANNEL,
    TRANSIENT_EXCEPTIONS,
    CompiledGraph,
    Graph,
    ParkRequested,
    merge_state,
)

NodeFn = Callable[[LoopState], "Mapping[str, Any] | None"]


def linear(*nodes: tuple[str, NodeFn], max_steps: int | None = None) -> CompiledGraph:
    """Compile ``a -> b -> ... -> END`` from ``(name, fn)`` pairs."""
    graph = Graph()
    names = [name for name, _ in nodes]
    for name, fn in nodes:
        graph.add_node(name, fn)
    for current, following in zip(names, names[1:], strict=False):
        graph.add_edge(current, following)
    graph.add_edge(names[-1], END)
    graph.set_entry(names[0])
    return graph.compile(max_steps=max_steps)


def fresh(ctx: LoopContext) -> LoopState:
    """A fresh tick's input for *ctx*."""
    return initial_state(ctx.instance_id, ctx.template, ctx.params)


def completed(_state: LoopState) -> Mapping[str, Any]:
    """A node that stamps the one favourable terminal status."""
    return {"status": LoopStatus.COMPLETED.value}


# ---------------------------------------------------------------------------
# Entry state (c): idle — a fresh tick on a thread whose last tick finished
# ---------------------------------------------------------------------------


def test_a_fresh_tick_runs_from_the_entry_node_and_settles(ctx: LoopContext) -> None:
    seen: list[str] = []

    def observe(state: LoopState) -> Mapping[str, Any]:
        seen.append("observe")
        return {"data": {"found": 2}}

    def act(state: LoopState) -> Mapping[str, Any]:
        seen.append("act")
        return {"status": LoopStatus.COMPLETED.value, "effects": [{"tool": "noop"}]}

    graph = linear(("observe", observe), ("act", act))
    final = graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert seen == ["observe", "act"]
    assert final["status"] == LoopStatus.COMPLETED.value
    assert final["data"] == {"found": 2}
    assert final["tick"] == 1

    snap = graph.snapshot(ctx, ctx.thread_id)
    assert snap.idle is True
    assert snap.next_node == ""
    assert snap.steps == 0


def test_the_next_tick_keeps_memo_and_starts_data_from_a_clean_slate(
    ctx: LoopContext,
) -> None:
    """``memo`` is cross-tick memory; ``data`` is per-tick scratch. Both by design.

    ``initial_state`` deliberately omits ``memo`` and ``tick``, so a fresh tick
    leaves those channels untouched — which is what lets a multi-tick template
    keep the handle it is waiting on instead of restarting from zero every time.
    """
    entry_state: list[LoopState] = []

    def step(state: LoopState) -> Mapping[str, Any]:
        entry_state.append(dict(state))  # type: ignore[arg-type]
        return {
            "status": LoopStatus.COMPLETED.value,
            "memo": {"cursor": state.get("memo", {}).get("cursor", 0) + 1},
            "data": {"scratch": "tick"},
        }

    graph = linear(("step", step))
    graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    final = graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert entry_state[1]["memo"] == {"cursor": 1}  # survived the tick boundary
    assert entry_state[1]["data"] == {}  # replaced by the fresh tick
    assert final["memo"] == {"cursor": 2}
    assert final["tick"] == 2


# ---------------------------------------------------------------------------
# Entry state (a): parked
# ---------------------------------------------------------------------------


def _parking_graph(calls: Counter[str]) -> CompiledGraph:
    """``observe -> gate``, where ``gate`` parks until a resume is deposited."""

    def observe(state: LoopState) -> Mapping[str, Any]:
        calls["observe"] += 1
        return {"data": {"draft": "hello"}}

    def gate(state: LoopState) -> Mapping[str, Any]:
        calls["gate"] += 1
        resume = (state.get("gates") or {}).get(RESUME_CHANNEL)
        if resume is None:
            raise ParkRequested("appr-1", {"summary": "send the draft"})
        return {"status": LoopStatus.COMPLETED.value, "log": [f"resumed with {resume['verdict']}"]}

    return linear(("observe", observe), ("gate", gate))


def test_a_park_stops_the_tick_and_records_where(ctx: LoopContext) -> None:
    calls: Counter[str] = Counter()
    graph = _parking_graph(calls)

    final = graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert final["status"] == LoopStatus.PARKED.value
    snap = graph.snapshot(ctx, ctx.thread_id)
    assert snap.parked is True
    assert snap.parked_at == "gate"
    assert snap.next_node == "gate"
    assert snap.approval_id == "appr-1"
    assert snap.park_payload == {"summary": "send the draft"}
    assert snap.mid_run is False


def test_a_parked_thread_invokes_nothing_however_often_it_is_ticked(
    ctx: LoopContext,
) -> None:
    """The property that stops a parked loop re-charging the world once per tick."""
    calls: Counter[str] = Counter()
    graph = _parking_graph(calls)
    graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert calls == {"observe": 1, "gate": 1}

    for _ in range(5):
        state = graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
        assert state["status"] == LoopStatus.PARKED.value

    assert calls == {"observe": 1, "gate": 1}


def test_a_resume_enters_at_the_parked_node_and_replays_nothing_before_it(
    ctx: LoopContext,
) -> None:
    calls: Counter[str] = Counter()
    graph = _parking_graph(calls)
    graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    final = graph.invoke(ctx, ctx.thread_id, resume={"state": "approved", "by": "owner"})

    assert calls == {"observe": 1, "gate": 2}
    assert final["status"] == LoopStatus.COMPLETED.value
    assert any("resumed with" in line for line in final["log"])
    assert graph.snapshot(ctx, ctx.thread_id).parked is False


def test_a_resume_deposits_the_verdict_as_a_hint_never_as_authority(
    ctx: LoopContext,
) -> None:
    """The resumed node is handed the decision, in a channel it must not trust.

    The channel lives in a checkpoint, and a checkpoint is storage this package
    does not treat as trusted — so the node that parked re-reads the approvals
    store itself, exactly as the execution seam does.
    """
    seen: list[Mapping[str, Any]] = []

    def gate(state: LoopState) -> Mapping[str, Any]:
        resume = (state.get("gates") or {}).get(RESUME_CHANNEL)
        if resume is None:
            raise ParkRequested("appr-7")
        seen.append(resume)
        return {"status": LoopStatus.COMPLETED.value}

    graph = linear(("gate", gate))
    graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    graph.invoke(ctx, ctx.thread_id, resume={"state": "approved"})

    assert seen == [{"node": "gate", "approval_id": "appr-7", "verdict": {"state": "approved"}}]


def test_a_resumed_tick_that_stamps_nothing_fails_closed_rather_than_reading_parked(
    ctx: LoopContext,
) -> None:
    """A stale PARKED would render as a neutral non-result about an answered human."""

    def gate(state: LoopState) -> Mapping[str, Any] | None:
        if (state.get("gates") or {}).get(RESUME_CHANNEL) is None:
            raise ParkRequested("appr-2")
        return None  # the resumed path forgets to stamp a status

    graph = linear(("gate", gate))
    graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    final = graph.invoke(ctx, ctx.thread_id, resume={"state": "approved"})

    assert final["status"] == LoopStatus.FAILED.value


# ---------------------------------------------------------------------------
# Entry state (b): mid-run
# ---------------------------------------------------------------------------


def test_a_crash_resumes_at_the_node_that_died_without_replaying_earlier_ones(
    ctx: LoopContext,
) -> None:
    calls: Counter[str] = Counter()

    def a(state: LoopState) -> Mapping[str, Any]:
        calls["a"] += 1
        return {"data": {"fetched": True}}

    def b(state: LoopState) -> Mapping[str, Any]:
        calls["b"] += 1
        if calls["b"] == 1:
            raise RuntimeError("the process died here")
        return {"status": LoopStatus.COMPLETED.value}

    graph = linear(("a", a), ("b", b))

    with pytest.raises(RuntimeError, match="the process died here"):
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    snap = graph.snapshot(ctx, ctx.thread_id)
    assert snap.mid_run is True
    assert snap.next_node == "b"
    assert "b: RuntimeError: the process died here" in snap.error
    assert snap.path == ("a", "b")

    # No fresh state is supplied: the next invocation resumes what is in flight.
    final = graph.invoke(ctx, ctx.thread_id)

    assert calls == {"a": 1, "b": 2}
    assert final["status"] == LoopStatus.COMPLETED.value
    assert final["data"] == {"fetched": True}  # a's work survived the crash


def test_a_crash_does_not_overwrite_the_templates_own_account_of_the_tick(
    ctx: LoopContext,
) -> None:
    """A node that already decided ABORTED must still read ABORTED after a crash.

    The fault is recorded in the checkpoint ENVELOPE and as one line in the
    durable log. It does not touch ``status`` or ``error``, which belong to the
    template.
    """

    def decide(state: LoopState) -> Mapping[str, Any]:
        return {"status": LoopStatus.ABORTED.value, "error": "policy denied the send"}

    def cleanup(state: LoopState) -> Mapping[str, Any]:
        raise RuntimeError("the cleanup blew up")

    graph = linear(("decide", decide), ("cleanup", cleanup))
    with pytest.raises(RuntimeError):
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    snap = graph.snapshot(ctx, ctx.thread_id)
    assert snap.state["status"] == LoopStatus.ABORTED.value
    assert snap.state["error"] == "policy denied the send"
    assert "cleanup: RuntimeError: the cleanup blew up" in snap.error


def test_an_idle_thread_with_no_fresh_state_refuses_rather_than_guessing(
    ctx: LoopContext,
) -> None:
    graph = linear(("step", completed))
    with pytest.raises(LoopError, match="nothing in flight"):
        graph.invoke(ctx, ctx.thread_id)


def test_the_checkpoint_names_the_next_node_before_that_node_runs(
    ctx: LoopContext,
) -> None:
    """THE kill-drill invariant, observed from inside the node that follows.

    A SIGKILL one instruction after this write resumes at ``b``; one instruction
    earlier re-runs ``a`` — which is why every external effect is receipted
    rather than trusted to run once.
    """
    observed_next: list[str] = []

    def a(state: LoopState) -> None:
        return None

    def b(state: LoopState) -> Mapping[str, Any]:
        raw = ctx.checkpoints.load(ctx.thread_id) or {}
        observed_next.append(str(raw.get("next_node")))
        return {"status": LoopStatus.COMPLETED.value}

    linear(("a", a), ("b", b)).invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert observed_next == ["b"]


def test_a_checkpoint_from_another_engine_version_is_refused(ctx: LoopContext) -> None:
    """Resuming against a schema you are guessing at re-executes committed nodes."""
    ctx.checkpoints.save(
        ctx.thread_id,
        {"version": CHECKPOINT_VERSION + 1, "state": {}, "next_node": "somewhere"},
    )
    graph = linear(("step", completed))
    with pytest.raises(LoopError, match="refusing to resume"):
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))


def test_a_checkpoint_naming_a_node_this_template_lacks_is_refused(
    ctx: LoopContext,
) -> None:
    """The usual cause is a template renamed under a live instance."""
    graph = linear(("step", completed))
    ctx.checkpoints.save(
        ctx.thread_id,
        {"version": CHECKPOINT_VERSION, "state": {}, "next_node": "gone", "steps": 1},
    )
    with pytest.raises(LoopError, match="which this template does not have"):
        graph.invoke(ctx, ctx.thread_id)


# ---------------------------------------------------------------------------
# The reducers
# ---------------------------------------------------------------------------


def test_merge_state_caps_the_accumulating_channels() -> None:
    """A loop instance is ticked forever; an uncapped ``add`` grows without limit."""
    state = merge_state({"log": [], "effects": []}, {"log": [f"line {i}" for i in range(60)]})
    state = merge_state(state, {"effects": [{"n": i} for i in range(60)]})

    assert len(state["log"]) == LOG_CAP == 50
    assert state["log"][0] == "line 10"  # the OLDEST are dropped, not the newest
    assert state["log"][-1] == "line 59"
    assert len(state["effects"]) == LOG_CAP
    assert state["effects"][-1] == {"n": 59}


def test_the_cap_holds_across_ticks_not_just_within_one(ctx: LoopContext) -> None:
    def chatty(state: LoopState) -> Mapping[str, Any]:
        return {
            "status": LoopStatus.COMPLETED.value,
            "log": [f"tick {state['tick']} line {i}" for i in range(30)],
            "effects": [{"tick": state["tick"], "n": i} for i in range(30)],
        }

    graph = linear(("chatty", chatty))
    for _ in range(4):
        final = graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert len(final["log"]) == LOG_CAP
    assert len(final["effects"]) == LOG_CAP
    assert final["log"][-1] == "tick 4 line 29"


def test_a_channel_with_no_declared_reducer_is_replaced_outright() -> None:
    """The right default for per-tick scratch, and the wrong one for an audit log."""
    state = merge_state({"data": {"old": 1}, "status": "running"}, {"data": {"new": 2}})
    assert state["data"] == {"new": 2}


def test_merge_state_mutates_neither_argument() -> None:
    """A node changes state by RETURNING an update, never by editing what it got.

    That is what makes the checkpoint written afterwards a complete description
    of what the node did.
    """
    original = {"log": ["a"], "data": {"k": 1}}
    update = {"log": ["b"]}
    merged = merge_state(original, update)

    assert original == {"log": ["a"], "data": {"k": 1}}
    assert update == {"log": ["b"]}
    assert merged["log"] == ["a", "b"]


# ---------------------------------------------------------------------------
# The step budget
# ---------------------------------------------------------------------------


def _cycle_graph(seen: list[str], *, max_steps: int | None = None) -> CompiledGraph:
    """``propose -> evaluate -> propose`` forever, with a branch to END never taken."""

    def propose(state: LoopState) -> None:
        seen.append("propose")
        return None

    def evaluate(state: LoopState) -> None:
        seen.append("evaluate")
        return None

    graph = Graph()
    graph.add_node("propose", propose)
    graph.add_node("evaluate", evaluate)
    graph.add_edge("propose", "evaluate")
    graph.add_conditional_edges(
        "evaluate", lambda _state: "again", {"again": "propose", "done": END}
    )
    graph.set_entry("propose")
    return graph.compile(max_steps=max_steps)


def test_a_runaway_cycle_terminates_with_the_path_it_was_going_round(
    make_ctx: Callable[..., LoopContext],
) -> None:
    ctx = make_ctx(max_steps=6)
    seen: list[str] = []
    graph = _cycle_graph(seen)

    with pytest.raises(RecursionExceeded) as caught:
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    error = caught.value
    assert error.steps == 6
    assert error.max_steps == 6
    assert len(seen) == 6
    # The path carries the node it was ABOUT to enter as well as the six it ran,
    # because "where would it have gone next" is half of reading a cycle.
    assert error.path == ("propose", "evaluate") * 3 + ("propose",)
    assert "propose -> evaluate -> propose" in error.detail
    assert "propose x4" in error.detail


def test_an_exhausted_tick_leaves_the_thread_startable_rather_than_bricked(
    make_ctx: Callable[..., LoopContext],
) -> None:
    """The alternative resumes into the same exhausted cycle forever.

    That is a permanent brick healed only by an operator who knows to drop the
    checkpoint — so the budget is reset and the thread is left terminal, with the
    tick settled ABORTED so it still counts against the acceptance floor.
    """
    ctx = make_ctx(max_steps=4)
    seen: list[str] = []
    graph = _cycle_graph(seen)

    with pytest.raises(RecursionExceeded):
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))

    snap = graph.snapshot(ctx, ctx.thread_id)
    assert snap.idle is True
    assert snap.steps == 0
    assert snap.state["status"] == LoopStatus.ABORTED.value
    assert "exceeded its budget" in snap.state["error"]

    # And a fresh tick really does start again from the entry node.
    seen.clear()
    with pytest.raises(RecursionExceeded):
        graph.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert seen[0] == "propose"


def test_the_budget_is_the_stricter_of_the_context_and_the_template(
    make_ctx: Callable[..., LoopContext],
) -> None:
    """A template that knows its own bound may lower the ceiling, never raise it."""
    ctx = make_ctx(max_steps=10)
    seen: list[str] = []
    with pytest.raises(RecursionExceeded) as caught:
        _cycle_graph(seen, max_steps=3).invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert caught.value.max_steps == 3

    ctx = make_ctx(max_steps=2, instance_id="t2")
    seen.clear()
    with pytest.raises(RecursionExceeded) as caught:
        _cycle_graph(seen, max_steps=9).invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert caught.value.max_steps == 2


def test_a_park_and_resume_do_not_refill_the_step_budget(
    make_ctx: Callable[..., LoopContext],
) -> None:
    """A tick that resumed after a park is still the same tick.

    Refilling the budget on resume would let a template that parks mid-cycle
    churn forever, one process at a time, with every individual invocation
    looking well-behaved. The measurement: two of the four steps are spent
    before the park, so exactly one node may run after the resume — under a
    refilled budget three would.
    """
    ctx = make_ctx(max_steps=4)
    calls: Counter[str] = Counter()

    def observe(state: LoopState) -> None:
        calls["observe"] += 1
        return None

    def gate(state: LoopState) -> None:
        calls["gate"] += 1
        if (state.get("gates") or {}).get(RESUME_CHANNEL) is None:
            raise ParkRequested("appr-3")
        return None

    def spin(state: LoopState) -> None:
        calls["spin"] += 1
        return None

    graph = Graph()
    graph.add_node("observe", observe)
    graph.add_node("gate", gate)
    graph.add_node("spin", spin)
    graph.add_edge("observe", "gate")
    graph.add_edge("gate", "spin")
    graph.add_conditional_edges("spin", lambda _state: "again", {"again": "spin", "done": END})
    graph.set_entry("observe")
    compiled = graph.compile()

    compiled.invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert compiled.snapshot(ctx, ctx.thread_id).steps == 2

    with pytest.raises(RecursionExceeded):
        compiled.invoke(ctx, ctx.thread_id, resume={"state": "approved"})

    assert calls == {"observe": 1, "gate": 2, "spin": 1}


# ---------------------------------------------------------------------------
# Failing closed on the terminal status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stamped",
    [
        pytest.param(None, id="never-stamped"),
        pytest.param("", id="empty"),
        pytest.param("done", id="unrecognised"),
        pytest.param("COMPLETED", id="wrong-case"),
    ],
)
def test_a_tick_without_a_recognised_terminal_status_fails_closed(
    ctx: LoopContext, stamped: str | None
) -> None:
    """The single line in the executor most worth getting right.

    The predecessor defaulted this to ``COMPLETED`` — the most favourable
    possible outcome, placed exactly where the loop grades itself.
    """

    def step(state: LoopState) -> Mapping[str, Any] | None:
        return None if stamped is None else {"status": stamped}

    final = linear(("step", step)).invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert final["status"] == LoopStatus.FAILED.value
    assert final["error"]


def test_the_failure_explains_which_of_the_two_cases_it_was(ctx: LoopContext) -> None:
    running = linear(("step", lambda _state: None)).invoke(
        ctx, ctx.thread_id, state=fresh(ctx)
    )
    assert "still marked 'running'" in running["error"]

    drifted = linear(("step", lambda _state: {"status": "done"})).invoke(
        ctx, ctx.thread_id, state=fresh(ctx)
    )
    assert "no usable terminal status" in drifted["error"]


def test_an_error_the_template_already_recorded_is_not_overwritten(
    ctx: LoopContext,
) -> None:
    """A template's own account of what went wrong outranks the fallback text."""

    def step(state: LoopState) -> Mapping[str, Any]:
        return {"status": "confused", "error": "the inbox returned an unparseable page"}

    final = linear(("step", step)).invoke(ctx, ctx.thread_id, state=fresh(ctx))
    assert final["status"] == LoopStatus.FAILED.value
    assert final["error"] == "the inbox returned an unparseable page"


# ---------------------------------------------------------------------------
# Retries, routing and node contracts
# ---------------------------------------------------------------------------


def test_only_declared_transient_faults_are_retried(ctx: LoopContext) -> None:
    calls: Counter[str] = Counter()

    def flaky(state: LoopState) -> Mapping[str, Any]:
        calls["flaky"] += 1
        if calls["flaky"] < 3:
            raise TransientLoopError("the socket hiccuped")
        return {"status": LoopStatus.COMPLETED.value}

    graph = Graph()
    graph.add_node("flaky", flaky, retries=3)
    graph.add_edge("flaky", END)
    graph.set_entry("flaky")

    final = graph.compile().invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert calls["flaky"] == 3
    assert final["status"] == LoopStatus.COMPLETED.value


def test_a_fault_outside_the_allowlist_is_not_retried_even_when_retries_are_declared(
    ctx: LoopContext,
) -> None:
    calls: Counter[str] = Counter()

    def broken(state: LoopState) -> Mapping[str, Any]:
        calls["broken"] += 1
        raise ValueError("a bug, not a blip")

    graph = Graph()
    graph.add_node("broken", broken, retries=5)
    graph.add_edge("broken", END)
    graph.set_entry("broken")

    with pytest.raises(ValueError, match="a bug, not a blip"):
        graph.compile().invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert calls["broken"] == 1


def test_the_retry_allowlist_excludes_every_fault_that_must_not_be_repeated() -> None:
    """Retrying any of these is a specific, named, expensive mistake.

    ``EffectStateUnknown`` is the duplicate irreversible effect the whole receipt
    layer exists to prevent; ``EffectDenied`` is a policy bypass with a loop for a
    driver; a bare ``OSError`` covers ``PermissionError`` and
    ``FileNotFoundError``, neither of which gets better by being tried again.
    """
    for fault in (EffectStateUnknown, EffectDenied, OSError, PermissionError, FileNotFoundError):
        assert not issubclass(fault, TRANSIENT_EXCEPTIONS)
    for fault in (TransientLoopError, ConnectionError, TimeoutError):
        assert issubclass(fault, TRANSIENT_EXCEPTIONS)


def test_a_router_that_names_an_undeclared_branch_stops_the_tick(
    ctx: LoopContext,
) -> None:
    """The fall-through target of a misrouted branch is always the happy path."""
    graph = Graph()
    graph.add_node("decide", lambda _state: None)
    graph.add_node("act", completed)
    graph.add_conditional_edges("decide", lambda _state: "maybe", {"yes": "act", "no": END})
    graph.add_edge("act", END)
    graph.set_entry("decide")

    with pytest.raises(LoopError, match="not one of its declared branches"):
        graph.compile().invoke(ctx, ctx.thread_id, state=fresh(ctx))


def test_a_node_that_returns_something_other_than_channels_is_a_template_bug(
    ctx: LoopContext,
) -> None:
    """Returning the state itself silently discards every other channel."""
    with pytest.raises(LoopError, match="returns a mapping of channel updates"):
        linear(("step", lambda _state: "done")).invoke(  # type: ignore[arg-type,return-value]
            ctx, ctx.thread_id, state=fresh(ctx)
        )


# ---------------------------------------------------------------------------
# Build-time validation: every one of these is a 03:00 failure moved to a terminal
# ---------------------------------------------------------------------------


def test_a_graph_with_no_route_to_end_is_refused_at_compile_time() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    graph.add_node("b", completed)
    graph.add_edge("a", "b")
    graph.add_edge("b", "a")
    graph.set_entry("a")

    with pytest.raises(ValueError, match="no path from the entry node"):
        graph.compile()


def test_a_node_with_no_outgoing_edge_is_refused() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    graph.set_entry("a")
    with pytest.raises(ValueError, match="has no outgoing edge"):
        graph.compile()


def test_an_edge_to_an_unknown_node_is_refused() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    graph.add_edge("a", "typo")
    graph.set_entry("a")
    with pytest.raises(ValueError, match="routes to unknown node"):
        graph.compile()


def test_a_graph_refuses_to_replace_a_node_or_reuse_the_engines_namespace() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    with pytest.raises(ValueError, match="already in this graph"):
        graph.add_node("a", completed)
    with pytest.raises(ValueError, match="reserved"):
        graph.add_node(END, completed)
    with pytest.raises(ValueError, match="reserved"):
        graph.add_node("__resume__", completed)


def test_a_node_may_not_be_given_two_successor_rules() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    graph.add_edge("a", END)
    with pytest.raises(ValueError, match="already has an outgoing route"):
        graph.add_conditional_edges("a", lambda _state: "x", {"x": END})


def test_the_entry_point_may_not_be_moved() -> None:
    graph = Graph()
    graph.add_node("a", completed)
    graph.add_node("b", completed)
    graph.set_entry("a")
    with pytest.raises(ValueError, match="entry is already set"):
        graph.set_entry("b")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_every_node_transition_reaches_the_event_log(ctx: LoopContext) -> None:
    """Structural, not something each template author has to remember."""
    linear(("a", lambda _state: None), ("b", completed)).invoke(
        ctx, ctx.thread_id, state=fresh(ctx)
    )

    actions = [event["action"] for event in ctx.events.read(after=0, limit=100)]
    assert actions == ["a.enter", "a.exit", "b.enter", "b.exit"]


def test_a_park_is_logged_as_a_park_and_never_as_an_error(ctx: LoopContext) -> None:
    """Logging a park as an error is how an operator learns to ignore errors."""
    calls: Counter[str] = Counter()
    _parking_graph(calls).invoke(ctx, ctx.thread_id, state=fresh(ctx))

    actions = [event["action"] for event in ctx.events.read(after=0, limit=100)]
    assert "gate.parked" in actions
    assert "gate.error" not in actions


def test_a_crashing_node_is_logged_as_an_error(ctx: LoopContext) -> None:
    def boom(state: LoopState) -> Mapping[str, Any]:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        linear(("boom", boom)).invoke(ctx, ctx.thread_id, state=fresh(ctx))

    events = {event["action"]: event for event in ctx.events.read(after=0, limit=100)}
    assert "boom.error" in events
    assert events["boom.error"]["payload"]["error"] == "RuntimeError: nope"


def test_an_unwritable_event_log_is_surfaced_rather_than_swallowed_or_fatal(
    make_ctx: Callable[..., LoopContext],
) -> None:
    """A silently failing event log starves the learning pass, whose cursor it is.

    So the failure is folded into the durable ``log`` channel, where the next
    checkpoint carries it — and the tick still completes, because observability
    must not be able to fail a tick that did its work.
    """

    class BrokenEventLog:
        def append(self, event: Mapping[str, Any]) -> int:
            raise OSError("the log volume is full")

        def read(self, *, after: int = 0, limit: int = 500) -> list[Mapping[str, Any]]:
            return []

    ctx = make_ctx(events=BrokenEventLog())
    final = linear(("step", completed)).invoke(ctx, ctx.thread_id, state=fresh(ctx))

    assert final["status"] == LoopStatus.COMPLETED.value
    assert any("event log unavailable for step.enter" in line for line in final["log"])
