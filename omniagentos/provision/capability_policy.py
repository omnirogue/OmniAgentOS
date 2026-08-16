"""U-E2 — the capability decision machine.

One conjunctive auto-grant floor, one compare-and-swap into a terminal state, one
90-second caller wall, and the latency telemetry the floor's SLO is measured
against. Nothing in this module reads a credential, names an environment
variable, or calls the broker: it decides, and the broker resolves later against
the grant row this writes.

THE FLOOR IS EXACTLY THIS CONJUNCTION -- see :meth:`PolicyFacts.floor_allows`::

    known ∧ callable_now ∧ read_only ∧ in_approved_scope ∧ lane_allowed
          ∧ ¬paid ∧ ¬comms_write ∧ ¬danger_group

Every conjunct is a named field rather than an inlined expression so a reviewer
can see which one refused, and so a receipt can say so. Anything the floor does
not admit PARKS for an operator; it is never quietly downgraded into a grant.

TWO CLOCKS, DELIBERATELY SEPARATE
    * The **caller wall** is 90 seconds, measured from the durable ``opened_at``
      on the decision row (not from a caller's in-memory stopwatch, which does
      not survive the process). At the wall the request CAS-es to ``timed_out``
      and the worker gets a typed ``next_action`` and resumes.
    * The **operator decision window** is minutes, and is not modelled here at
      all. An operator who answers after the wall does not "un-time-out" the old
      request -- :meth:`CapabilityPolicy.operator_decide` LOSES the CAS and
      creates no grant. The operator's approval mints a FRESH request id.
    * ``grant_decision_ms`` measures neither of those: it is how long the
      DECISION took, on a monotonic counter, and the floor SLO (p50 <100ms,
      p95 <500ms) is read from the ``granted_by = 'autogrant'`` rows only.

BALANCE RULE
Every terminal state carries a ``next_action`` from a closed vocabulary, and the
only "keep waiting" answer -- ``await_operator`` -- is refused on a terminal row
by migration 112's CHECK. No refusal can leave a worker with nothing to do.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from omniagentos.connectors import Capability, ConnectorError, ConnectorRegistry, load_registry

LOG = logging.getLogger(__name__)

#: In-run grants expire after 1,800s (§7: ``min(requested_at + 1800s, task_end,
#: session_end)``; the task/session legs are applied by the caller that owns
#: those lifetimes). A renewed need mints a new request id -- extending a row
#: would erase its decision latency and its attribution.
AUTO_GRANT_TTL_SECONDS = 1800

#: The caller-visible park wall. Hard, at all hours (S7-7).
CALLER_WALL_SECONDS = 90.0

PENDING_STATE = "pending_operator"
TERMINAL_STATES = frozenset({"granted", "denied", "hard_rejected", "timed_out"})

#: The complete machine-actionable vocabulary. "Pending" is not in it, and
#: ``await_operator`` is legal only while the row is still pending.
NEXT_ACTIONS = frozenset(
    {
        "retry_with_grant",
        "escalate_tier",
        "split_bounded",
        "continue_degraded",
        "await_operator",
        "stop_terminal",
    }
)

#: Metered capabilities: a call costs money, so they are floor-EXCLUDED and park
#: for an operator with a budget envelope (D-10/D-11, K2-14).
#:
#: This lives here rather than on the registry model because U-E2 does not own
#: ``connectors/__init__.py`` or ``configs/connectors.yaml``, and because the two
#: existing paid ids are already declared by the loop budget ledger. The two
#: ledgers are reconciled by a test (``test_paid_ledger_covers_the_loop_seam``)
#: rather than by importing the scheduler into the provisioning path. When U-N1
#: lands a metered web provider it declares its id here, or moves the whole set
#: onto the registry -- what it may NOT do is discover its cost class at a 90s
#: park.
PAID_CAPABILITY_IDS: frozenset[str] = frozenset({"replicate.generate", "model.complete"})

#: The eight conjunct names, in predicate order. Reported verbatim in receipts so
#: "why did this park?" is answerable without reading this file.
FLOOR_CONJUNCTS = (
    "known",
    "callable_now",
    "read_only",
    "in_approved_scope",
    "lane_allowed",
    "paid",
    "comms_write",
    "danger_group",
)


class Clock(Protocol):
    """Wall-clock source for the 90-second caller wall. Injected in tests."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class PolicyViolation(RuntimeError):
    """A grant was attempted for something the floor may not admit.

    Raised by the independent pre-issue re-read, NOT by the floor predicate. It
    exists because the predicate's inputs are the attackable surface: a rung
    file, a lane allowlist, or a tampered facts source can claim a metered or
    writing capability is floor-eligible. The registry is re-read immediately
    before the grant is written, so a forged "yes" still produces no grant.
    """


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    """The eight floor inputs for one capability, each resolved separately."""

    known: bool
    callable_now: bool
    read_only: bool
    in_approved_scope: bool
    lane_allowed: bool
    paid: bool
    comms_write: bool
    danger_group: bool

    @property
    def floor_allows(self) -> bool:
        """known ∧ callable_now ∧ read_only ∧ in_approved_scope ∧ lane_allowed
        ∧ ¬paid ∧ ¬comms_write ∧ ¬danger_group."""
        return (
            self.known
            and self.callable_now
            and self.read_only
            and self.in_approved_scope
            and self.lane_allowed
            and not self.paid
            and not self.comms_write
            and not self.danger_group
        )

    @property
    def blocking_conjuncts(self) -> tuple[str, ...]:
        """The conjuncts that refused, in predicate order; empty iff the floor allows."""
        blocked = []
        for name in FLOOR_CONJUNCTS:
            value = getattr(self, name)
            negated = name in {"paid", "comms_write", "danger_group"}
            if value if negated else not value:
                blocked.append(name)
        return tuple(blocked)


@dataclass(frozen=True, slots=True)
class PolicyReceipt:
    """What the caller is told. Carries no key name, no credential, no grant token."""

    request_id: str
    state: str
    reason_code: str
    next_action: str
    grant_decision_ms: int | None
    decided_at: str | None
    expires_at: str | None
    granted_capability_ids: tuple[str, ...] = ()
    excluded_capability_ids: tuple[str, ...] = ()
    facts: tuple[tuple[str, PolicyFacts], ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def granted(self) -> bool:
        return self.state == "granted"


class CapabilityDecisions:
    """The transaction boundary over migration 112's ``capability_decisions``.

    Two writes matter and both are compare-and-swap against
    ``state = 'pending_operator'``: :meth:`cas_terminal` and :meth:`cas_grant`.
    A racing writer loses the UPDATE (``rowcount == 0``) rather than overwriting
    a decision, and migration 112's ``capability_decision_terminal_is_final``
    trigger catches anyone who tries without the WHERE clause.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    @property
    def _conn(self) -> sqlite3.Connection:
        connection: sqlite3.Connection = self._store._connection
        return connection

    def open(
        self,
        request_id: str,
        *,
        holder_agent_id: str,
        subject_kind: str,
        subject_id: str,
        opened_at: str,
    ) -> dict[str, Any]:
        """Create the pending row, or return the existing one for a replayed id.

        A replay of the same ``request_id`` with a DIFFERENT holder or subject is
        an error, not an update: the row is the durable record of what was asked.
        """
        with self._store._lock:
            existing = self.get(request_id)
            if existing is None:
                try:
                    self._conn.execute(
                        "INSERT INTO capability_decisions "
                        "(request_id, holder_agent_id, subject_kind, subject_id, opened_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (request_id, holder_agent_id, subject_kind, subject_id, opened_at),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    # Another PROCESS opened the same request id between the read
                    # and the insert. Its row is the record; ours never existed.
                    self._conn.rollback()
                    existing = self.get(request_id)
                    if existing is None:
                        raise
                else:
                    row = self.get(request_id)
                    assert row is not None
                    return row
        if (
            existing["holder_agent_id"] != holder_agent_id
            or existing["subject_kind"] != subject_kind
            or existing["subject_id"] != subject_id
        ):
            raise ValueError(
                f"request_id {request_id!r} already records a different holder/subject"
            )
        return existing

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM capability_decisions WHERE request_id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def cas_terminal(
        self,
        request_id: str,
        *,
        state: str,
        reason_code: str,
        next_action: str,
        grant_decision_ms: int,
        decided_at: str,
        excluded_ids: Sequence[str] = (),
    ) -> bool:
        """Move a pending row to a NON-grant terminal state. True iff this call won."""
        if state not in TERMINAL_STATES or state == "granted":
            raise ValueError("cas_terminal issues no grant; use cas_grant for 'granted'")
        if next_action not in NEXT_ACTIONS or next_action == "await_operator":
            raise ValueError(f"{next_action!r} is not a terminal next_action")
        with self._store._lock:
            cursor = self._conn.execute(
                "UPDATE capability_decisions SET state = ?, reason_code = ?, next_action = ?, "
                "grant_decision_ms = ?, decided_at = ?, excluded_ids_json = ? "
                "WHERE request_id = ? AND state = ?",
                (
                    state,
                    reason_code,
                    next_action,
                    max(0, grant_decision_ms),
                    decided_at,
                    json.dumps(list(excluded_ids), separators=(",", ":")),
                    request_id,
                    PENDING_STATE,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def note_park_reason(self, request_id: str, *, reason_code: str) -> bool:
        """Record WHY a still-pending request parked. True iff it was still pending.

        A park is a deferral, not an answer, but the operator who has to answer it
        needs the same typed reason the caller got. Without this the row reads
        ``reason_code = ''`` and the only place the blocking conjunct exists is in
        the receipt the caller already walked away with.
        """
        with self._store._lock:
            cursor = self._conn.execute(
                "UPDATE capability_decisions SET reason_code = ? "
                "WHERE request_id = ? AND state = ?",
                (reason_code, request_id, PENDING_STATE),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def cas_grant(
        self,
        request_id: str,
        *,
        capability_ids: Sequence[str],
        action_classes: dict[str, str],
        granted_by: str,
        grant_decision_ms: int,
        decided_at: str,
        expires_at: str,
        note: str,
        excluded_ids: Sequence[str] = (),
    ) -> bool:
        """Win the CAS and write the grant rows, or do neither.

        Both halves run in ONE ``BEGIN IMMEDIATE``. The alternative orderings both
        leak: CAS-then-grant can leave a ``granted`` decision with no grant if the
        insert fails, and grant-then-CAS can leave a live grant behind a decision
        that lost. The two ``agent_capabilities`` / ``capability_grant_log``
        statements mirror ``CapabilityStore.issue_scoped_grant`` rather than
        calling it, because that method opens its own transaction and a nested
        BEGIN is an error -- the duplication buys atomicity, and the columns it
        writes are pinned by ``test_autogrant_writes_the_same_columns_as_issue_scoped_grant``.
        """
        if not capability_ids:
            raise ValueError("a grant must name at least one capability")
        with self._store._lock:
            self._store._begin()
            try:
                cursor = self._conn.execute(
                    "UPDATE capability_decisions SET state = 'granted', reason_code = '', "
                    "next_action = 'retry_with_grant', grant_decision_ms = ?, decided_at = ?, "
                    "granted_by = ?, expires_at = ?, granted_ids_json = ?, excluded_ids_json = ? "
                    "WHERE request_id = ? AND state = ?",
                    (
                        max(0, grant_decision_ms),
                        decided_at,
                        granted_by,
                        expires_at,
                        json.dumps(sorted(capability_ids), separators=(",", ":")),
                        json.dumps(sorted(excluded_ids), separators=(",", ":")),
                        request_id,
                        PENDING_STATE,
                    ),
                )
                if cursor.rowcount != 1:
                    self._store._rollback()
                    return False
                holder = str(
                    self._conn.execute(
                        "SELECT holder_agent_id FROM capability_decisions WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()["holder_agent_id"]
                )
                # C11/D-31: the grant inherits the PROJECT the request was made
                # for, read here from the immutable envelope inside the same
                # transaction. It is never a parameter: a caller that could name
                # the project a grant binds to could bind a project-A ask to a
                # project-B grant, which is the exact substitution this change
                # exists to make impossible. No envelope (a decision written
                # directly, as some tests and the legacy path do) means no
                # binding, and an unbound grant authorizes no project-scoped
                # work.
                request_row = self._conn.execute(
                    "SELECT project_id FROM capability_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                project_id = None if request_row is None else request_row["project_id"]
                for cap_id in sorted(capability_ids):
                    self._conn.execute(
                        "INSERT INTO agent_capabilities "
                        "(agent_id, capability_id, granted_at, granted_by, note, mode, "
                        "expires_at, issued_by, request_id, project_id) "
                        "VALUES (?, ?, ?, ?, ?, 'read', ?, ?, ?, ?) "
                        "ON CONFLICT(agent_id, capability_id) DO UPDATE SET "
                        "granted_at = excluded.granted_at, granted_by = excluded.granted_by, "
                        "note = excluded.note, mode = excluded.mode, "
                        "expires_at = excluded.expires_at, issued_by = excluded.issued_by, "
                        "request_id = excluded.request_id, project_id = excluded.project_id",
                        (holder, cap_id, decided_at, granted_by, note, expires_at,
                         granted_by, request_id, project_id),
                    )
                    self._conn.execute(
                        "INSERT INTO capability_grant_log "
                        "(agent_id, capability_id, action, action_class, actor, note, ts, "
                        "mode, expires_at, issued_by, request_id) "
                        "VALUES (?, ?, 'grant', ?, ?, ?, ?, 'read', ?, ?, ?)",
                        (holder, cap_id, action_classes.get(cap_id, ""), granted_by, note,
                         decided_at, expires_at, granted_by, request_id),
                    )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise
        return True


class CapabilityPolicy:
    """Decide one capability request. Fails closed; never resolves a credential."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        registry_loader: Callable[[], ConnectorRegistry] = load_registry,
        paid_capability_ids: frozenset[str] = PAID_CAPABILITY_IDS,
    ) -> None:
        self._clock = clock or SystemClock()
        self._registry_loader = registry_loader
        self._paid_capability_ids = paid_capability_ids

    # -- floor inputs --------------------------------------------------------

    def is_paid(self, capability: Capability) -> bool:
        """Whether calling this capability costs money."""
        return capability.id in self._paid_capability_ids

    def facts_for(
        self,
        capability_id: str,
        *,
        in_approved_scope: bool,
        lane_allowed: bool,
    ) -> tuple[Capability | None, PolicyFacts]:
        """Resolve all eight floor inputs for one capability id."""
        registry = self._registry_loader()
        try:
            capability: Capability | None = registry.capability(capability_id)
        except (ConnectorError, KeyError):
            capability = None
        group = registry.groups.get(capability.group) if capability is not None else None
        return capability, PolicyFacts(
            known=capability is not None,
            callable_now=bool(capability and capability.callable_now),
            read_only=bool(capability and capability.resolved_read_only),
            in_approved_scope=in_approved_scope,
            lane_allowed=lane_allowed,
            paid=bool(capability and self.is_paid(capability)),
            # An outbound communication is a comms-group capability that is not a
            # pure read. Kept separate from the read_only conjunct so a receipt
            # can distinguish "this writes" from "this messages a human".
            comms_write=bool(
                capability and capability.group == "comms" and not capability.resolved_read_only
            ),
            danger_group=bool(group is not None and group.danger),
        )

    def _assert_floor_grantable(self, capability_ids: Sequence[str]) -> dict[str, str]:
        """Re-read the registry immediately before issuing, and refuse off-floor ids.

        Independent of :meth:`facts_for`: this is what makes "mark a paid
        capability auto-grantable" produce a ``policy_violation`` instead of a
        live grant. Returns each id's action class for the audit log.
        """
        registry = self._registry_loader()
        classes: dict[str, str] = {}
        for cap_id in capability_ids:
            try:
                cap = registry.capability(cap_id)
            except (ConnectorError, KeyError) as exc:
                raise PolicyViolation(f"{cap_id} is not in the registry") from exc
            if not cap.callable_now:
                raise PolicyViolation(f"{cap_id} has no reviewed call path")
            if not cap.resolved_read_only:
                raise PolicyViolation(f"{cap_id} is not read-only")
            if self.is_paid(cap):
                raise PolicyViolation(f"{cap_id} is metered and can never ride the floor")
            group = registry.groups.get(cap.group)
            if group is not None and group.danger:
                raise PolicyViolation(f"{cap_id} belongs to danger group {cap.group!r}")
            classes[cap_id] = cap.action_class.value
        return classes

    # -- receipts ------------------------------------------------------------

    def _receipt(
        self,
        row: dict[str, Any],
        facts: Sequence[tuple[str, PolicyFacts]] = (),
    ) -> PolicyReceipt:
        return PolicyReceipt(
            request_id=str(row["request_id"]),
            state=str(row["state"]),
            reason_code=str(row["reason_code"] or ""),
            next_action=str(row["next_action"]),
            grant_decision_ms=row["grant_decision_ms"],
            decided_at=row["decided_at"],
            expires_at=row["expires_at"],
            granted_capability_ids=tuple(json.loads(row["granted_ids_json"] or "[]")),
            excluded_capability_ids=tuple(json.loads(row["excluded_ids_json"] or "[]")),
            facts=tuple(facts),
        )

    def _reread(
        self,
        decisions: CapabilityDecisions,
        request_id: str,
        facts: Sequence[tuple[str, PolicyFacts]] = (),
    ) -> PolicyReceipt:
        row = decisions.get(request_id)
        if row is None:  # pragma: no cover -- the row was just written.
            raise KeyError(request_id)
        return self._receipt(row, facts)

    # -- the decision --------------------------------------------------------

    def decide(
        self,
        store: Any,
        request_id: str,
        *,
        holder_agent_id: str,
        subject_kind: str = "tool",
        subject_id: str,
        capability_ids: Sequence[str] | None = None,
        excluded_ids: Sequence[str] = (),
        in_approved_scope: bool,
        lane_allowed: bool,
    ) -> PolicyReceipt:
        """Open (or replay) one request, then grant on the floor, refuse, or park.

        ``capability_ids`` defaults to ``(subject_id,)`` -- the ``tool`` case. A
        ``key_scope`` request passes the ids its group expanded to plus the
        members it EXCLUDED, so the receipt can name them. Passing an empty
        expansion is a typed refusal, never a silent no-op.
        """
        ids = tuple(capability_ids) if capability_ids is not None else (subject_id,)
        decisions = CapabilityDecisions(store)
        opened = decisions.open(
            request_id,
            holder_agent_id=holder_agent_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            opened_at=_iso(self._clock.now()),
        )
        if opened["state"] in TERMINAL_STATES:
            # Idempotent replay: a duplicate delivery returns the prior decision.
            return self._receipt(opened)

        started = time.perf_counter()
        facts: list[tuple[str, PolicyFacts]] = [
            (
                cap_id,
                self.facts_for(
                    cap_id, in_approved_scope=in_approved_scope, lane_allowed=lane_allowed
                )[1],
            )
            for cap_id in ids
        ]

        def elapsed_ms() -> int:
            return max(0, round((time.perf_counter() - started) * 1000))

        decided_at = _iso(self._clock.now())

        if not ids:
            # A key_scope group that expanded to nothing. Refusing it typed --
            # and naming every member it could not grant -- is the difference
            # between an answer and a pretend grant.
            decisions.cas_terminal(
                request_id,
                state="hard_rejected",
                reason_code="empty_scope_expansion",
                next_action="continue_degraded",
                grant_decision_ms=elapsed_ms(),
                decided_at=decided_at,
                excluded_ids=excluded_ids,
            )
            return self._reread(decisions, request_id, facts)

        unknown = [cap_id for cap_id, fact in facts if not fact.known]
        if unknown:
            decisions.cas_terminal(
                request_id,
                state="hard_rejected",
                reason_code="unknown_capability",
                next_action="stop_terminal",
                grant_decision_ms=elapsed_ms(),
                decided_at=decided_at,
                excluded_ids=unknown,
            )
            return self._reread(decisions, request_id, facts)

        dead_ends = [cap_id for cap_id, fact in facts if not fact.callable_now]
        if dead_ends:
            # Declared but unreviewed. Parking a caller for 90 seconds on
            # something that CANNOT be called afterwards is the dead end this
            # refuses: an operator approval would not make it callable.
            decisions.cas_terminal(
                request_id,
                state="hard_rejected",
                reason_code="no_call_path",
                next_action="continue_degraded",
                grant_decision_ms=elapsed_ms(),
                decided_at=decided_at,
                excluded_ids=dead_ends,
            )
            return self._reread(decisions, request_id, facts)

        blocked = [(cap_id, fact) for cap_id, fact in facts if not fact.floor_allows]
        if blocked:
            # Park. The row stays pending with next_action 'await_operator' until
            # an operator decides or the 90s wall CAS-es it to timed_out. The
            # blocking conjunct is written down so the operator reads the same
            # typed reason the caller was handed.
            conjuncts = sorted({name for _, fact in blocked for name in fact.blocking_conjuncts})
            decisions.note_park_reason(
                request_id, reason_code=f"requires_operator:{','.join(conjuncts)}"[:120]
            )
            return self._reread(decisions, request_id, facts)

        try:
            action_classes = self._assert_floor_grantable(ids)
        except PolicyViolation as exc:
            # The floor said yes and the registry says no. Something upstream is
            # lying about a cost class or an action class, so this is louder than
            # a park: terminal, named, and with no broker resolution behind it.
            LOG.error("capability floor refused at issue time for %s: %s", request_id, exc)
            decisions.cas_terminal(
                request_id,
                state="denied",
                reason_code="policy_violation",
                next_action="continue_degraded",
                grant_decision_ms=elapsed_ms(),
                decided_at=decided_at,
                excluded_ids=ids,
            )
            return self._reread(decisions, request_id, facts)

        decisions.cas_grant(
            request_id,
            capability_ids=ids,
            action_classes=action_classes,
            granted_by="autogrant",
            grant_decision_ms=elapsed_ms(),
            decided_at=decided_at,
            expires_at=_iso(self._clock.now() + timedelta(seconds=AUTO_GRANT_TTL_SECONDS)),
            note="capability floor auto-grant",
            excluded_ids=excluded_ids,
        )
        return self._reread(decisions, request_id, facts)

    def park(
        self,
        store: Any,
        request_id: str,
        *,
        holder_agent_id: str,
        subject_kind: str,
        subject_id: str,
    ) -> PolicyReceipt:
        """Open a request the floor may NEVER admit, for an operator to decide.

        Skills and extra agents are the two cases: neither is a connector
        capability, so there is no registry row to resolve eight floor inputs
        against, and §7's table escalates both to an operator by default. They
        take the same 90-second wall and the same terminal vocabulary as
        everything else -- what they never take is an automatic yes.
        """
        if subject_kind not in {"skill", "extra_agent"}:
            raise ValueError("park() is for the operator-only request kinds")
        decisions = CapabilityDecisions(store)
        row = decisions.open(
            request_id,
            holder_agent_id=holder_agent_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            opened_at=_iso(self._clock.now()),
        )
        if row["state"] == PENDING_STATE:
            decisions.note_park_reason(
                request_id, reason_code=f"requires_operator:{subject_kind}"
            )
            row = decisions.get(request_id) or row
        return self._receipt(row)

    def enforce_caller_wall(self, store: Any, request_id: str) -> PolicyReceipt:
        """Release a caller parked past 90 seconds with a typed next move.

        Measured from the durable ``opened_at``, so a restarted caller reaches the
        same verdict. Idempotent: a request that already reached a terminal state
        (including one an operator decided in time) is returned unchanged.
        """
        decisions = CapabilityDecisions(store)
        row = decisions.get(request_id)
        if row is None:
            raise KeyError(request_id)
        if row["state"] in TERMINAL_STATES:
            return self._receipt(row)
        waited = (self._clock.now() - _parse_iso(str(row["opened_at"]))).total_seconds()
        if waited < CALLER_WALL_SECONDS:
            return self._receipt(row)
        decisions.cas_terminal(
            request_id,
            state="timed_out",
            reason_code="caller_wall_90s",
            # S7-2: never keep parking, never auto-grant on silence. The caller
            # escalates a tier and carries the typed reason into the next attempt.
            next_action="escalate_tier",
            grant_decision_ms=round(waited * 1000),
            decided_at=_iso(self._clock.now()),
        )
        return self._reread(decisions, request_id)

    def operator_decide(
        self,
        store: Any,
        request_id: str,
        *,
        decision: str,
        operator: str,
        capability_ids: Sequence[str] | None = None,
        next_action: str | None = None,
        reason_code: str = "",
    ) -> PolicyReceipt:
        """Apply a human decision to a parked request, or lose the CAS.

        The operator is the ONLY authority above the floor, and must be named:
        ``human:<operator>``. If the 90-second wall already timed the request out,
        this LOSES -- no grant is written and the timed-out receipt is returned
        unchanged. The approval is not lost; it mints a fresh request id.

        ``capability_ids`` defaults to the request's own subject, which is the
        capability id for a ``tool`` request. A caller deciding a ``key_scope``
        park passes the server-side expansion: an operator never types a set of
        ids that the service did not derive.
        """
        if not operator.startswith("human:") or len(operator) <= len("human:"):
            raise ValueError("an operator decision must name a human:<operator> principal")
        if decision not in {"granted", "denied"}:
            raise ValueError("an operator may only grant or deny")

        decisions = CapabilityDecisions(store)
        row = decisions.get(request_id)
        if row is None:
            raise KeyError(request_id)
        if row["state"] in TERMINAL_STATES:
            return self._receipt(row)

        started = time.perf_counter()
        ids = tuple(capability_ids) if capability_ids is not None else (str(row["subject_id"]),)
        decided_at = _iso(self._clock.now())

        if decision == "denied":
            decisions.cas_terminal(
                request_id,
                state="denied",
                reason_code=reason_code or "operator_denied",
                next_action=next_action or "continue_degraded",
                grant_decision_ms=max(0, round((time.perf_counter() - started) * 1000)),
                decided_at=decided_at,
            )
            return self._reread(decisions, request_id)

        # An operator MAY approve above the floor -- that is what parking is for.
        # What no authority can do is grant something with no call path or no
        # registry entry, so those two checks stay.
        registry = self._registry_loader()
        classes: dict[str, str] = {}
        for cap_id in ids:
            try:
                cap = registry.capability(cap_id)
            except (ConnectorError, KeyError) as exc:
                raise PolicyViolation(f"{cap_id} is not in the registry") from exc
            if not cap.callable_now:
                raise PolicyViolation(f"{cap_id} has no reviewed call path")
            classes[cap_id] = cap.action_class.value

        decisions.cas_grant(
            request_id,
            capability_ids=ids,
            action_classes=classes,
            granted_by=operator,
            grant_decision_ms=max(0, round((time.perf_counter() - started) * 1000)),
            decided_at=decided_at,
            expires_at=_iso(self._clock.now() + timedelta(seconds=AUTO_GRANT_TTL_SECONDS)),
            note=f"operator grant by {operator}",
        )
        return self._reread(decisions, request_id)


__all__ = [
    "AUTO_GRANT_TTL_SECONDS",
    "CALLER_WALL_SECONDS",
    "FLOOR_CONJUNCTS",
    "NEXT_ACTIONS",
    "PAID_CAPABILITY_IDS",
    "PENDING_STATE",
    "TERMINAL_STATES",
    "CapabilityDecisions",
    "CapabilityPolicy",
    "Clock",
    "PolicyFacts",
    "PolicyReceipt",
    "PolicyViolation",
    "SystemClock",
]
