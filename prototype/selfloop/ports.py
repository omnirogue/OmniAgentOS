"""The twelve host seams, as ``typing.Protocol`` — declarations only, no code.

A developer who reads only this file knows exactly what they must supply to run
``selfloop`` against their own storage, their own policy and their own gate. That
is the point of keeping all twelve in one place: the alternative, a protocol
defined next to each consumer, means the answer to "what do I have to implement?"
is spread across nine modules and is never complete.

**Read the docstrings as obligations, not as commentary.** For most of these
ports the guarantee lives in the semantics and not in the signature — a
``claim()`` that returns ``bool`` is trivially satisfiable by ``return True``, and
an adapter that does so has silently deleted the exactly-once property while
still type-checking. Every obligation stated below is one an adapter MUST honour;
where a real system has been burned by an adapter that did not, the docstring
says which one and how.

These Protocols are deliberately *not* ``runtime_checkable``. Several carry
attributes as well as methods, which ``isinstance`` cannot check anyway, and a
half-working structural check invites callers to treat a passing ``isinstance``
as proof that the obligations above are met. They are not. Verify an adapter with
tests, which is what ``tests/conftest.py`` runs every suite against both shipped
adapters for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol

from selfloop.contracts import (
    ActionClass,
    GateReceipt,
    GateSpec,
    LearningSignal,
    PolicyDecision,
)

if TYPE_CHECKING:  # pragma: no cover - import-cycle avoidance, not runtime behaviour
    from selfloop.context import LoopContext


class Clock(Protocol):
    """Two clocks, deliberately separate, because conflating them forged evidence.

    ``now_iso()`` is the RECORD STAMP. It is what goes on a row, and a caller may
    legitimately pin it — a backfill that stamps yesterday's events with today's
    time has falsified an audit trail.

    ``elapsed()`` reads a physical monotonic source and is the ONLY thing any
    freshness, age or anti-forgery check may read. It is never pinned and never
    steps.

    The bug that separated them: a gate slower than the anti-forgery window read
    as future-dated against a wall clock that had been adjusted mid-run, and a
    PASSING gate settled adverse. That defect had zero test coverage anywhere
    before it shipped, because every test used a wall clock that behaved.
    """

    def now_iso(self) -> str:
        """ISO-8601 record stamp. May be pinned by a caller for a backfill."""
        ...

    def elapsed(self) -> float:
        """Monotonic seconds from an arbitrary origin. Never pinned, never steps back."""
        ...


class ReceiptStore(Protocol):
    """Exactly-once bookkeeping for external effects: claim, act, complete.

    Three of these four methods carry their entire guarantee in their SEMANTICS,
    so they are stated here as obligations rather than left to the signature:

    * ``claim()`` is INSERT-OR-IGNORE. It returns "did I win the race", not "does
      a row exist". A caller that loses MUST fail closed — it must not proceed on
      the theory that the winner will probably succeed.
    * ``release()`` MUST be a no-op once a result has been recorded, and that
      must be enforced in the BACKEND (``... AND result_json IS NULL``), not by
      the caller checking first. This is the crash-window guarantee, and a caller
      must not be able to talk the store out of it — including a caller that is
      confused, buggy, or a future refactor of this package.
    * ``complete()`` MUST be durable before it returns. Not queued, not buffered,
      not "the OS will get to it". That obligation is the entire content of the
      kill drill: a process that dies immediately after ``complete()`` returns
      must find the completed row when it restarts.

    Keys are attempt-scoped by the caller (``<key>``, ``<key>#a2``, ...), so a
    retry never re-opens a row back into ``claimed`` — a re-opened claim is
    indistinguishable from the crash window, and a TTL here would be the
    double-billing bug with a delay in front of it. A timer observes nothing.
    """

    def claim(self, key: str, *, instance_id: str, node: str, at: str) -> bool:
        """Insert a claim row. True only if THIS caller created it."""
        ...

    def get(self, key: str) -> Mapping[str, Any] | None:
        """The row, or None. Never invents a row that was not written."""
        ...

    def complete(self, key: str, *, envelope_json: str, at: str) -> None:
        """Record this attempt's terminal outcome. Durable before returning."""
        ...

    def release(self, key: str) -> bool:
        """Drop a claim that provably produced no effect. No-op once a result exists."""
        ...


class ApprovalStore(Protocol):
    """The park/approve rows: create once, read many, decide exactly once.

    ``get()`` by id is the method whose absence made the predecessor
    unportable — it reached into the host's database connection with a raw
    ``SELECT`` because the host's data layer never exposed a by-id read, and that
    one line pinned the whole loop runtime to one schema.

    ``decide()`` MUST be a compare-and-set on ``state == 'pending'`` and MUST
    return False rather than raise when it loses. Two workers, or a worker and an
    operator at a CLI, can decide the same row concurrently; without the CAS both
    "succeed" and the second silently overwrites the first, so an approval can
    overwrite a rejection.

    ``create()`` returns False when the row already existed. That is the normal
    path, not an error: approval ids are deterministic so that a replayed tick
    re-derives the same id, finds the row, and pages nobody. One row, one page,
    ever.
    """

    def get(self, approval_id: str) -> Mapping[str, Any] | None:
        """The row, or None. Writes nothing — a read must never mint a row."""
        ...

    def create(self, row: Mapping[str, Any]) -> bool:
        """Insert if absent. False means it already existed, which is not an error."""
        ...

    def decide(self, approval_id: str, *, state: str, by: str, note: str, at: str) -> bool:
        """Compare-and-set from pending. False when another decider won the race."""
        ...


class RecordStore(Protocol):
    """ONE generic durable store for every non-receipt record.

    Lessons, lesson-uses, outcomes, signals, evidence, reconciliations — all of
    them are ``(kind, id, payload)``. Making it kind-generic rather than one port
    per record type is exactly what makes "add a new learning signal" a
    zero-port, zero-schema, zero-migration edit.

    The four methods are four distinct semantics, and picking the wrong one is a
    correctness bug rather than a style choice:

    * ``put_once`` is HISTORY. A run must not be able to overwrite its own report
      card. Insert-if-absent; False means a record already exists and the caller
      must treat the existing one as authoritative.
    * ``put_latest`` is a CACHE. A fresher green must be able to supersede a
      stale one. Last write wins per slot. "Append-only" is not one policy, and a
      store that offers only one of these is wrong for half its records.
    * ``query(kind, **equals)`` is equality-only, on purpose: it keeps this port
      at five methods and the in-memory adapter trivial. Attribution over-fetches
      and filters in Python, which is correct at prototype scale and wrong at any
      real scale — a production backend should index ``(kind, scope, cursor)``.
    * ``transition`` is a COMPARE-AND-SET, and it is the method that makes lesson
      state safe. Promote, retire, append-evidence and counter updates all race:
      the runtime's learning pass runs unattended while an operator runs the CLI.
      Without a CAS, a lesson retired for regression can be resurrected to
      promoted by a concurrent write that read the row a moment earlier.

    An adapter MUST implement ``transition`` atomically with respect to other
    writers of the same ``(kind, record_id)``. Read-modify-write in Python
    without a lock or a conditional UPDATE does not satisfy this.
    """

    def put_once(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> bool:
        """Insert if absent. False when a record for this id already exists."""
        ...

    def put_latest(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> None:
        """Write, replacing any previous payload for this id."""
        ...

    def get(self, kind: str, record_id: str) -> Mapping[str, Any] | None:
        """The payload, or None."""
        ...

    def query(self, kind: str, /, **equals: Any) -> list[Mapping[str, Any]]:
        """Every payload of *kind* whose fields equal all the given values."""
        ...

    def transition(
        self,
        kind: str,
        record_id: str,
        *,
        expect: Mapping[str, Any],
        set: Mapping[str, Any],  # noqa: A002 - the name IS the contract; callers read as SQL
    ) -> bool:
        """Atomically apply *set* only if every field in *expect* still matches.

        False means another writer moved the row first. The caller must re-read
        and decide again, never retry blindly.
        """
        ...


class EventLog(Protocol):
    """The ordered replay cursor, and the only port whose RETURN VALUE is the point.

    ``append()`` returns a MONOTONIC INTEGER, and that integer is the cursor.
    "Extract signals since cursor N" is what makes the learning pass exactly-once
    and re-runnable, and it is why the cursor may not be a timestamp: two events
    written in the same millisecond are unordered, so a time window either
    re-mines them or drops them, and a clock that steps backwards silently
    re-mines an arbitrary stretch of history as if it were new evidence.

    Monotonic means strictly increasing across the life of the log, including
    across processes and restarts. An adapter that restarts numbering, or that
    can hand the same integer to two writers, has broken the learning pass in a
    way no test of a single process will show.
    """

    def append(self, event: Mapping[str, Any]) -> int:
        """Append one event; return its cursor. Strictly increasing, forever."""
        ...

    def read(self, *, after: int = 0, limit: int = 500) -> list[Mapping[str, Any]]:
        """Events with a cursor strictly greater than *after*, in cursor order."""
        ...


class CheckpointStore(Protocol):
    """The durability seam under the executor.

    ``save()`` MUST be durable before it returns. That one obligation is the
    entire content of the kill drill: the engine writes the checkpoint in one
    call before the next node runs, and a process killed with SIGKILL at any
    instant must resume from the last node whose checkpoint returned.

    Thread ids are ``f"{template}:{instance}"`` (see
    ``LoopContext.thread_id``) so two templates driving the same instance can
    never resume each other's state — which is not hypothetical, because
    resuming a half-finished graph into a different template's node names is a
    silent no-op, not a crash.
    """

    def load(self, thread_id: str) -> Mapping[str, Any] | None:
        """The last saved checkpoint, or None for a thread that has never run."""
        ...

    def save(self, thread_id: str, checkpoint: Mapping[str, Any]) -> None:
        """Persist. Durable before returning."""
        ...

    def drop(self, thread_id: str) -> None:
        """Forget this thread. For operator use; the runtime never calls it."""
        ...


class LeasePort(Protocol):
    """Per-instance mutual exclusion, held for the whole tick.

    Two obligations, and the second one is the interesting one:

    * ``hold()`` MUST raise :class:`~selfloop.contracts.LeaseHeld` — carrying
      whatever diagnostics it can read about the holder — rather than block.
      Blocking builds a queue of processes all intending to run the same tick,
      which is the double execution the lease exists to prevent, merely delayed.
    * ``hold()`` MUST NOT implement stale-reclaim by age. An age threshold that
      can TAKE a lease is the unlink-and-recreate double-acquire race wearing a
      costume: two processes both observe the lease as stale, both take it, and
      both run. Age fields on a diagnostics record are for humans to read; they
      are not an input to acquisition.

    A POSIX advisory lock satisfies both for free — the kernel releases it when
    the holder dies, so ``kill -9`` costs nothing and no reclaim path is needed.
    A database lease does not get that for free and must state how it handles a
    holder that died, which is precisely the question the flock design refuses to
    answer by never asking it.
    """

    def hold(self, name: str) -> AbstractContextManager[None]:
        """Acquire for the duration of the context, or raise ``LeaseHeld`` now."""
        ...


class PolicyPort(Protocol):
    """The caller's classification of an action class. Deliberately weak.

    One method and one attribute, replacing a several-hundred-line configuration
    package. It is weak by design: the T2 approval floor is applied by
    ``selfloop.policy`` *after* this value is read, so an adapter can make T0 and
    T1 stricter and can never lower the floor. The floor is not a default this
    port can override — it is not reachable from here at all.

    ``evaluate()`` MUST raise :class:`~selfloop.contracts.PolicyError` when it
    cannot classify, and the gate turns that into a denial. "Could not classify"
    is never "assume read-only"; that reasoning makes an unrecognised action
    class the cheapest possible way out of the gate.

    ``approval_expiry_hours`` is on the port rather than on the context because
    it is a policy statement — how long a human's decision remains authority —
    and it belongs with whoever makes the other policy statements.
    """

    approval_expiry_hours: int

    def evaluate(self, action_class: ActionClass) -> PolicyDecision:
        """Classify one action class. Raises ``PolicyError`` rather than guessing."""
        ...


class ModelPort(Protocol):
    """Optional by design: nothing in the shipped package requires a model.

    Both shipped templates and the quickstart run to completion against a null
    implementation that raises if it is ever called, which is what makes the
    five-minute promise real — no API key, no network, no account.

    ``last_call`` is an attribute on the port rather than something harvested by
    subclassing a host budget guard, which is what the predecessor did and what
    coupled it to a private JSON-repair helper in another package.

    A model may write a lesson's *claim*. It may never write a lesson's
    *guidance* as the sole payload of a promotion: free-form model text as the
    thing that gets injected back into the next prompt is the agent-prose
    promotion this whole design exists to refuse. See ``selfloop.guidance``.
    """

    last_call: Mapping[str, Any] | None

    def complete(
        self, messages: Sequence[Mapping[str, str]], *, purpose: str, **kw: Any
    ) -> str:
        """One completion. ``purpose`` is for the caller's own accounting."""
        ...

    def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        *,
        purpose: str,
        **kw: Any,
    ) -> Mapping[str, Any]:
        """A completion parsed as JSON. Must raise unless every required key is present."""
        ...


class GateRunner(Protocol):
    """The independent verifier — the port with the strictest contract here.

    ``run()`` takes a SPEC TO EXECUTE and never a verdict to record. There is no
    argument through which a caller can hand in ``passed=True``. That single
    constraint is what makes the whole outcome ledger trustworthy, and it costs
    nothing.

    Two failure modes, and they are not the same thing:

    * A gate that RAN and ruled against the work returns
      ``GateReceipt(passed=False)``. That is evidence, and it is adverse.
    * A gate that could not be run at all — missing workspace, unusable
      interpreter, gate binary absent — MUST raise
      :class:`~selfloop.contracts.GateUnavailable`. Absence of evidence is not
      evidence of failure, and a refusal recorded as a failure feeds the
      auto-pause floor. Composition records ``gate_passed = None`` for this, which
      settles the tick as neutral/uncorroborated: not accepted, not blamed.

    And the rule that costs the most to get wrong: a gate that ran but collected
    ZERO checks MUST also raise ``GateUnavailable``, never return
    ``passed=True``. A vacuous gate is worse than no gate. No gate settles every
    tick as visibly uncorroborated; a vacuous gate settles every tick as
    invisibly accepted, and the loop's own optimism becomes its training signal.
    The predecessor's default gate command named a test file in its own repo that
    passed regardless of what the loop had produced, and every loop seeded
    without an explicit gate settled favourable over garbage for months.
    ``GateReceipt.is_vacuous`` exists so that check reads the same everywhere.
    """

    def run(self, spec: GateSpec) -> GateReceipt:
        """Execute *spec* and report what happened. Raises ``GateUnavailable``."""
        ...


class Notifier(Protocol):
    """Tell a human that something is parked.

    ``page()`` returns True ONLY on confirmed delivery, and the caller records
    the delivery event only on True. Recording a page on a failed send turns one
    transient outage into a permanently unpaged approval, because the same
    recorded event is the dedupe key that stops the next tick paging again.

    The shipped default records a deferred page and returns False. That is the
    honest behaviour for an unattended worker: in a scrubbed environment the
    webhook URL is itself a credential the worker does not have, so the worker
    records that a page is owed and something with credentials delivers it.
    """

    def page(self, *, approval_id: str, summary: str, deep_link: str) -> bool:
        """True only if delivery was CONFIRMED. Never optimistic."""
        ...


class SignalSource(Protocol):
    """THE extension point of the learning loop.

    A new learning signal is a function of this shape appended to
    ``ctx.signal_sources``. No port change, no schema change, no migration —
    because the :class:`~selfloop.contracts.LearningSignal` values it yields land
    in the kind-generic :class:`RecordStore`.

    ``extract()`` is handed a CURSOR and not a time window, which is what makes
    the pass exactly-once and re-runnable: the caller advances the cursor only
    after the pass completes, so a crash mid-pass re-mines rather than skips.

    Three obligations on any source you write:

    * Read the durable record AFTER the fact. Never hook the hot path. A source
      that observes a running tick observes a tick that has not been graded yet.
    * Never derive a signal from a NEUTRAL outcome. Idle and parked ticks are
      working as designed; "this stage produced no artifact" is TRUE for every
      one of them, and at a similarity threshold of 0.3 that token soup clusters
      fast and auto-promotes noise.
    * Never derive a signal from the actor's own prose. The predecessor promoted
      knowledge by matching an agent's output text against a fact id, so an agent
      that mentioned ``fact_id=42`` in its own report promoted fact 42.
    """

    name: str

    def extract(self, ctx: LoopContext, *, since_cursor: int) -> Iterable[LearningSignal]:
        """Yield signals found in the record strictly after *since_cursor*."""
        ...


__all__ = [
    "ApprovalStore",
    "CheckpointStore",
    "Clock",
    "EventLog",
    "GateRunner",
    "LeasePort",
    "ModelPort",
    "Notifier",
    "PolicyPort",
    "ReceiptStore",
    "RecordStore",
    "SignalSource",
]
