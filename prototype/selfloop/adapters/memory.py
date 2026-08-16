"""In-memory implementations of all twelve ports, plus a one-call context builder.

This module is what makes the five-minute promise real: zero configuration, zero
files, zero dependencies, no API key, no network. ``build_memory_context()``
returns a fully wired :class:`~selfloop.context.LoopContext` that a stranger can
tick immediately, and every test suite in this package runs against these
adapters as well as the sqlite ones.

Three properties are load-bearing rather than conveniences, and an adapter that
drops any of them will let a test pass that production would fail:

* **Every stored payload is JSON round-tripped on the way in.** The sqlite
  adapter has to serialise, so this one does too. Without it, a payload
  containing a tuple, a set or a live object stores fine here and explodes in
  production — the exact class of divergence that makes "it worked in the tests"
  useless. It also means a caller who mutates the dict they handed us cannot
  retroactively rewrite a record that has already been written down.
* **Everything is guarded by one lock per store.** ``RecordStore.transition`` is
  a compare-and-set and the port says so explicitly: a read-modify-write in
  Python without a lock does not satisfy it, and the race it loses to is real —
  the runtime's learning pass runs unattended while an operator runs the CLI.
* **Absence never renders as success.** :class:`NullModel` raises rather than
  returning ``""``; :class:`ScriptedGate` raises ``GateUnavailable`` rather than
  passing when nobody scripted a verdict; :class:`StaticPolicy` raises
  ``PolicyError`` on an action class it does not know.

What these adapters are NOT: durable. Nothing here survives the process, so they
cannot be the subject of the kill drill and must not back a real loop. That is
what :mod:`selfloop.adapters.sqlite` is for.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import (
    ActionClass,
    GateReceipt,
    GateSpec,
    GateUnavailable,
    LearningSignal,
    LoopError,
    PolicyDecision,
    PolicyError,
    ToolRegistry,
)
from selfloop.lease import InProcessLease

#: The instant :class:`MemoryClock` starts from unless a caller says otherwise.
#: A fixed instant rather than ``datetime.now()``, because a demo clock that
#: reads the host wall clock makes every recorded stamp different on every run,
#: which is precisely what a golden-file or replay test cannot tolerate.
DEFAULT_START_ISO = "2026-01-01T00:00:00+00:00"


def canonical_value(value: Any) -> Any:
    """JSON round-trip one value, exactly as a durable adapter would store it.

    ``default=str`` matches :func:`selfloop.contracts.digest_key`, so a value
    that *digests* as a string also *stores* as one and the two never disagree
    about what a payload contains.

    Applied to stored payloads AND to the values in ``expect``/``equals``
    comparisons. Without the second half, a caller passing ``expect={"ids":
    ("a",)}`` would never match a stored ``["a"]`` and every compare-and-set
    would fail for a reason no error message could explain.
    """
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def fields_match(payload: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """True when every field in *expected* equals the stored value in *payload*."""
    return all(payload.get(key) == canonical_value(value) for key, value in expected.items())


def approval_row_id(row: Mapping[str, Any]) -> str:
    """The identity of an approval row: strictly ``row["approval_id"]``.

    One spelling, enforced in one place and imported by both shipped adapters,
    because the alternative is the failure :class:`~selfloop.contracts.RecordKind`
    exists to prevent — a join key spelled two ways creates a second, silent
    namespace in which every write succeeds and every read from the other
    spelling returns nothing, forever. Accepting ``id`` as a fallback would be
    exactly that second spelling, so it is refused.
    """
    approval_id = row.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise ValueError(
            "an approval row must carry a non-empty string 'approval_id'; it is the "
            f"row's identity and the only spelling either shipped adapter reads (got {row!r})"
        )
    return approval_id


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class MemoryClock:
    """A controllable :class:`~selfloop.ports.Clock` that reads nothing external.

    The two clocks stay separate here for the same reason the port separates
    them. :meth:`advance` moves both — that is time passing. :meth:`pin` moves
    only the record stamp, which is the backfill case the port describes, and it
    deliberately leaves :meth:`elapsed` alone: a backfill may falsify a stamp
    with a caller's consent, and must never be able to falsify a freshness check.

    :meth:`advance` refuses a negative delta. That is the "never steps back"
    obligation made mechanical rather than documented, and it is why a test can
    exercise an expiry window without sleeping.
    """

    def __init__(self, *, start_iso: str = DEFAULT_START_ISO, start_elapsed: float = 0.0) -> None:
        self._origin = datetime.fromisoformat(start_iso)
        self._elapsed = float(start_elapsed)
        self._pinned: str | None = None
        self._lock = threading.Lock()

    def now_iso(self) -> str:
        """ISO-8601 record stamp."""
        with self._lock:
            if self._pinned is not None:
                return self._pinned
            return (self._origin + timedelta(seconds=self._elapsed)).isoformat()

    def elapsed(self) -> float:
        """Monotonic seconds. Only ever moved forward, by :meth:`advance`."""
        with self._lock:
            return self._elapsed

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by *seconds*."""
        if seconds < 0:
            raise ValueError(
                f"a monotonic clock cannot step back (got {seconds!r}); a test that needs "
                "an earlier record stamp wants pin(), which leaves elapsed() alone"
            )
        with self._lock:
            self._elapsed += float(seconds)

    def pin(self, stamp: str) -> None:
        """Pin the RECORD STAMP only, for a backfill. ``elapsed`` is untouched."""
        with self._lock:
            self._pinned = stamp

    def unpin(self) -> None:
        """Resume deriving the record stamp from the advanced origin."""
        with self._lock:
            self._pinned = None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

#: The exact column names both shipped adapters return from
#: ``ReceiptStore.get``. Written down because every test in this package runs
#: against both, and a key present in one adapter and absent in the other is a
#: test that passes on memory and fails on sqlite for reasons unrelated to the
#: behaviour under test.
RECEIPT_FIELDS = ("key", "instance_id", "node", "claimed_at", "result_json", "completed_at")


class MemoryReceiptStore:
    """Exactly-once bookkeeping in a dict. See :class:`~selfloop.ports.ReceiptStore`.

    ``complete`` is durable-before-returning in the only sense a dict can be —
    which is why this store must not be the subject of the kill drill. It is here
    so the *protocol* can be exercised without a file.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, *, instance_id: str, node: str, at: str) -> bool:
        """Insert-or-ignore. True only when THIS caller created the row."""
        with self._lock:
            if key in self._rows:
                return False
            self._rows[key] = {
                "key": key,
                "instance_id": instance_id,
                "node": node,
                "claimed_at": at,
                "result_json": None,
                "completed_at": None,
            }
            return True

    def get(self, key: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._rows.get(key)
            return dict(row) if row is not None else None

    def complete(self, key: str, *, envelope_json: str, at: str) -> None:
        """Record this attempt's terminal outcome onto the claimed row.

        Raises when no row exists. A completion for a key that was never claimed
        means the claim/act/complete sequence has come apart somewhere upstream,
        and inventing the row would hide that: the caller would believe an
        exactly-once protocol had run when in fact nothing arbitrated the race.
        """
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                raise LoopError(
                    f"cannot complete receipt {key!r}: no claim row exists. A completion "
                    "without a claim means nothing arbitrated the race for this effect."
                )
            row["result_json"] = envelope_json
            row["completed_at"] = at

    def release(self, key: str) -> bool:
        """Drop a claim that provably produced no effect. No-op once a result exists.

        The ``result_json is None`` guard lives HERE, in the store, and not in the
        caller — that is the crash-window guarantee, and a caller must not be able
        to talk the store out of it, including a future refactor of this package.
        """
        with self._lock:
            row = self._rows.get(key)
            if row is None or row.get("result_json") is not None:
                return False
            del self._rows[key]
            return True


class MemoryApprovalStore:
    """Park/approve rows in a dict. See :class:`~selfloop.ports.ApprovalStore`."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, approval_id: str) -> Mapping[str, Any] | None:
        """The row, or None. Writes nothing — a read must never mint a row."""
        with self._lock:
            row = self._rows.get(approval_id)
            return dict(row) if row is not None else None

    def create(self, row: Mapping[str, Any]) -> bool:
        """Insert if absent. False means it already existed, which is not an error.

        Approval ids are deterministic, so a replayed tick re-derives the same id,
        lands here, gets False, and pages nobody. One row, one page, ever.
        """
        approval_id = approval_row_id(row)
        stored = canonical_value(dict(row))
        stored.setdefault("state", "pending")
        with self._lock:
            if approval_id in self._rows:
                return False
            self._rows[approval_id] = stored
            return True

    def decide(self, approval_id: str, *, state: str, by: str, note: str, at: str) -> bool:
        """Compare-and-set from ``pending``. False when another decider won.

        Returns False rather than raising, because losing is a normal outcome:
        two workers, or a worker and an operator at a CLI, can decide the same row
        at the same moment. Without the CAS both would "succeed" and the second
        would silently overwrite the first — which is how an approval overwrites
        a rejection.
        """
        with self._lock:
            row = self._rows.get(approval_id)
            if row is None or row.get("state") != "pending":
                return False
            row["state"] = state
            row["decided_by"] = by
            row["decided_at"] = at
            row["note"] = note
            return True


class MemoryRecordStore:
    """The kind-generic record store. See :class:`~selfloop.ports.RecordStore`."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put_once(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> bool:
        """HISTORY. Insert-if-absent; False when a record already exists.

        A run must not be able to overwrite its own report card, so the caller
        that gets False must treat the existing record as authoritative rather
        than retrying with its own version.
        """
        with self._lock:
            slot = (str(kind), record_id)
            if slot in self._rows:
                return False
            self._rows[slot] = canonical_value(dict(payload))
            return True

    def put_latest(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> None:
        """CACHE. Last write wins, so a fresher green supersedes a stale one."""
        with self._lock:
            self._rows[(str(kind), record_id)] = canonical_value(dict(payload))

    def get(self, kind: str, record_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._rows.get((str(kind), record_id))
            return dict(row) if row is not None else None

    def query(self, kind: str, /, **equals: Any) -> list[Mapping[str, Any]]:
        """Every payload of *kind* whose fields equal all the given values.

        Equality only, and a full scan. Correct at prototype scale and wrong at
        any real scale — a production backend should index ``(kind, scope,
        cursor)`` — but keeping it this simple is what keeps the port at five
        methods and this adapter readable in one sitting.
        """
        wanted = str(kind)
        with self._lock:
            rows = [dict(row) for (row_kind, _), row in self._rows.items() if row_kind == wanted]
        return [row for row in rows if fields_match(row, equals)]

    def transition(
        self,
        kind: str,
        record_id: str,
        *,
        expect: Mapping[str, Any],
        set: Mapping[str, Any],  # noqa: A002 - the name IS the contract; callers read it as SQL
    ) -> bool:
        """Compare-and-set. False means another writer moved the row first.

        Held under the same lock as every other write to this store, because the
        port's obligation is atomicity with respect to other writers of the same
        ``(kind, record_id)`` and a read-modify-write outside a lock does not
        provide it. Without this, a lesson retired for regression can be
        resurrected to promoted by a concurrent writer that read the row a moment
        earlier.
        """
        slot = (str(kind), record_id)
        with self._lock:
            row = self._rows.get(slot)
            if row is None or not fields_match(row, expect):
                return False
            updated = dict(row)
            updated.update(canonical_value(dict(set)))
            self._rows[slot] = updated
            return True


class MemoryEventLog:
    """The ordered replay cursor: a list and a counter.

    The counter is strictly increasing and never restarts. That is the whole
    contract: "extract signals since cursor N" is what makes the learning pass
    exactly-once and re-runnable, and a log that restarts numbering — or hands
    the same integer to two writers — breaks it in a way no single-process test
    will show.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._next = 1
        self._lock = threading.Lock()

    def append(self, event: Mapping[str, Any]) -> int:
        """Append one event; return its cursor."""
        with self._lock:
            cursor = self._next
            self._next += 1
            stored = canonical_value(dict(event))
            # Injected by the log, and it overwrites any caller-supplied value of
            # the same name: the cursor is the log's statement about ordering, not
            # the writer's, and a writer that could set it could re-mine or skip
            # arbitrary stretches of history.
            stored["cursor"] = cursor
            self._events.append(stored)
            return cursor

    def read(self, *, after: int = 0, limit: int = 500) -> list[Mapping[str, Any]]:
        """Events with a cursor strictly greater than *after*, in cursor order."""
        if limit <= 0:
            return []
        with self._lock:
            return [dict(e) for e in self._events if int(e["cursor"]) > after][:limit]


class MemoryCheckpointStore:
    """The durability seam, in a dict. See :class:`~selfloop.ports.CheckpointStore`.

    Snapshots on the way in and on the way out, so the engine mutating the state
    dict it just saved cannot retroactively edit the "durable" checkpoint. An
    adapter that stored the live object would make a whole class of resume bug
    invisible in tests and fatal in production.
    """

    def __init__(self) -> None:
        self._threads: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, thread_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._threads.get(thread_id)
            return dict(row) if row is not None else None

    def save(self, thread_id: str, checkpoint: Mapping[str, Any]) -> None:
        with self._lock:
            self._threads[thread_id] = canonical_value(dict(checkpoint))

    def drop(self, thread_id: str) -> None:
        """Forget this thread. For operator use; the runtime never calls it."""
        with self._lock:
            self._threads.pop(thread_id, None)


# ---------------------------------------------------------------------------
# Policy, model, gate, notifier, signals
# ---------------------------------------------------------------------------

#: The default classification :class:`StaticPolicy` applies. Consequential and
#: worse require a human; read-only and reversible do not. Note what this table
#: cannot do: it cannot let a T2 tool through, because the approval floor is
#: applied by ``selfloop.policy`` *after* a ``PolicyPort`` is consulted and is
#: not reachable from here.
DEFAULT_POLICY_TABLE: Mapping[ActionClass, PolicyDecision] = {
    ActionClass.READ_ONLY: PolicyDecision(
        requires_approval=False,
        reason="read-only",
        action_class=ActionClass.READ_ONLY,
    ),
    ActionClass.INTERNAL_REVERSIBLE: PolicyDecision(
        requires_approval=False,
        reason="reversible local mutation",
        action_class=ActionClass.INTERNAL_REVERSIBLE,
    ),
    ActionClass.CONSEQUENTIAL: PolicyDecision(
        requires_approval=True,
        reason="externally visible effect",
        action_class=ActionClass.CONSEQUENTIAL,
    ),
    ActionClass.IRREVERSIBLE: PolicyDecision(
        requires_approval=True,
        reason="irreversible",
        action_class=ActionClass.IRREVERSIBLE,
    ),
    ActionClass.ALWAYS_HUMAN: PolicyDecision(
        requires_approval=True,
        reason="declared always-human",
        action_class=ActionClass.ALWAYS_HUMAN,
    ),
}


class StaticPolicy:
    """A table-driven :class:`~selfloop.ports.PolicyPort` for tests and demos.

    Deliberately independent of ``selfloop.policy`` so that the adapter layer
    imports nothing from the seam stack above it. ``selfloop.policy.TierPolicy``
    is the equivalent shipped default for production wiring; this one exists so
    that ``build_memory_context()`` can hand back a working context without
    dragging in the whole seam.

    ``evaluate`` raises :class:`~selfloop.contracts.PolicyError` for an action
    class it has no row for. "Could not classify" is never "assume read-only" —
    that reading turns an unrecognised action class into the cheapest possible
    way out of the gate.
    """

    def __init__(
        self,
        table: Mapping[ActionClass, PolicyDecision] | None = None,
        *,
        approval_expiry_hours: int = 24,
    ) -> None:
        self._table = dict(table if table is not None else DEFAULT_POLICY_TABLE)
        self.approval_expiry_hours = int(approval_expiry_hours)

    def evaluate(self, action_class: ActionClass) -> PolicyDecision:
        decision = self._table.get(action_class)
        if decision is None:
            raise PolicyError(
                f"no policy row for action class {action_class!r}; refusing to guess. "
                "An unclassifiable action must deny, never default to read-only."
            )
        return decision


class NullModel:
    """A :class:`~selfloop.ports.ModelPort` that raises if it is ever called.

    Shipped as the default so the five-minute promise is testable rather than
    asserted: both shipped templates and the quickstart run to completion against
    this, which proves they need no API key, no network and no account. If your
    template reaches a model, you find out here, loudly, instead of discovering
    it when somebody else runs the quickstart.
    """

    def __init__(self) -> None:
        self.last_call: Mapping[str, Any] | None = None

    def complete(self, messages: Sequence[Mapping[str, str]], *, purpose: str, **kw: Any) -> str:
        raise LoopError(
            f"this loop was built with NullModel, but something asked it to think "
            f"(purpose={purpose!r}). Nothing shipped in selfloop requires a model; pass a "
            "real ModelPort on the LoopContext if your template does."
        )

    def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        *,
        purpose: str,
        **kw: Any,
    ) -> Mapping[str, Any]:
        raise LoopError(
            f"this loop was built with NullModel, but something asked it for JSON "
            f"(purpose={purpose!r}, required_keys={list(required_keys)})."
        )


class RecordingModel:
    """A scripted :class:`~selfloop.ports.ModelPort` that remembers every prompt.

    The recording half is the point: the learning loop's liveness test has to
    show that a promoted lesson actually reached the next run's prompt, and
    ``model.calls[-1]["text"]`` is how it looks. (Prompt presence is evidence
    that injection happened — it is emphatically NOT evidence that the lesson
    helped. That question is settled by a gate against a world artifact, never
    by a substring match, because a scorer that rewards its own text appearing
    in a prompt grades its own homework.)

    Runs out of script by RAISING rather than by returning ``""``. A model double
    that quietly answers nothing turns "the test forgot a reply" into "the loop
    handled an empty completion", and the second is a much more expensive thing
    to discover.
    """

    def __init__(
        self,
        replies: Sequence[str] = (),
        json_replies: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.last_call: Mapping[str, Any] | None = None
        self._replies = list(replies)
        self._json_replies = [dict(reply) for reply in json_replies]

    def _record(self, messages: Sequence[Mapping[str, str]], purpose: str, kind: str) -> None:
        call = {
            "kind": kind,
            "purpose": purpose,
            "messages": [dict(m) for m in messages],
            "text": "\n".join(str(m.get("content", "")) for m in messages),
        }
        self.calls.append(call)
        self.last_call = call

    def complete(self, messages: Sequence[Mapping[str, str]], *, purpose: str, **kw: Any) -> str:
        self._record(messages, purpose, "complete")
        if not self._replies:
            raise LoopError(
                f"RecordingModel has no scripted reply left for purpose={purpose!r}; "
                "script one rather than letting the loop see an empty completion"
            )
        return self._replies.pop(0)

    def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        *,
        purpose: str,
        **kw: Any,
    ) -> Mapping[str, Any]:
        self._record(messages, purpose, "complete_json")
        if not self._json_replies:
            raise LoopError(
                f"RecordingModel has no scripted JSON reply left for purpose={purpose!r}"
            )
        reply = self._json_replies.pop(0)
        missing = [key for key in required_keys if key not in reply]
        if missing:
            raise LoopError(
                f"scripted JSON reply for purpose={purpose!r} is missing required keys "
                f"{missing}; the port requires this to raise rather than return a partial"
            )
        return reply


def passing_receipt(*, checks: int = 1, detail: str = "", ran_at: str = "") -> GateReceipt:
    """A non-vacuous passing :class:`~selfloop.contracts.GateReceipt` for tests.

    ``checks`` defaults to 1 and may not be 0: a passing receipt that collected
    nothing is the vacuous gate this package refuses everywhere, and a test
    helper that mints one would smuggle it straight past the refusal.
    """
    if checks <= 0:
        raise ValueError(
            "a passing gate receipt must have collected at least one check; a zero-check "
            "pass is a vacuous gate and GateRunner must raise GateUnavailable instead"
        )
    return GateReceipt(
        passed=True,
        checks_collected=checks,
        checks_passed=checks,
        detail=detail,
        ran_at=ran_at,
    )


def failing_receipt(*, checks: int = 1, detail: str = "", ran_at: str = "") -> GateReceipt:
    """A non-vacuous failing receipt: the gate RAN and ruled against the work."""
    if checks <= 0:
        raise ValueError("a failing gate receipt must also have collected at least one check")
    return GateReceipt(
        passed=False,
        checks_collected=checks,
        checks_passed=max(0, checks - 1),
        detail=detail,
        ran_at=ran_at,
    )


class ScriptedGate:
    """A :class:`~selfloop.ports.GateRunner` that replays verdicts a test supplied.

    It enforces the two rules the port states, so that a test cannot accidentally
    build a gate weaker than any real one:

    * **Out of script is UNAVAILABLE, not passing.** With no scripted verdict and
      no default, :meth:`run` raises ``GateUnavailable``. Absence of evidence is
      not evidence of anything, and it settles the tick as
      neutral/uncorroborated — visibly unverified rather than invisibly accepted.
    * **A vacuous receipt is refused.** A receipt with ``checks_collected == 0``
      raises ``GateUnavailable`` however the test wrote it. The predecessor's
      default gate named a test file in its own repo that passed regardless of
      what the loop had produced, and every loop seeded without an explicit gate
      settled favourable over garbage for months.

    ``runs`` records the specs it was asked to execute, so a test can assert the
    gate was actually reached rather than assuming it.
    """

    def __init__(
        self,
        verdicts: Sequence[GateReceipt] = (),
        *,
        default: GateReceipt | None = None,
    ) -> None:
        self.verdicts: list[GateReceipt] = list(verdicts)
        self.runs: list[GateSpec] = []
        self.default = default

    def run(self, spec: GateSpec) -> GateReceipt:
        self.runs.append(spec)
        receipt = self.verdicts.pop(0) if self.verdicts else self.default
        if receipt is None:
            raise GateUnavailable(
                "no_scripted_verdict",
                f"ScriptedGate has no verdict for {spec.label or ' '.join(spec.command)!r}. "
                "An unscripted gate reports that it could not run, never that it passed.",
            )
        if receipt.is_vacuous:
            raise GateUnavailable(
                "vacuous_gate",
                "the scripted receipt collected zero checks; a gate that tested nothing "
                "must report unavailability, because a vacuous pass is worse than no gate",
            )
        return receipt


class RecordingNotifier:
    """A :class:`~selfloop.ports.Notifier` that captures pages in a list.

    ``pages`` is what a test reads to assert "exactly one page for a repeatedly
    parked approval". Delivery into a list genuinely is confirmed, so the default
    returns True; construct it with ``delivers=False`` to exercise the outage
    path, where the caller must NOT record a delivery event — recording a page
    that never went out turns one transient outage into a permanently unpaged
    approval, because that same recorded event is the dedupe key that stops the
    next tick paging again.
    """

    def __init__(self, *, delivers: bool = True) -> None:
        self.pages: list[dict[str, str]] = []
        self.delivers = bool(delivers)

    def page(self, *, approval_id: str, summary: str, deep_link: str) -> bool:
        self.pages.append({"approval_id": approval_id, "summary": summary, "deep_link": deep_link})
        return self.delivers


class ScriptedSignalSource:
    """A :class:`~selfloop.ports.SignalSource` that yields signals a test supplied.

    Honours the cursor contract rather than ignoring it — only signals strictly
    after ``since_cursor`` are yielded — because a source that returns everything
    every time makes the learning pass look idempotent when it is not, and the
    exactly-once property of the pass is the thing under test.
    """

    def __init__(self, name: str, signals: Iterable[LearningSignal] = ()) -> None:
        self.name = name
        self.signals: list[LearningSignal] = list(signals)

    def extract(self, ctx: LoopContext, *, since_cursor: int) -> Iterator[LearningSignal]:
        for signal in list(self.signals):
            if signal.cursor > since_cursor:
                yield signal


# ---------------------------------------------------------------------------
# The one-call constructor
# ---------------------------------------------------------------------------


def build_memory_context(**overrides: Any) -> LoopContext:
    """A fully wired :class:`~selfloop.context.LoopContext` in one call.

    Every port gets a fresh in-memory implementation and every keyword overrides
    exactly one :class:`LoopContext` field, so swapping in a real backend is a
    keyword rather than a rewrite. Running a suite against the sqlite adapters is
    therefore::

        backend = SqliteBackend(tmp_path / "loop.db")
        ctx = build_memory_context(instance_id="t1", **backend.as_context_overrides())

    This function lives in the adapters package and not in ``selfloop.context``
    on purpose. An earlier revision had a ``for_testing()`` helper on the context
    itself, which made the foundation depend on a layer above it and produced a
    circular import the first time somebody imported an adapter from a contract.
    Test fixtures build contexts; contexts do not build themselves.

    An unrecognised keyword raises rather than being ignored. A silently dropped
    ``min_suport=5`` builds a context that is not the one the caller asked for,
    and the test that follows proves something about the default instead.

    Two defaults are worth reading before you rely on them:

    * ``lease`` is an :class:`~selfloop.lease.InProcessLease`, which protects
      nothing between OS processes. That is correct for a test and for the
      quickstart, and wrong for anything a scheduler starts — use
      ``FlockLease`` or ``SqliteLease`` there.
    * ``gate`` is an empty :class:`ScriptedGate`, so every tick settles
      neutral/uncorroborated until a caller scripts a verdict or passes a real
      gate. That is the same answer ``gate=None`` gives, reached through the code
      path production actually uses, and it means nothing here can accidentally
      teach itself from its own optimism.
    """
    known = {f.name for f in fields(LoopContext)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TypeError(
            f"build_memory_context() got unexpected keyword(s) {unknown}; valid LoopContext "
            f"fields are {sorted(known)}. Unknown keywords are refused rather than ignored, "
            "because a silently dropped override builds a context nobody asked for."
        )
    defaults: dict[str, Any] = {
        "instance_id": "demo",
        "template": "demo",
        "tools": ToolRegistry(),
        "clock": MemoryClock(),
        "receipts": MemoryReceiptStore(),
        "approvals": MemoryApprovalStore(),
        "records": MemoryRecordStore(),
        "events": MemoryEventLog(),
        "checkpoints": MemoryCheckpointStore(),
        "lease": InProcessLease(accept_single_process_only=True),
        "policy": StaticPolicy(),
        "model": NullModel(),
        "gate": ScriptedGate(),
        "notifier": RecordingNotifier(),
    }
    defaults.update(overrides)
    return LoopContext(**defaults)


__all__ = [
    "DEFAULT_POLICY_TABLE",
    "DEFAULT_START_ISO",
    "RECEIPT_FIELDS",
    "MemoryApprovalStore",
    "MemoryCheckpointStore",
    "MemoryClock",
    "MemoryEventLog",
    "MemoryReceiptStore",
    "MemoryRecordStore",
    "NullModel",
    "RecordingModel",
    "RecordingNotifier",
    "ScriptedGate",
    "ScriptedSignalSource",
    "StaticPolicy",
    "approval_row_id",
    "build_memory_context",
    "canonical_value",
    "failing_receipt",
    "fields_match",
    "passing_receipt",
]
