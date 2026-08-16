"""The durable record layer: seven row shapes, two write policies, one cursor.

Everything this package learns from is read back out of here, so the shapes in
this file are the only description of what a ``selfloop`` deployment actually
knows about itself. They are plain frozen dataclasses with ``as_dict()`` and
``from_payload()`` on both ends, because the storage port
(:class:`~selfloop.ports.RecordStore`) is deliberately kind-generic — it moves
``Mapping[str, Any]`` and knows nothing about lessons or receipts — and the
translation has to live somewhere a reader can find it.

**"Append-only" is not one policy, and treating it as one is a correctness bug.**
This module exposes the distinction as two named functions rather than leaving
callers to pick a ``RecordStore`` method from memory:

* :func:`write_history` (``put_once``) — a run must not be able to overwrite its
  own report card. An :class:`OutcomeRecord`, an :class:`EvidenceRecord` and a
  :class:`ReconciliationRecord` are history: the second write for the same id is
  refused and the existing row is authoritative.
* :func:`write_cache` (``put_latest``) — a fresher green must be able to
  supersede a stale one. A step receipt, a lesson counter and the learning
  cursor are caches: last write wins, and integrity comes from binding at read
  time rather than from immutability.

A store that offers only one of these is wrong for half of the records below.

**What is deliberately not here: a run manifest.** A manifest is a *projection*
over the events and receipts already written, and making it a seventh
independent writer means a run has two accounts of itself that can disagree —
at which point an operator has to decide which one to believe, and the audit
trail has stopped being an audit trail. Build the summary by reading; never by
writing it a second time.

**The event log is the only best-effort writer.** :func:`emit` cannot raise. The
durable writers can and must: a lesson that failed to persist has not been
learned, and pretending otherwise is how a loop reports progress it did not
make.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from selfloop.contracts import (
    EvidenceGrade,
    GateReceipt,
    GateSpec,
    LoopStatus,
    RecordKind,
    RiskTier,
    digest_key,
    outcome_class,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; ledger never imports a context
    from selfloop.context import LoopContext

# ---------------------------------------------------------------------------
# Small shared vocabularies
# ---------------------------------------------------------------------------

#: Terminal outcome of ONE receipted attempt, mirrored into the ledger from the
#: receipt store. Four values, and the last two are the pair that people
#: collapse and should not:
#:
#: * ``succeeded`` — the tool reported success AND its independent verifier
#:   agreed. Only this value short-circuits a replay.
#: * ``failed`` — a decisive negative. Something answered, and the answer was no.
#: * ``unknown`` — a request may have left this process and its fate was never
#:   established (a timeout, a partial send, a crash between claim and
#:   completion, a verify predicate that raised). The runtime fails closed on it
#:   forever; only a human reconciliation clears it.
#: * ``unavailable`` — the effect's authority was never reached, so provably
#:   nothing happened. It is recorded as a TERMINAL outcome rather than by
#:   deleting the claim row, because deleting is a second store call and a crash
#:   between the two leaves the row claimed-with-no-result — bricking the
#:   business key with the exact ``unknown`` state the release existed to avoid.
#:   One durable write, no window. The guard frees the next attempt slot on this
#:   value WITHOUT consuming the key's retry budget.
RECEIPT_SUCCEEDED = "succeeded"
RECEIPT_FAILED = "failed"
RECEIPT_UNKNOWN = "unknown"
RECEIPT_UNAVAILABLE = "unavailable"

#: The four values above, for a caller that wants to validate a string.
RECEIPT_OUTCOMES: frozenset[str] = frozenset(
    {RECEIPT_SUCCEEDED, RECEIPT_FAILED, RECEIPT_UNKNOWN, RECEIPT_UNAVAILABLE}
)

#: A :class:`LessonUseRecord` is written BEFORE the run it belongs to produces an
#: outcome, so it starts ``pending`` and is finalised by attribution. The two
#: states are named rather than inferred from ``helped is None`` because the
#: difference between "not attributed yet" and "attributed, did not help" is the
#: whole of the attribution signal.
LESSON_USE_PENDING = "pending"
LESSON_USE_ATTRIBUTED = "attributed"

#: Prefix of the loop's own actor string (:attr:`LoopContext.actor` is
#: ``loop:<instance>``). Any record that requires a HUMAN decider — an approval,
#: a reconciliation — refuses a ``by`` value carrying this prefix. Because the
#: loop cannot put any other string in that field, self-approval is structurally
#: impossible rather than merely forbidden.
AUTOMATION_ACTOR_PREFIX = "loop:"

#: Record id of the learning pass's high-water mark. The cursor is stored as an
#: ordinary record so it survives a restart and so an operator can read it with
#: the same tooling as everything else.
DEFAULT_CURSOR_NAME = "learning"


def _mapping(value: Any) -> dict[str, Any]:
    """Coerce a payload field back to a plain dict. Never raises on junk."""
    return dict(value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# EventRecord — the replay cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventRecord:
    """One line of the ordered event log. ``cursor`` is the whole point of it.

    The learning pass asks for "everything after cursor N", advances N only when
    a pass completes, and therefore re-mines rather than skips when it crashes
    mid-pass. That is possible because :meth:`~selfloop.ports.EventLog.append`
    returns a strictly increasing integer, and it is NOT possible with a
    timestamp: two events written in the same millisecond are unordered, so a
    time window either double-counts them or drops them, and a clock that steps
    backwards silently re-mines an arbitrary stretch of history as if it were
    new evidence.

    An ``EventLog`` adapter MUST echo the assigned cursor back in the row it
    returns from :meth:`~selfloop.ports.EventLog.read`, under the key
    ``"cursor"``. Without it a reader cannot advance past what it just read, and
    the pass loops over the same events forever. :meth:`from_payload` reads 0
    for a row that omits it, which re-mines from the beginning — the safe
    direction, and loud enough to notice.
    """

    cursor: int
    at: str
    instance_id: str
    template: str
    #: Coarse channel: ``"tick"``, ``"effect"``, ``"gate"``, ``"learning"``.
    kind: str
    #: What happened, in the loop's own vocabulary: ``"node_enter"``,
    #: ``"effect_denied"``, ``"lesson_promoted"``.
    action: str
    #: Join key back to the run this event belongs to. Empty for events that
    #: belong to the instance rather than to any one run (a lease refusal).
    run_id: str = ""
    node: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cursor": int(self.cursor),
            "at": self.at,
            "instance_id": self.instance_id,
            "template": self.template,
            "kind": self.kind,
            "action": self.action,
            "run_id": self.run_id,
            "node": self.node,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EventRecord:
        return cls(
            cursor=int(payload.get("cursor", 0) or 0),
            at=str(payload.get("at", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            kind=str(payload.get("kind", "")),
            action=str(payload.get("action", "")),
            run_id=str(payload.get("run_id", "")),
            node=str(payload.get("node", "")),
            payload=_mapping(payload.get("payload")),
        )


# ---------------------------------------------------------------------------
# ReceiptRecord — one attempt's terminal envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptRecord:
    """One ATTEMPT's terminal envelope, mirrored out of the receipt store.

    Mirrored rather than read through the receipt port because the receipt store
    is keyed by attempt key and exists to arbitrate a race; the ledger is keyed
    by nothing in particular and exists to be read. Reporting, the CLI and the
    learning pass all read this copy, and none of them can disturb the exactly-
    once bookkeeping by doing so.

    ``verified`` is three-valued and the third value is the interesting one.
    ``True`` and ``False`` are what an independent verifier RULED; ``None`` means
    no verifier was declared, or it did not run. ``None`` is never to be read as
    "fine" — an effect whose independent check did not happen has not been
    checked, and :func:`selfloop.learn` mines precisely the disagreement between
    ``declared_success`` and ``verified`` as a learning signal, which it cannot
    do if absence has been laundered into agreement.
    """

    #: The attempt key: ``<business_key>``, ``<business_key>#a2``, ... Attempt
    #: scoping is what stops a retry re-opening a row back into ``claimed``,
    #: which would be indistinguishable from the crash window.
    id: str
    at: str
    instance_id: str
    template: str
    node: str
    tool: str
    business_key: str
    attempt: int
    #: One of :data:`RECEIPT_OUTCOMES`.
    outcome: str
    #: What the TOOL said about itself. Never a verdict on its own.
    declared_success: bool | None = None
    #: What an INDEPENDENT verifier ruled. ``None`` means it did not run.
    verified: bool | None = None
    #: Which channel the verdict came from. An ``ACTOR_NARRATIVE`` grade on a
    #: ``succeeded`` row is a receipt that is only as good as the tool's opinion
    #: of itself, and it should be visible as such in the ledger.
    evidence_grade: EvidenceGrade = EvidenceGrade.ACTOR_NARRATIVE
    #: Digest of the exact arguments the effect was invoked with, so a receipt
    #: can be matched against the approval that authorised it.
    args_digest: str = ""
    tier: RiskTier = RiskTier.T0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "instance_id": self.instance_id,
            "template": self.template,
            "node": self.node,
            "tool": self.tool,
            "business_key": self.business_key,
            "attempt": int(self.attempt),
            "outcome": self.outcome,
            "declared_success": self.declared_success,
            "verified": self.verified,
            "evidence_grade": int(self.evidence_grade),
            "args_digest": self.args_digest,
            "tier": int(self.tier),
            "detail": self.detail,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReceiptRecord:
        declared = payload.get("declared_success")
        verified = payload.get("verified")
        return cls(
            id=str(payload["id"]),
            at=str(payload.get("at", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            node=str(payload.get("node", "")),
            tool=str(payload.get("tool", "")),
            business_key=str(payload.get("business_key", "")),
            attempt=int(payload.get("attempt", 1) or 1),
            outcome=str(payload.get("outcome", RECEIPT_UNKNOWN)),
            declared_success=None if declared is None else bool(declared),
            verified=None if verified is None else bool(verified),
            evidence_grade=EvidenceGrade(int(payload.get("evidence_grade", 0) or 0)),
            args_digest=str(payload.get("args_digest", "")),
            tier=RiskTier(int(payload.get("tier", 0) or 0)),
            detail=str(payload.get("detail", "")),
        )


# ---------------------------------------------------------------------------
# DecisionRecord — everywhere the system chose between proceeding and stopping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRecord:
    """One point at which the system chose between proceeding and stopping.

    Deliberately ONE shape for policy verdicts, approval requests, human
    approvals and rejections, expiries and lease refusals. Splitting them into a
    row type each produces four tables that have to be joined before anybody can
    answer "why did nothing happen last Tuesday?", and that question is the only
    reason the rows exist.

    ``by`` carries the identity that made the call, and it is what makes
    self-approval visible: the loop can only ever write ``loop:<instance>``
    (see :data:`AUTOMATION_ACTOR_PREFIX`), so an approval attributed to an
    automation identity is legible as one in the ledger even if some future
    caller manages to write it.
    """

    id: str
    at: str
    instance_id: str
    template: str
    node: str
    #: What was being decided: a tool name, a lesson id, an instance's lease.
    subject: str
    #: ``allow`` / ``park`` / ``deny`` from the tier gate, or an approval
    #: lifecycle value (``pending`` / ``approved`` / ``rejected`` / ``expired``),
    #: or ``refused`` for a lease. Never ``approve`` — see
    #: :data:`selfloop.contracts.Decision` for why that spelling was banned.
    decision: str
    by: str
    reason: str = ""
    tier: int | None = None
    approval_id: str | None = None
    run_id: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "instance_id": self.instance_id,
            "template": self.template,
            "node": self.node,
            "subject": self.subject,
            "decision": self.decision,
            "by": self.by,
            "reason": self.reason,
            "tier": self.tier,
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DecisionRecord:
        tier = payload.get("tier")
        return cls(
            id=str(payload["id"]),
            at=str(payload.get("at", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            node=str(payload.get("node", "")),
            subject=str(payload.get("subject", "")),
            decision=str(payload.get("decision", "")),
            by=str(payload.get("by", "")),
            reason=str(payload.get("reason", "")),
            tier=None if tier is None else int(tier),
            approval_id=payload.get("approval_id"),
            run_id=str(payload.get("run_id", "")),
            detail=_mapping(payload.get("detail")),
        )


# ---------------------------------------------------------------------------
# OutcomeRecord — the three-column split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeRecord:
    """The composed verdict for ONE run. The loop's only training label.

    Three columns, kept separate and separately named, and the separation is the
    single most valuable thing in this package:

    * :attr:`self_reported_status` — what the loop CLAIMED. A claim, never
      evidence.
    * :attr:`gate_passed` — what an EXECUTED gate RULED. ``None`` means the gate
      DID NOT RUN. Absence, never failure.
    * :attr:`outcome_class` — the composition, under may-lower-never-raise.

    Collapse those into one column and the following happens, because it did: a
    routine that parked every single tick scored an acceptance rate of 1.0
    across ten self-graded runs whose declared gate had never executed once. The
    number was then fed back in as the loop's own training signal. With the
    columns split, the same ten runs read as ten neutral rows, a
    :attr:`gate_passed` of ``NULL`` ten times over, and an acceptance floor that
    honestly reports ``meets=None`` — nothing to grade.

    :attr:`accepted` is a derived property and not a stored field, so it can
    never disagree with the columns it summarises. There is no way to write a
    row that says ``accepted`` while ``gate_passed`` is ``NULL``.
    """

    #: Normally the run id: one run, one report card.
    id: str
    run_id: str
    instance_id: str
    template: str
    at: str
    #: A :class:`~selfloop.contracts.LoopStatus` value.
    self_reported_status: str
    #: ``None`` means the gate did not run: no gate configured, a gate that
    #: raised :class:`~selfloop.contracts.GateUnavailable`, or a gate that
    #: collected zero checks (which is the same thing wearing a receipt).
    gate_passed: bool | None
    #: ``favourable`` / ``neutral`` / ``adverse``.
    outcome_class: str
    #: The learning SCOPE this run belongs to — the partition attribution and
    #: clustering are computed within. Empty means "unscoped", which is legal and
    #: means the run contributes to no scope's baseline.
    scope: str = ""
    #: Structured tag naming WHAT went wrong, e.g. ``"timeout"``,
    #: ``"auth_revoked"``. Clustering partitions by ``(scope, failure_tag)``
    #: before it compares any tokens, so an adverse row with no tag yields no
    #: signal at all — which is the correct outcome, not a gap to be filled with
    #: a placeholder.
    failure_tag: str = ""
    detail: str = ""
    #: Why the gate did not rule, when it did not. A ``GateUnavailable.reason``.
    #: Empty when a gate ruled.
    gate_unavailable_reason: str = ""
    gate_detail: str = ""
    #: How many checks the gate collected. Zero on a row whose
    #: :attr:`gate_passed` is ``NULL`` for vacuity, and that pairing is what an
    #: operator greps for when they suspect a gate has quietly stopped testing.
    checks_collected: int = 0

    @property
    def accepted(self) -> bool:
        """True only when the loop claimed success AND a gate corroborated it.

        Derived, never stored. A favourable claim with ``gate_passed is None`` is
        uncorroborated and is not an acceptance; that is the whole mechanism by
        which a loop's own optimism is stopped from becoming its training
        signal.
        """
        return self.outcome_class == "favourable" and self.gate_passed is True

    @property
    def corroborated(self) -> bool:
        """True when a gate actually ruled on this run, either way."""
        return self.gate_passed is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "template": self.template,
            "at": self.at,
            "self_reported_status": self.self_reported_status,
            "gate_passed": self.gate_passed,
            "outcome_class": self.outcome_class,
            "scope": self.scope,
            "failure_tag": self.failure_tag,
            "detail": self.detail,
            "gate_unavailable_reason": self.gate_unavailable_reason,
            "gate_detail": self.gate_detail,
            "checks_collected": int(self.checks_collected),
            "accepted": self.accepted,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutcomeRecord:
        """Rebuild from a stored row.

        ``outcome_class`` is re-derived through
        :func:`selfloop.contracts.outcome_class` when the stored value is not
        one of the three known words. That function fails closed on anything it
        does not recognise — an unclassifiable row is ``adverse``, never
        ``neutral`` — so a ledger whose vocabulary has drifted trips the floor
        and reaches an operator instead of quietly leaving the denominator.
        """
        stored_class = str(payload.get("outcome_class", ""))
        if stored_class not in ("favourable", "neutral", "adverse"):
            stored_class = outcome_class(str(payload.get("self_reported_status", "")))
        gate_passed = payload.get("gate_passed")
        return cls(
            id=str(payload["id"]),
            run_id=str(payload.get("run_id", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            at=str(payload.get("at", "")),
            self_reported_status=str(payload.get("self_reported_status", LoopStatus.FAILED.value)),
            gate_passed=None if gate_passed is None else bool(gate_passed),
            outcome_class=stored_class,
            scope=str(payload.get("scope", "")),
            failure_tag=str(payload.get("failure_tag", "")),
            detail=str(payload.get("detail", "")),
            gate_unavailable_reason=str(payload.get("gate_unavailable_reason", "")),
            gate_detail=str(payload.get("gate_detail", "")),
            checks_collected=int(payload.get("checks_collected", 0) or 0),
        )


# ---------------------------------------------------------------------------
# EvidenceRecord — a gate receipt bound to the exact content it judged
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """A :class:`~selfloop.contracts.GateReceipt` bound to what it judged.

    A receipt on its own says "something passed". This row says WHAT passed, by
    carrying a digest of the exact content the gate ruled on
    (:attr:`subject_digest`), so a later reader can recompute the digest and
    detect that the content has moved on since the verdict was minted. Without
    the bind, a verdict is transferable: it attaches to an id, and whatever
    content later occupies that id inherits a pass it never earned.

    This is HISTORY. Write it with :func:`write_history`. A run that could
    overwrite its own evidence row has an evidence ledger that proves only what
    the run most recently wanted it to prove.
    """

    id: str
    at: str
    run_id: str
    instance_id: str
    template: str
    #: What was judged, in the loop's vocabulary: a node name, an artifact path,
    #: a lesson id.
    subject: str
    #: Digest of the exact content judged. The TOCTOU bind.
    subject_digest: str
    receipt: GateReceipt
    #: The spec that was executed. ``None`` when the runner does not execute a
    #: command (an artifact gate does not).
    spec: GateSpec | None = None
    #: The CHANNEL the verdict came from. See
    #: :class:`~selfloop.contracts.EvidenceGrade`: a verdict that could only
    #: reach ``ACTOR_NARRATIVE`` should have refused to answer rather than
    #: answer weakly, because a weak answer is indistinguishable from a strong
    #: one once it is a boolean in a ledger.
    evidence_grade: EvidenceGrade = EvidenceGrade.LOCAL_ARTIFACT

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "template": self.template,
            "subject": self.subject,
            "subject_digest": self.subject_digest,
            "receipt": self.receipt.as_dict(),
            "spec": None if self.spec is None else self.spec.as_dict(),
            "evidence_grade": int(self.evidence_grade),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        receipt_row = _mapping(payload.get("receipt"))
        spec_row = _mapping(payload.get("spec"))
        command = tuple(str(part) for part in spec_row.get("command", ()))
        # ``GateSpec.as_dict`` SYNTHESISES a display label from the command when
        # none was set. Reading that back as content would silently turn a
        # rendering default into a stored field, so a label that is exactly the
        # joined command is dropped and left to be derived again.
        label = str(spec_row.get("label", ""))
        if label == " ".join(command):
            label = ""
        spec = (
            GateSpec(
                command=command,
                cwd=str(spec_row.get("cwd", "")),
                timeout_s=float(spec_row.get("timeout_s", 600.0) or 600.0),
                env={str(k): str(v) for k, v in _mapping(spec_row.get("env")).items()},
                label=label,
            )
            if command
            else None
        )
        return cls(
            id=str(payload["id"]),
            at=str(payload.get("at", "")),
            run_id=str(payload.get("run_id", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            subject=str(payload.get("subject", "")),
            subject_digest=str(payload.get("subject_digest", "")),
            receipt=GateReceipt(
                passed=bool(receipt_row.get("passed", False)),
                checks_collected=int(receipt_row.get("checks_collected", 0) or 0),
                checks_passed=int(receipt_row.get("checks_passed", 0) or 0),
                checks_skipped=int(receipt_row.get("checks_skipped", 0) or 0),
                checks_deselected=int(receipt_row.get("checks_deselected", 0) or 0),
                detail=str(receipt_row.get("detail", "")),
                ran_at=str(receipt_row.get("ran_at", "")),
                duration_s=float(receipt_row.get("duration_s", 0.0) or 0.0),
            ),
            spec=spec,
            evidence_grade=EvidenceGrade(int(payload.get("evidence_grade", 1) or 1)),
        )


# ---------------------------------------------------------------------------
# ReconciliationRecord — the audited way out of a fail-closed unknown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationRecord:
    """A human's declaration of what actually happened to an unknown effect.

    :class:`~selfloop.contracts.EffectStateUnknown` is permanent by design: the
    runtime will not re-execute an effect that may already have happened, and no
    timer changes that, because a timer observes nothing. The only way out is a
    person who went and looked at the external system. This record is that
    person's statement, and it exists as its own row so the escape hatch is
    itself audited — an unknown that was cleared should be as visible as the
    unknown that caused it.

    Two constructor rules, both fail-closed:

    * ``by`` must be a human. A value carrying :data:`AUTOMATION_ACTOR_PREFIX`
      is refused, so the loop cannot clear its own unknowns. The loop's actor
      string is the only identity it can produce, which makes this structural
      rather than a matter of the loop behaving well.
    * ``outcome`` must be decisive. ``succeeded`` or ``failed`` — nothing else.
      "Probably fine" is the state we are already in, and recording it as a
      third value would let an undecided reconciliation clear a claim that was
      never established.
    """

    id: str
    at: str
    #: The receipt attempt key whose unknown state this settles.
    receipt_key: str
    instance_id: str
    template: str
    #: ``succeeded`` or ``failed``: what the human established, at the external
    #: system, actually happened.
    outcome: str
    by: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in (RECEIPT_SUCCEEDED, RECEIPT_FAILED):
            raise ValueError(
                f"a reconciliation must be decisive: outcome must be "
                f"{RECEIPT_SUCCEEDED!r} or {RECEIPT_FAILED!r}, got {self.outcome!r} — "
                "an undecided reconciliation clears nothing and would only launder "
                "an unknown into a settled row"
            )
        if not self.by.strip():
            raise ValueError(
                "a reconciliation needs a named decider; an anonymous escape from a "
                "fail-closed unknown is not auditable"
            )
        if self.by.startswith(AUTOMATION_ACTOR_PREFIX):
            raise ValueError(
                f"a reconciliation may not be made by an automation identity "
                f"(got by={self.by!r}) — the loop cannot establish what happened at an "
                "external system it could not reach, which is the entire reason the "
                "state is unknown"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "receipt_key": self.receipt_key,
            "instance_id": self.instance_id,
            "template": self.template,
            "outcome": self.outcome,
            "by": self.by,
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReconciliationRecord:
        return cls(
            id=str(payload["id"]),
            at=str(payload.get("at", "")),
            receipt_key=str(payload.get("receipt_key", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            outcome=str(payload.get("outcome", "")),
            by=str(payload.get("by", "")),
            note=str(payload.get("note", "")),
        )


# ---------------------------------------------------------------------------
# LessonUseRecord — written before the run, finalised after it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LessonUseRecord:
    """One promoted lesson, injected into one run. Written BEFORE the outcome.

    The ordering is the whole design. Writing this row before the run produces
    its outcome is what makes the later comparison an ATTRIBUTION — we committed
    in advance to which runs the lesson was in — rather than a correlation
    fished out of history afterwards, which is a shape that always finds
    something.

    :attr:`helped` starts ``None`` and stays ``None`` until attribution runs.
    ``None`` is not ``False``: a park, an abort, an unknown effect state and an
    idle tick all produce no judgeable result, and attribution finalises this
    row only when a NON-NEUTRAL outcome exists for the run. Counting neutral
    ticks as ``used`` without ``helped`` is how a flaky weekend auto-retires a
    good lesson — the counters fill with non-results and the Wilson bound
    collapses on evidence that was never about the lesson at all.

    :attr:`fingerprint` records the exact lesson content that was injected, so
    attribution credits the text that actually ran and not whatever the lesson
    row says today.
    """

    id: str
    at: str
    lesson_id: str
    run_id: str
    instance_id: str
    template: str
    scope: str = ""
    #: :data:`LESSON_USE_PENDING` until attribution, then
    #: :data:`LESSON_USE_ATTRIBUTED`.
    state: str = LESSON_USE_PENDING
    #: ``None`` until attributed. Never defaults to ``False``.
    helped: bool | None = None
    #: The run's composed outcome class, copied in at attribution time. Empty
    #: while pending.
    outcome_class: str = ""
    #: Content fingerprint of the lesson AS INJECTED.
    fingerprint: str = ""

    @property
    def pending(self) -> bool:
        """True while this use has not yet been graded by attribution."""
        return self.state != LESSON_USE_ATTRIBUTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "lesson_id": self.lesson_id,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "template": self.template,
            "scope": self.scope,
            "state": self.state,
            "helped": self.helped,
            "outcome_class": self.outcome_class,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LessonUseRecord:
        helped = payload.get("helped")
        return cls(
            id=str(payload["id"]),
            at=str(payload.get("at", "")),
            lesson_id=str(payload.get("lesson_id", "")),
            run_id=str(payload.get("run_id", "")),
            instance_id=str(payload.get("instance_id", "")),
            template=str(payload.get("template", "")),
            scope=str(payload.get("scope", "")),
            state=str(payload.get("state", LESSON_USE_PENDING)),
            helped=None if helped is None else bool(helped),
            outcome_class=str(payload.get("outcome_class", "")),
            fingerprint=str(payload.get("fingerprint", "")),
        )


def lesson_use_id(lesson_id: str, run_id: str) -> str:
    """Deterministic id for "this lesson, in this run".

    Deterministic so the row is written exactly once even if the injection path
    runs twice within a tick, and so attribution can address the row directly
    instead of scanning for it.
    """
    return f"use_{digest_key('lesson_use', lesson_id, run_id)[:20]}"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def emit(
    ctx: LoopContext,
    kind: str,
    action: str,
    payload: Mapping[str, Any] | None = None,
    *,
    run_id: str = "",
    node: str = "",
) -> int:
    """Append one event and return its cursor. **This function never raises.**

    Observability must not be able to fail a tick. A ledger write is the one
    thing in a tick that has no business changing the tick's verdict, and if an
    exception from it escapes — a full disk, a locked database, an adapter whose
    schema drifted, a payload carrying an object the store cannot serialise — a
    tick that did its work correctly is recorded as ``FAILED``. A ``FAILED``
    tick counts against the acceptance floor, so the loop then auto-pauses
    because its *monitoring* broke. That failure mode is worse than the one it
    would be reporting.

    The price is stated plainly rather than hidden: a dropped event is evidence
    the learning pass will never see, which slows learning down. That is the
    right side of the trade, because the alternative — a false adverse verdict —
    is not merely slow, it is wrong, and it propagates into the training label.

    Returns 0 when the append did not happen. Zero is not a valid cursor; a
    caller that stores it as a high-water mark will re-mine from the beginning
    of the log, which is the safe direction (re-mining is idempotent; skipping
    is not).

    ``BaseException`` is deliberately NOT caught. ``KeyboardInterrupt`` and
    ``SystemExit`` must still stop a process; a loop you cannot interrupt is a
    worse operational problem than a loop that lost a log line.
    """
    try:
        try:
            at = ctx.clock.now_iso()
        except Exception:
            at = ""
        record = EventRecord(
            cursor=0,
            at=at,
            instance_id=ctx.instance_id,
            template=ctx.template,
            kind=str(kind),
            action=str(action),
            run_id=run_id,
            node=node,
            payload=dict(payload or {}),
        )
        row = record.as_dict()
        row.pop("cursor")  # the log assigns it; a caller-supplied one is fiction
        return int(ctx.events.append(row))
    except Exception:
        return 0


def write_history(
    ctx: LoopContext,
    kind: RecordKind | str,
    record_id: str,
    payload: Mapping[str, Any],
) -> bool:
    """Insert-if-absent. Use this for records that are HISTORY.

    An outcome, a piece of evidence, a reconciliation: a run must not be able to
    overwrite its own report card. ``False`` means a record already exists for
    this id, and that is not an error — it is the normal path on a replay, and
    the EXISTING record is authoritative. A caller that reacts to ``False`` by
    writing again under a fresh id has reintroduced exactly the problem this
    prevents: two accounts of one run, disagreeing, with nothing to say which is
    real.

    Unlike :func:`emit`, this propagates. A history record that failed to
    persist has not been recorded, and a tick that carries on as though it had
    is reporting progress it did not make.
    """
    return bool(ctx.records.put_once(str(kind), record_id, dict(payload)))


def write_cache(
    ctx: LoopContext,
    kind: RecordKind | str,
    record_id: str,
    payload: Mapping[str, Any],
) -> None:
    """Last-write-wins per slot. Use this for records that are a CACHE.

    A step receipt, a counter, a cursor: a fresher green must be able to
    supersede a stale one, and integrity comes from binding at READ time — the
    lesson fingerprint, the approval's argument digest — rather than from the
    row being immutable. When a cached row turns out not to match what the
    reader expected, the correct response is to redo the work, which is cheap;
    with history the correct response is to refuse, which is not.

    Choosing between this and :func:`write_history` is a correctness decision,
    not a style one. Write an outcome here and a re-run silently rewrites the
    past; write a cursor with :func:`write_history` and it can never advance.
    """
    ctx.records.put_latest(str(kind), record_id, dict(payload))


# ---------------------------------------------------------------------------
# Reading, and the cursor
# ---------------------------------------------------------------------------


def read_events(ctx: LoopContext, *, after: int = 0, limit: int = 500) -> list[EventRecord]:
    """Events with a cursor strictly greater than *after*, in cursor order.

    Thin on purpose: the value it adds over calling the port directly is that
    every reader in the package goes through one place that knows an event row
    is supposed to echo its ``cursor``, so the obligation on an ``EventLog``
    adapter is enforced by one failure rather than discovered in four.
    """
    return [EventRecord.from_payload(row) for row in ctx.events.read(after=after, limit=limit)]


def _cursor_record_id(name: str) -> str:
    return f"cursor_{name}"


def _stamp(ctx: LoopContext) -> str:
    """``Clock.now_iso`` for a record stamp, or ``""`` if the clock is unusable.

    A record stamp is documentation. It is never read by a freshness or
    anti-forgery check — those read ``Clock.elapsed``, which is monotonic — so a
    missing stamp degrades a row's legibility and cannot change a verdict.
    """
    try:
        return ctx.clock.now_iso()
    except Exception:
        return ""


def _as_cursor(value: Any) -> int:
    """Parse a stored cursor value. Anything unreadable is 0.

    A cursor we cannot parse is a cursor we do not have. Re-mining from the
    beginning is idempotent — extraction is keyed by signal id — whereas
    trusting a malformed high-water mark skips evidence permanently and
    silently.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_cursor(ctx: LoopContext, name: str = DEFAULT_CURSOR_NAME) -> int:
    """The learning pass's high-water mark, or 0 if it has never run.

    0 means "mine from the beginning", which is the correct default for an
    absent cursor: re-mining costs time and nothing else, while guessing a
    non-zero starting point would skip evidence nobody would ever notice was
    missing.
    """
    row = ctx.records.get(RecordKind.CURSOR.value, _cursor_record_id(name))
    return 0 if not row else _as_cursor(row.get("cursor"))


def advance_cursor(ctx: LoopContext, name: str, value: int) -> int:
    """Move the cursor forward to *value*. Returns the cursor now in force.

    Three properties, each preventing a specific failure:

    * **It never moves backwards.** A lower value is ignored, not written. A
      cursor that can go back re-mines an arbitrary stretch of settled history
      as if it were new evidence, and because candidate ids are content-stable
      that evidence lands on the same candidates and inflates their support
      without a single new run having happened. Support is a count of DISTINCT
      RUNS precisely so that cannot happen; a rewindable cursor would defeat it
      from the other end.
    * **It is a compare-and-set.** The runtime's learning pass runs unattended
      while an operator may be running the CLI, and a read-modify-write between
      the two silently drops one pass's progress. Losing the CAS is not an
      error — the winner moved the cursor further along — so this call re-reads
      and returns what is now true. The CAS expects the RAW stored value rather
      than the parsed one, so a row whose cursor is corrupt is replaced rather
      than becoming permanently unadvanceable.
    * **The caller advances it only after a pass COMPLETES.** Not enforceable
      here, but it is why this is a separate call and not something extraction
      does as it goes: a crash mid-pass must re-mine, never skip.
    """
    record_id = _cursor_record_id(name)
    kind = RecordKind.CURSOR.value
    target = int(value)

    row = ctx.records.get(kind, record_id)
    if row is None:
        if target <= 0:
            return 0
        if ctx.records.put_once(
            kind, record_id, {"id": record_id, "name": name, "cursor": target, "at": _stamp(ctx)}
        ):
            return target
        row = ctx.records.get(kind, record_id)  # somebody created it first
        if row is None:
            return read_cursor(ctx, name)

    stored = row.get("cursor")
    current = _as_cursor(stored)
    if target <= current:
        return current

    moved = ctx.records.transition(
        kind,
        record_id,
        expect={"cursor": stored},
        set={"cursor": target, "at": _stamp(ctx), "name": name},
    )
    return target if moved else read_cursor(ctx, name)


__all__ = [
    "AUTOMATION_ACTOR_PREFIX",
    "DEFAULT_CURSOR_NAME",
    "LESSON_USE_ATTRIBUTED",
    "LESSON_USE_PENDING",
    "RECEIPT_FAILED",
    "RECEIPT_OUTCOMES",
    "RECEIPT_SUCCEEDED",
    "RECEIPT_UNAVAILABLE",
    "RECEIPT_UNKNOWN",
    "DecisionRecord",
    "EventRecord",
    "EvidenceRecord",
    "LessonUseRecord",
    "OutcomeRecord",
    "ReceiptRecord",
    "ReconciliationRecord",
    "advance_cursor",
    "emit",
    "lesson_use_id",
    "read_cursor",
    "read_events",
    "write_cache",
    "write_history",
]
