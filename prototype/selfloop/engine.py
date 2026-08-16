"""The durable executor: a hand-written state machine that replaces LangGraph.

Every graph the source system actually built was strictly
*sequential-with-branching*: each node had at most one successor, and every
conditional router returned exactly one target. Nothing fanned out, nothing
joined, nothing ran two branches concurrently. Under that shape a graph
library's "superstep" contains exactly one node, so its superstep durability
guarantee — *the state is persisted at every superstep boundary* — is
**exactly** equivalent to the one sentence this module implements instead:

    persist the state after each node returns, before the next node runs.

That is the whole of the durability story and the whole of the kill drill. A
process killed with ``SIGKILL`` at any instant either has a checkpoint naming
the node it was about to enter (so the next invocation enters there) or does not
(so that node runs again — which is why every external effect goes through an
attempt-keyed receipt rather than trusting the executor to run it once).

Four things this module refuses to do, each argued at its implementation site:
it does not swallow a node's exception (:meth:`CompiledGraph._drive`), does not
guess a terminal status (:meth:`CompiledGraph._finalise`), does not retry
anything it was not told is retryable (:data:`TRANSIENT_EXCEPTIONS`), and does
not raise :class:`ParkRequested` — it only ever catches it, because the one
raise site in this package is ``selfloop.kit`` and an AST test pins that.

At runtime this module imports :mod:`selfloop.contracts` and the standard
library, and nothing else; :class:`~selfloop.context.LoopContext` is imported
under ``TYPE_CHECKING`` for annotations only. The executor therefore sits at the
bottom of the package's import DAG and can be read, and tested, without pulling
in a single adapter.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol, cast, get_type_hints

from selfloop.contracts import (
    MAX_ATTEMPTS_CEILING,
    LoopError,
    LoopState,
    LoopStatus,
    RecursionExceeded,
    TransientLoopError,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only; see the module docstring
    from selfloop.context import LoopContext

# ---------------------------------------------------------------------------
# Constants and the retry allowlist
# ---------------------------------------------------------------------------

#: The terminal sentinel. Route here to end the tick.
#:
#: Spelled with dunders so it can never collide with a node name — :meth:`Graph.add_node`
#: refuses any name starting with ``__`` and reserves that namespace for the engine.
END: Final[str] = "__end__"

#: Reserved key inside the ``gates`` channel where a resumed park's verdict is
#: deposited. A node re-entered after a park finds
#: ``state["gates"]["__resume__"] == {"node": ..., "approval_id": ..., "verdict": ...}``.
#:
#: It is a HINT and never authority. The node that parked must re-read the
#: approvals store itself, exactly as the execution seam does — this channel
#: lives in a checkpoint, and a checkpoint is storage the package does not treat
#: as trusted.
RESUME_CHANNEL: Final[str] = "__resume__"

#: Event kind for the node transitions :func:`observed` writes.
NODE_EVENT_KIND: Final[str] = "loop.node"

#: Checkpoint envelope version. A checkpoint written by a different version of
#: this engine is REFUSED rather than best-effort parsed: resuming a half-finished
#: run against a schema you are guessing at is how a loop silently re-executes a
#: node it had already committed.
CHECKPOINT_VERSION: Final[int] = 1

#: The complete set of faults a node may be retried on, in-process, within one
#: tick. An allowlist and not a denylist, and the difference is safety-critical:
#: retrying :class:`~selfloop.contracts.EffectStateUnknown` is the duplicate
#: irreversible effect the whole receipt layer exists to prevent, retrying
#: :class:`~selfloop.contracts.EffectDenied` or
#: :class:`~selfloop.contracts.EffectNotApproved` is a policy-bypass attempt with
#: a loop for a driver, and retrying
#: :class:`~selfloop.contracts.BlockedLoopError` burns the tick on a dead
#: credential only a human can replace. None appear below, so all of them
#: propagate on the first attempt.
#:
#: Note what is deliberately ABSENT: bare ``OSError``. ``PermissionError``,
#: ``FileNotFoundError`` and ``IsADirectoryError`` are all ``OSError`` subclasses
#: and none of them get better by being tried again — a loop that retries a
#: permission error is a loop hammering a door it is not allowed through. Only
#: the two genuinely transient ``OSError`` subclasses are listed, alongside
#: ``ConnectionError`` (itself an ``OSError`` subclass) and ``TimeoutError``.
TRANSIENT_EXCEPTIONS: Final[tuple[type[Exception], ...]] = (
    TransientLoopError,
    ConnectionError,
    TimeoutError,
    BlockingIOError,
    InterruptedError,
)

#: A node function: it is handed the whole state and returns the channels it
#: wants to change, or ``None`` for "I changed nothing".
NodeFn = Callable[[LoopState], "Mapping[str, Any] | None"]

#: A conditional router: pure with respect to the world, and it returns a KEY
#: into the mapping supplied alongside it, never a node name directly.
RouterFn = Callable[[LoopState], str]


class ParkRequested(Exception):  # noqa: N818 - it is control flow, not an error
    """A node has stopped and is waiting for a human. **Not a failure.**

    Deliberately NOT a :class:`~selfloop.contracts.LoopError`. Every other
    exception this package defines means something went wrong; this one means the
    machinery worked exactly as designed and correctly declined to act without a
    person. Making it a ``LoopError`` would mean any handler written as
    ``except LoopError`` — of which this package and its templates have several —
    would swallow a park and convert "waiting for a human" into "an error
    occurred", which is both wrong and, at T2+, dangerous.

    The same argument is why there is exactly ONE raise site for this class in the
    whole package (``selfloop.kit``) and why ``tests/test_ast_invariants.py`` pins
    it: a park is a protocol between the effect gate and the executor, and a
    second raise site is a second protocol nobody wrote down. Template authors
    never raise it.

    The footgun to know about: a node whose body is wrapped in a broad
    ``except Exception`` will swallow this and the tick will report whatever that
    handler decided instead of parking. Do not write that handler.
    """

    def __init__(self, approval_id: str, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"parked awaiting approval {approval_id}")
        self.approval_id = approval_id
        self.payload: Mapping[str, Any] = MappingProxyType(dict(payload or {}))


# ---------------------------------------------------------------------------
# Reducer-aware state merge
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def channel_reducers() -> Mapping[str, Callable[[Any, Any], Any]]:
    """Per-channel merge functions, read off :class:`~selfloop.contracts.LoopState`.

    The reducers are declared where the schema is — as ``Annotated`` metadata on
    the ``TypedDict`` — and discovered here at first use rather than duplicated
    as a table in this module. That is the point: adding an accumulating channel
    to ``LoopState`` is a one-line edit to ``contracts.py`` and the executor picks
    it up, whereas a hand-maintained list in the executor is a list that goes
    stale silently and merges a new channel with the wrong semantics.

    A channel with no ``Annotated`` reducer has REPLACE semantics — the update
    wins outright. That is the right default for ``data`` (per-tick scratch),
    ``status`` and ``error``, and the wrong one for ``log`` and ``effects``, which
    is exactly why those two carry a capped-add reducer. Resolved lazily and
    cached, because this module does no work at import time.
    """
    hints = get_type_hints(LoopState, include_extras=True)
    reducers: dict[str, Callable[[Any, Any], Any]] = {}
    for channel, hint in hints.items():
        for meta in getattr(hint, "__metadata__", ()):
            if callable(meta):
                reducers[channel] = meta
                break
    return MappingProxyType(reducers)


def merge_state(state: Mapping[str, Any], update: Mapping[str, Any] | None) -> LoopState:
    """Apply *update* to *state*, honouring each channel's declared reducer.

    Returns a new mapping; neither argument is mutated. A node therefore cannot
    change the state of a tick by mutating the dict it was handed — it changes
    state only by returning an update, which is what makes the checkpoint written
    afterwards a complete description of what the node did.
    """
    reducers = channel_reducers()
    merged: dict[str, Any] = dict(state)
    for channel, value in (update or {}).items():
        reducer = reducers.get(channel)
        if reducer is None:
            merged[channel] = value
        else:
            merged[channel] = reducer(merged.get(channel) or [], value)
    return cast(LoopState, merged)


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def emit(ctx: LoopContext, kind: str, action: str, payload: Mapping[str, Any] | None = None) -> int:
    """Append one event to the :class:`~selfloop.ports.EventLog`; return its cursor.

    Returns ``0`` when the append failed. The caller is expected to make that
    visible rather than to shrug at it — see :func:`observed`.
    """
    return int(
        ctx.events.append(
            {
                "kind": kind,
                "action": action,
                "actor": ctx.actor,
                "instance_id": ctx.instance_id,
                "template": ctx.template,
                "at": ctx.clock.now_iso(),
                "payload": dict(payload or {}),
            }
        )
    )


def observed(ctx: LoopContext, name: str, fn: NodeFn) -> NodeFn:
    """Wrap a node so that entering, leaving, parking and failing all land on the log.

    Applied by :meth:`CompiledGraph.invoke` to every node of every template, so
    observability is structural rather than something each template author has to
    remember. **Templates must not wrap again** — a doubly-wrapped node emits
    every transition twice, and the learning pass reads that log.

    Four actions, and the third one is the reason this exists as a function rather
    than a ``try/except`` inline: ``<node>.parked`` is emitted for
    :class:`ParkRequested` and ``<node>.error`` is not, because a park is the
    machinery working and logging it as an error is how an operator learns to
    ignore the error channel.

    An event log that cannot be written is not allowed to kill the tick — but it
    is also not allowed to disappear. A silently failing event log starves the
    learning pass (its cursor comes from this log), which is precisely the failure
    mode this package exists to refuse, so the failure is folded into the durable
    ``log`` channel of the state the node returns, where the next checkpoint
    carries it and an operator can see it.
    """

    def node(state: LoopState) -> Mapping[str, Any] | None:
        complaints: list[str] = []

        def record(action: str, payload: Mapping[str, Any] | None = None) -> None:
            try:
                emit(ctx, NODE_EVENT_KIND, action, payload)
            except Exception as exc:  # noqa: BLE001 - see the docstring: surfaced, not swallowed
                complaints.append(
                    f"event log unavailable for {action}: {type(exc).__name__}: {exc}"
                )

        record(f"{name}.enter", {"tick": state.get("tick", 0)})
        try:
            update = fn(state)
        except ParkRequested as park:
            record(f"{name}.parked", {"approval_id": park.approval_id})
            raise
        except Exception as exc:
            record(f"{name}.error", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        record(f"{name}.exit", {})
        if not complaints:
            return update
        surfaced: dict[str, Any] = dict(update or {})
        surfaced["log"] = [*(surfaced.get("log") or []), *complaints]
        return surfaced

    node.__name__ = f"observed_{name}"
    return node


# ---------------------------------------------------------------------------
# The graph description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSpec:
    """One node: its name, its callable, and how many times it may be attempted."""

    name: str
    fn: NodeFn
    retries: int = 1


class Graph:
    """A description of a sequential-with-branching loop. Builds nothing until compiled.

    Deliberately dumb: it holds nodes and edges and validates their shape. Every
    interesting property of this package — the effect gate that must precede an
    effect node, the receipt around a tool call, the approval a T2 send parks on —
    lives in ``selfloop.kit``, which assembles graphs out of this. Keeping the two
    apart is what makes the executor readable in one sitting.
    """

    def __init__(self) -> None:
        #: Public, and public on purpose: ``selfloop.kit`` checks membership
        #: (``if PARK in graph.nodes``) so it can add the shared terminal node
        #: exactly once per graph.
        self.nodes: dict[str, NodeSpec] = {}
        self.edges: dict[str, str] = {}
        self.conditionals: dict[str, tuple[RouterFn, dict[str, str]]] = {}
        self.entry: str = ""

    def add_node(self, name: str, fn: NodeFn, retries: int = 1) -> str:
        """Add a node and return its name, so builders can chain on it.

        *retries* is the TOTAL number of in-process attempts, and it applies only
        to the faults in :data:`TRANSIENT_EXCEPTIONS`. A node that performs an
        external effect should leave it at 1 unless the tool has declared
        ``replay_on_unknown``: the durable retry for an effect is the next tick in
        the next process, arbitrated by the receipt, not a loop inside this one.
        """
        if not name or not isinstance(name, str):
            raise ValueError("a node needs a non-empty name; it is the checkpoint's resume key")
        if name == END or name.startswith("__"):
            raise ValueError(
                f"node name {name!r} is reserved: the engine owns the '__' namespace "
                f"(END is {END!r}, resumed parks land in gates[{RESUME_CHANNEL!r}])"
            )
        if name in self.nodes:
            raise ValueError(
                f"node {name!r} is already in this graph; replacing a node silently "
                "would let a later builder call change what an edge points at"
            )
        if not callable(fn):
            raise ValueError(f"node {name!r}: fn must be callable")
        if not 1 <= int(retries) <= MAX_ATTEMPTS_CEILING:
            raise ValueError(
                f"node {name!r}: retries must be between 1 and {MAX_ATTEMPTS_CEILING} "
                f"(got {retries!r}) — an unbounded in-process retry is an outage "
                "generator, not a repair"
            )
        self.nodes[name] = NodeSpec(name=name, fn=fn, retries=int(retries))
        return name

    def add_edge(self, source: str, target: str) -> None:
        """Route unconditionally from *source* to *target* (a node name, or :data:`END`)."""
        self._require_node(source, "an edge source")
        self._require_free(source)
        self.edges[source] = target

    def add_conditional_edges(
        self, source: str, router: RouterFn, mapping: Mapping[str, str]
    ) -> None:
        """Route from *source* by asking *router* for a KEY into *mapping*.

        The indirection through a mapping is not ceremony. It means the set of
        destinations a node can reach is a static, inspectable property of the
        graph — which is what lets :meth:`compile` prove that every branch lands
        somewhere real and that :data:`END` is reachable at all — while a router
        that returned node names directly would make routing a runtime surprise.

        A router that returns a key outside *mapping* fails the tick loudly rather
        than falling through to a default, because the fall-through target of a
        misrouted branch is always the happy path.
        """
        self._require_node(source, "a conditional-edge source")
        self._require_free(source)
        if not callable(router):
            raise ValueError(f"conditional edges from {source!r}: router must be callable")
        if not mapping:
            raise ValueError(
                f"conditional edges from {source!r} need a non-empty mapping; "
                "a router with nowhere to go cannot terminate the tick"
            )
        self.conditionals[source] = (router, {str(k): str(v) for k, v in mapping.items()})

    def set_entry(self, name: str) -> None:
        """Name the node a FRESH tick starts at. Resumes ignore it and enter mid-graph."""
        self._require_node(name, "the entry point")
        if self.entry and self.entry != name:
            raise ValueError(
                f"entry is already set to {self.entry!r}; refusing to move it to {name!r} "
                "— two set_entry calls in one builder is a copy-paste, not an intent"
            )
        self.entry = name

    def compile(
        self, *, max_steps: int | None = None, retry_backoff_s: float = 0.0
    ) -> CompiledGraph:
        """Validate the whole shape, then freeze it into an executable graph.

        Everything checked here is checked ONCE, at build time, in the developer's
        terminal — because the alternative is discovering that a template lost its
        only edge to :data:`END` at 03:00, when the symptom is a
        :class:`~selfloop.contracts.RecursionExceeded` that looks like a runaway
        loop rather than like a missing line.

        *max_steps* is an optional per-template ceiling, for a template that knows
        its own bound (a propose/evaluate cycle derives it from ``max_rounds`` so
        that exhaustion renders a legible abort instead of an opaque engine
        error). When both it and ``ctx.max_steps`` are set, the STRICTER wins.

        *retry_backoff_s* defaults to zero and that is a considered default: this
        package runs one tick per process, so the real backoff for a transient
        fault is the next scheduled invocation, minutes away, with a fresh
        process. An in-process sleep holds the instance's lease while it waits.
        """
        if not self.entry:
            raise ValueError("this graph has no entry node; call set_entry() before compile()")
        if not self.nodes:
            raise ValueError("this graph has no nodes")

        targets: dict[str, set[str]] = {}
        for name in self.nodes:
            if name in self.edges:
                targets[name] = {self.edges[name]}
            elif name in self.conditionals:
                targets[name] = set(self.conditionals[name][1].values())
            else:
                raise ValueError(
                    f"node {name!r} has no outgoing edge; every node must route "
                    f"somewhere, and a node that means 'stop' routes to END ({END!r})"
                )
        for name, reachable in targets.items():
            unknown = sorted(t for t in reachable if t != END and t not in self.nodes)
            if unknown:
                raise ValueError(
                    f"node {name!r} routes to unknown node(s) {unknown}; "
                    f"known nodes are {sorted(self.nodes)}"
                )

        seen: set[str] = set()
        frontier = [self.entry]
        while frontier:
            current = frontier.pop()
            if current in seen or current == END:
                continue
            seen.add(current)
            frontier.extend(targets.get(current, ()))
        if END not in {t for name in seen for t in targets.get(name, ())}:
            raise ValueError(
                f"no path from the entry node {self.entry!r} reaches END; every tick "
                "of this template would run until it exhausted max_steps"
            )

        return CompiledGraph(
            nodes=dict(self.nodes),
            edges=dict(self.edges),
            conditionals=dict(self.conditionals),
            entry=self.entry,
            max_steps=max_steps,
            retry_backoff_s=float(retry_backoff_s),
        )

    def _require_node(self, name: str, role: str) -> None:
        if name not in self.nodes:
            raise ValueError(
                f"{role} must be a node already added to this graph; {name!r} is not "
                f"(known nodes: {sorted(self.nodes)})"
            )

    def _require_free(self, source: str) -> None:
        if source in self.edges or source in self.conditionals:
            raise ValueError(
                f"node {source!r} already has an outgoing route; a node in a "
                "sequential graph has exactly one successor rule, and adding a "
                "second one silently discards the first"
            )


# ---------------------------------------------------------------------------
# The checkpoint, seen from the outside
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """What the durable checkpoint says about one thread, before anything runs.

    The three entry states of a tick are read off THIS and nothing else — not off
    a flag the caller passed, not off a row in another store. That is what makes a
    tick's behaviour a function of what actually survived the last process.
    """

    thread_id: str
    state: LoopState = field(default_factory=lambda: cast(LoopState, {}))
    #: The node the next invocation must enter. Empty when the last tick finished.
    next_node: str = ""
    #: Non-empty when the last tick stopped for a human. Equals ``next_node``.
    parked_at: str = ""
    approval_id: str = ""
    park_payload: Mapping[str, Any] = field(default_factory=dict)
    #: The fault that ended the last invocation, if one did. Envelope-level rather
    #: than a state channel, so that a crash cannot overwrite the template's own
    #: account of what it decided before the crash happened.
    error: str = ""
    tick: int = 0
    #: Nodes already executed in the RUN that is still in flight, and the path they
    #: took. Both survive a crash on purpose: ``max_steps`` bounds one tick, and a
    #: tick that resumed after a crash is still the same tick. Resetting the budget
    #: on resume would let a template that crashes mid-cycle churn forever, one
    #: process at a time, with every individual invocation looking well-behaved.
    steps: int = 0
    path: tuple[str, ...] = ()

    @property
    def parked(self) -> bool:
        """Waiting for a human. Nothing may be invoked until an approval resolves."""
        return bool(self.parked_at)

    @property
    def mid_run(self) -> bool:
        """A previous process died between two nodes. Resume AT :attr:`next_node`."""
        return bool(self.next_node) and not self.parked_at

    @property
    def idle(self) -> bool:
        """The last tick finished. The next one is fresh, on the same thread."""
        return not self.next_node and not self.parked_at


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledGraph:
    """An executable, immutable graph. One :meth:`invoke` call is one loop tick."""

    nodes: Mapping[str, NodeSpec]
    edges: Mapping[str, str]
    conditionals: Mapping[str, tuple[RouterFn, dict[str, str]]]
    entry: str
    max_steps: int | None = None
    retry_backoff_s: float = 0.0

    # -- reading the checkpoint --------------------------------------------

    def snapshot(self, ctx: LoopContext, thread_id: str) -> Snapshot:
        """Load the durable checkpoint for *thread_id* and describe it.

        A caller that needs to resolve an approval before resuming (the runtime
        does) reads the park's ``approval_id`` from here rather than by parsing the
        checkpoint itself. The envelope's shape is this module's business and
        nobody else's.
        """
        raw = ctx.checkpoints.load(thread_id)
        if not raw:
            return Snapshot(thread_id=thread_id)
        version = int(raw.get("version") or 0)
        if version != CHECKPOINT_VERSION:
            raise LoopError(
                f"checkpoint for thread {thread_id!r} is version {version}, this engine "
                f"writes version {CHECKPOINT_VERSION}; refusing to resume a half-finished "
                "run against a schema it is guessing at. Inspect it, then clear it with "
                "CheckpointStore.drop() to start the next tick fresh."
            )
        state = cast(LoopState, dict(raw.get("state") or {}))
        parked_at = str(raw.get("parked_at") or "")
        return Snapshot(
            thread_id=thread_id,
            state=state,
            next_node=str(raw.get("next_node") or ""),
            parked_at=parked_at,
            approval_id=str(raw.get("approval_id") or ""),
            park_payload=dict(raw.get("park_payload") or {}),
            error=str(raw.get("error") or ""),
            tick=int(raw.get("tick") or state.get("tick") or 0),
            steps=int(raw.get("steps") or 0),
            path=tuple(str(n) for n in (raw.get("path") or ())),
        )

    # -- running one tick ---------------------------------------------------

    def invoke(
        self,
        ctx: LoopContext,
        thread_id: str,
        *,
        state: LoopState | None = None,
        resume: Mapping[str, Any] | None = None,
    ) -> LoopState:
        """Advance *thread_id* by one tick and return the state it ended in.

        Three entry states, resolved from the checkpoint alone:

        **(a) Parked.** The last tick stopped for a human. With no *resume* this
        returns the parked state immediately and **invokes nothing** — not the
        parked node, not the entry node. That matters more than it looks: the
        alternative reading ("nothing is running, so start a fresh tick") would
        run the pre-park nodes again on every scheduled invocation for as long as
        the approval sat undecided, re-drafting, re-fetching and re-charging the
        world once per tick while presenting as a well-behaved parked loop.

        **(b) Mid-run.** A previous process died between two nodes. Execution
        resumes AT the recorded next node — the nodes before it are not replayed,
        because their effects are already in the checkpoint this is being read
        from.

        **(c) Idle.** The last tick finished. *state* (from
        :func:`~selfloop.contracts.initial_state`) starts a fresh tick on the same
        thread at ``tick + 1``. It is merged ONTO the surviving checkpoint through
        the channel reducers, which is how ``memo`` — absent from ``initial_state``
        — carries across ticks while ``data`` is replaced with a clean slate.

        *resume* is supplied by the caller once it has established that the park's
        approval is decided. It is deposited under ``gates["__resume__"]`` and the
        parked node is re-entered from the top. The node re-reads the approvals
        store itself; this value is a hint that a decision exists, never authority
        to act on it.
        """
        snap = self.snapshot(ctx, thread_id)

        if snap.parked and resume is None:
            return snap.state

        if snap.parked:
            gates = dict(snap.state.get("gates") or {})
            gates[RESUME_CHANNEL] = {
                "node": snap.parked_at,
                "approval_id": snap.approval_id,
                "verdict": dict(resume or {}),
            }
            current = merge_state(
                snap.state,
                {
                    # RUNNING, not PARKED: a resumed tick that finishes without any
                    # node stamping a status must fail closed to FAILED, and leaving
                    # the stale PARKED here would instead render it as a neutral
                    # non-result — an unattended loop reporting "waiting for a human"
                    # about a human who already answered.
                    "status": LoopStatus.RUNNING.value,
                    "error": None,
                    "gates": gates,
                    "log": [f"resumed at {snap.parked_at} on approval {snap.approval_id}"],
                },
            )
            node, steps, path = snap.parked_at, snap.steps, snap.path
        elif snap.mid_run:
            current, node, steps, path = snap.state, snap.next_node, snap.steps, snap.path
        else:
            if state is None:
                raise LoopError(
                    f"thread {thread_id!r} has nothing in flight and no fresh state was "
                    "supplied; pass state=initial_state(...) to start a tick"
                )
            current = merge_state(snap.state, dict(state))
            current["tick"] = snap.tick + 1
            node, steps, path = self.entry, 0, ()

        return self._drive(ctx, thread_id, current, node, steps, path)

    def _drive(
        self,
        ctx: LoopContext,
        thread_id: str,
        current: LoopState,
        node: str,
        steps: int,
        path: tuple[str, ...],
    ) -> LoopState:
        """Run nodes until END, a park, an exhausted budget, or a fault."""
        budget = self._budget(ctx)
        while node != END:
            if steps >= budget:
                self._exhausted(ctx, thread_id, current, node, steps, path, budget)
            spec = self.nodes.get(node)
            if spec is None:
                raise LoopError(
                    f"checkpoint for thread {thread_id!r} resumes at node {node!r}, which "
                    f"this template does not have (it has {sorted(self.nodes)}). The usual "
                    "cause is a template renamed under a live instance; clear the thread "
                    "with CheckpointStore.drop() once you know what the in-flight run was."
                )
            steps += 1
            path = (*path, node)
            try:
                update = self._attempt(ctx, spec, current)
                current = merge_state(current, self._checked(spec.name, update))
                nxt = self._route(spec.name, current)
            except ParkRequested as park:
                current = merge_state(
                    current,
                    {
                        "status": LoopStatus.PARKED.value,
                        "log": [f"{spec.name}: parked on approval {park.approval_id}"],
                    },
                )
                self._save(
                    ctx,
                    thread_id,
                    current,
                    next_node=spec.name,
                    parked_at=spec.name,
                    approval_id=park.approval_id,
                    park_payload=park.payload,
                    steps=steps,
                    path=path,
                )
                return current
            except Exception as exc:  # noqa: BLE001 - recorder, not handler: it re-raises
                # Not a handler. The failure is made durable so an operator reading
                # the checkpoint sees WHICH node died and why, and so the next
                # invocation resumes at that node rather than silently restarting
                # the tick from its entry — and then the exception continues on its
                # way, traceback intact, for the caller to render as FAILED.
                #
                # Note what is NOT touched: the state's own ``status`` and ``error``
                # channels. Those belong to the template, and a node that had
                # already stamped ABORTED before a later node crashed must still
                # read ABORTED when the tick resumes. The crash is recorded in the
                # checkpoint ENVELOPE and as one line in the durable ``log``, which
                # is informational, rather than by overwriting the template's own
                # account of what it decided.
                fault = f"{spec.name}: {type(exc).__name__}: {exc}"
                self._save(
                    ctx,
                    thread_id,
                    merge_state(current, {"log": [f"fault at {fault}"]}),
                    next_node=spec.name,
                    steps=steps,
                    path=path,
                    error=fault,
                )
                raise
            # THE kill-drill invariant, and the whole reason this module exists:
            # the checkpoint naming the NEXT node is durable before that node runs.
            # A SIGKILL one instruction later resumes at `nxt`; a SIGKILL one
            # instruction earlier re-runs `spec.name`, which is why every external
            # effect is receipted rather than trusted to run once.
            self._save(ctx, thread_id, current, next_node=nxt, steps=steps, path=path)
            node = nxt

        current = self._finalise(current)
        self._save(ctx, thread_id, current, next_node="", steps=0, path=())
        return current

    def _attempt(
        self, ctx: LoopContext, spec: NodeSpec, state: LoopState
    ) -> Mapping[str, Any] | None:
        """Run one node, retrying only the faults in :data:`TRANSIENT_EXCEPTIONS`."""
        node = observed(ctx, spec.name, spec.fn)
        attempt = 0
        while True:
            attempt += 1
            try:
                return node(state)
            except TRANSIENT_EXCEPTIONS:
                if attempt >= spec.retries:
                    raise
                if self.retry_backoff_s > 0:
                    time.sleep(self.retry_backoff_s * attempt)

    def _route(self, node: str, state: LoopState) -> str:
        """The single successor of *node*, given the state it just produced."""
        target = self.edges.get(node)
        if target is not None:
            return target
        router, mapping = self.conditionals[node]
        key = str(router(state))
        try:
            return mapping[key]
        except KeyError:
            raise LoopError(
                f"the router on node {node!r} returned {key!r}, which is not one of its "
                f"declared branches {sorted(mapping)}. A branch this graph cannot name "
                "is a branch nobody reviewed, so the tick stops here rather than "
                "falling through to whichever destination happens to be first."
            ) from None

    @staticmethod
    def _checked(node: str, update: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """A node returns channel updates or ``None``. Anything else is a template bug."""
        if update is None:
            return {}
        if isinstance(update, Mapping):
            return update
        raise LoopError(
            f"node {node!r} returned {type(update).__name__}; a node returns a mapping of "
            "channel updates, or None for 'I changed nothing'. Returning the state "
            "itself, or a bare value, silently discards every other channel."
        )

    def _budget(self, ctx: LoopContext) -> int:
        """Nodes this tick may execute. The STRICTER of the context's and the graph's."""
        if self.max_steps is None:
            return int(ctx.max_steps)
        return min(int(ctx.max_steps), int(self.max_steps))

    def _exhausted(
        self,
        ctx: LoopContext,
        thread_id: str,
        current: LoopState,
        node: str,
        steps: int,
        path: tuple[str, ...],
        budget: int,
    ) -> NoReturn:
        """Terminate a runaway tick legibly, and never leave the thread bricked.

        The predecessor raised a graph library's ``GraphRecursionError``, whose
        message told an operator that a loop had misbehaved without telling them
        where — and the answer was always in the cycle the loop had been going
        round, which the exception did not carry.

        Two things happen before the raise, in this order and for different
        reasons. The state is settled as ``ABORTED`` with the cycle written into
        ``error``, because a tick that ran out of budget produced no result and
        must count against the acceptance floor. And the checkpoint is written
        TERMINAL — no next node, budget reset — because the alternative is a thread
        that resumes into the same exhausted cycle on every future invocation and
        can never start a fresh tick again: a permanent brick, healed only by an
        operator who knows to drop the checkpoint.
        """
        cycle = _describe_cycle((*path, node))
        detail = (
            f"tick exceeded its budget of {budget} nodes at {node!r}; the path was cycling: {cycle}"
        )
        settled = merge_state(
            current,
            {"status": LoopStatus.ABORTED.value, "error": detail, "log": [detail]},
        )
        self._save(ctx, thread_id, settled, next_node="", steps=0, path=())
        raise RecursionExceeded(detail, steps=steps, max_steps=budget, path=(*path, node))

    @staticmethod
    def _finalise(state: LoopState) -> LoopState:
        """Stamp a terminal status, failing CLOSED on absence and on ``running``.

        Two cases collapse to ``FAILED`` here and neither is hypothetical. A status
        this package does not recognise means the template's vocabulary has drifted
        from the runtime's — after a rename, a deploy, or a hand-edited
        checkpoint — and a runtime that cannot classify a tick cannot vouch for it.
        A status still reading ``running`` at END means no node on the path taken
        stamped anything, which is the unhandled-branch case: the template reached
        its terminal node through a route its author did not think about.

        The predecessor defaulted both to ``COMPLETED``. That is the most
        favourable possible outcome placed at the exact point where the loop grades
        itself, and it is the single line most worth getting right in this file.
        """
        raw = str(state.get("status") or "")
        try:
            status = LoopStatus(raw)
        except ValueError:
            detail = f"tick produced no usable terminal status ({raw!r}) — failing closed"
            return merge_state(
                state,
                {
                    "status": LoopStatus.FAILED.value,
                    "error": state.get("error") or detail,
                    "log": [detail],
                },
            )
        if status is not LoopStatus.RUNNING:
            return state
        detail = "tick reached the end still marked 'running' — no node stamped a terminal status"
        return merge_state(
            state,
            {
                "status": LoopStatus.FAILED.value,
                "error": state.get("error") or detail,
                "log": [detail],
            },
        )

    def _save(
        self,
        ctx: LoopContext,
        thread_id: str,
        state: LoopState,
        *,
        next_node: str,
        steps: int,
        path: tuple[str, ...],
        parked_at: str = "",
        approval_id: str = "",
        park_payload: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """Write the checkpoint. It MUST be durable before this returns.

        Every property this executor claims rests on that one obligation, which is
        why it is stated on :class:`~selfloop.ports.CheckpointStore` as well: a
        store that buffers, queues, or leaves the write to the operating system has
        not saved anything, and the kill drill — SIGKILL the process, restart it,
        assert the effect happened exactly once — is precisely the test that tells
        the difference. There is nothing this module can do to verify it; an
        adapter that lies here breaks resume in a way no single-process test will
        show.
        """
        ctx.checkpoints.save(
            thread_id,
            {
                "version": CHECKPOINT_VERSION,
                "thread_id": thread_id,
                "state": dict(state),
                "next_node": next_node,
                "parked_at": parked_at,
                "approval_id": approval_id,
                "park_payload": dict(park_payload or {}),
                "error": error,
                "tick": int(state.get("tick") or 0),
                "steps": int(steps),
                "path": list(path),
                "updated_at": ctx.clock.now_iso(),
            },
        )


def _describe_cycle(path: Sequence[str]) -> str:
    """Render the repeating segment of *path*, plus how often each node ran.

    An operator reading ``propose -> evaluate -> propose (propose x17,
    evaluate x16)`` knows immediately which branch never converged. An operator
    reading ``recursion limit reached`` knows only that something went wrong, and
    has to reconstruct the path from the event log to find out what.
    """
    if not path:
        return "no nodes were executed"
    counts = Counter(path)
    tally = ", ".join(f"{name} x{n}" for name, n in counts.most_common(4) if n > 1)
    last = path[-1]
    segment = list(path[-8:])
    for index in range(len(path) - 2, -1, -1):
        if path[index] == last:
            segment = list(path[index:])
            break
    rendered = " -> ".join(segment)
    return f"{rendered} ({tally})" if tally else rendered


# ---------------------------------------------------------------------------
# The seam a different executor would implement
# ---------------------------------------------------------------------------


class ExecutorPort(Protocol):
    """What the runtime needs from an executor — the seam a substitute plugs into.

    :class:`CompiledGraph` is the shipped implementation and the only one the core
    package imports. The Protocol exists so that a LangGraph-backed executor can
    be dropped in without touching ``selfloop.runtime``: it would build a
    ``StateGraph`` from the same :class:`Graph` description, compile it with a
    ``SqliteSaver``, and translate :class:`ParkRequested` to ``interrupt()`` and
    the ``resume`` argument to ``Command(resume=...)``. That translation is
    mechanical precisely because both sides agree on the three entry states
    described on :meth:`CompiledGraph.invoke`.

    A substitute owes the same two guarantees, neither of which the signatures can
    express: the checkpoint is durable before the next node runs, and a tick that
    ends without a recognised terminal status is recorded as ``FAILED``.
    """

    def snapshot(self, ctx: LoopContext, thread_id: str) -> Snapshot:
        """Describe the durable checkpoint without running anything."""
        ...

    def invoke(
        self,
        ctx: LoopContext,
        thread_id: str,
        *,
        state: LoopState | None = None,
        resume: Mapping[str, Any] | None = None,
    ) -> LoopState:
        """Advance one tick. See :meth:`CompiledGraph.invoke` for the entry states."""
        ...


__all__ = [
    "CHECKPOINT_VERSION",
    "END",
    "NODE_EVENT_KIND",
    "RESUME_CHANNEL",
    "TRANSIENT_EXCEPTIONS",
    "CompiledGraph",
    "ExecutorPort",
    "Graph",
    "NodeFn",
    "NodeSpec",
    "ParkRequested",
    "RouterFn",
    "Snapshot",
    "channel_reducers",
    "emit",
    "merge_state",
    "observed",
]
