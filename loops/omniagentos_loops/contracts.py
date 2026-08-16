"""Frozen-ish contracts for the loop runtime: tiers, state, statuses, errors.

Kept import-light on purpose — ``omniagentos.contracts`` (pydantic) is the only
non-stdlib dependency, so the production hook and the tests can reason about
these shapes without pulling LangGraph in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Annotated, Any, TypedDict

from omniagentos.contracts import ActionClass

#: Newest-N cap for the accumulating audit channels in :class:`LoopState`. A
#: loop instance is long-lived (one thread_id, ticked forever), so an
#: unbounded ``operator.add`` reducer would grow the checkpoint without limit.
LOG_CAP = 50


class RiskTier(IntEnum):
    """Operator-facing risk tier of a loop tool.

    Ordering is trust-significant and the gate compares with ``>=``: **T2 and
    above always require a human approval**, regardless of what the ActionClass
    policy would allow on its own (AUTO mode auto-executes CONSEQUENTIAL for
    sessions; loops are unattended, so they take the stricter floor).
    """

    T0 = 0  # read-only / local inspection
    T1 = 1  # reversible internal mutation (local write, allowlisted restart)
    T2 = 2  # externally visible effect (outbound send, dispatch, publish)
    T3 = 3  # irreversible / money / customer-facing


#: Default ActionClass for a tier. A tool may override it (``LoopTool.action_class``)
#: when it knows better; it may never lower the tier's approval consequence,
#: because the gate takes the STRICTER of the two (see policy_gate.evaluate_tool).
TIER_ACTION_CLASS: dict[RiskTier, ActionClass] = {
    RiskTier.T0: ActionClass.READ_ONLY,
    RiskTier.T1: ActionClass.INTERNAL_REVERSIBLE,
    RiskTier.T2: ActionClass.CONSEQUENTIAL,
    RiskTier.T3: ActionClass.IRREVERSIBLE,
}

#: Tier at (and above) which an effect ALWAYS parks for a human.
APPROVAL_FLOOR_TIER = RiskTier.T2


class LoopStatus(StrEnum):
    """Terminal-ish status of one loop tick."""

    RUNNING = "running"
    IDLE = "idle"  # nothing to do this tick (cheap-check monitors exit here)
    COMPLETED = "completed"
    PARKED = "parked"  # waiting on a human approval; NOT a failure
    BLOCKED = "blocked"  # cannot proceed for a cause the SYSTEM owns; adverse
    ABORTED = "aborted"  # policy denied / approval rejected or expired
    FAILED = "failed"  # execution error


#: The tick produced the result the loop exists to produce.
FAVOURABLE_STATUSES = frozenset({LoopStatus.COMPLETED})

#: Statuses that mean "the tick behaved, but produced no judgeable result".
#: The acceptance floor counts these NEITHER way — they are excluded from its
#: denominator outright.
#:
#: Both collapses are defects this repo has actually shipped. Counting a
#: non-result as a REJECTION paused four routines on 2026-07-31. Counting it as
#: an ACCEPTANCE (which ``NON_ADVERSE_STATUSES`` did, by lumping COMPLETED in
#: here) let a loop that parked the same approval every tick report 100%
#: acceptance while healing nothing. Neutral is the only honest bucket.
NEUTRAL_STATUSES = frozenset({LoopStatus.IDLE, LoopStatus.PARKED})

#: Statuses that count AGAINST the acceptance floor.
#:
#: ``BLOCKED`` is the distinction the loop layer previously could not express.
#: A loop whose credential is dead does no work — which looks exactly like
#: ``IDLE`` — but it is a non-result the SYSTEM caused and can act on, so it
#: must trip the floor and reach an operator rather than idle green forever.
#: The rule for a poll-type loop: a transient fault (network blip, 5xx, rate
#: limit) is IDLE and neutral; a PERSISTENT authorization failure (401/403,
#: revoked grant, expired refresh token) is BLOCKED and adverse.
ADVERSE_STATUSES = frozenset({LoopStatus.ABORTED, LoopStatus.BLOCKED, LoopStatus.FAILED})

#: Statuses that must not be scored unfavourably. Retained for readers; it is
#: now derived, so it can never drift from the three sets above.
NON_ADVERSE_STATUSES = FAVOURABLE_STATUSES | NEUTRAL_STATUSES


class LoopError(RuntimeError):
    """Base class for loop-runtime failures."""


class TransientLoopError(LoopError):
    """A retryable fault (network blip, lock contention). See retry.py."""


class BlockedLoopError(LoopError):
    """The loop cannot proceed, and retrying will not help.

    The counterpart to :class:`TransientLoopError`, and the reason both exist:
    a poll-type loop must be able to say WHICH kind of "no work happened" it
    just had. A 5xx or a rate limit is transient — the loop steps aside as
    ``IDLE``, a neutral non-result, and tries again next tick. A dead
    credential, a revoked grant or an expired refresh token is BLOCKED: the
    system caused it, only the system can fix it, and it must count against the
    acceptance floor so an operator is told instead of watching a green loop do
    nothing forever.

    ``cause`` is a short machine-readable slug (e.g. ``"authorization"``) that
    survives into the run's detail; ``detail`` is the human sentence.
    """

    def __init__(self, detail: str, *, cause: str = "blocked") -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


class EffectDenied(LoopError):
    """Policy refused the effect. The tool was NOT executed."""


class EffectNotApproved(LoopError):
    """A T2+ effect reached the execution seam without a valid human approval.

    This is the last line of defence: it fires even when a template's
    ``policy_gate`` node has been removed, which is exactly what the counterfeit
    suite mutates.
    """


class EffectStateUnknown(LoopError):
    """A receipt exists without a result — the external effect may have run.

    Fail-closed (contracts/statemachine.md §idempotency): the runtime refuses to
    re-execute rather than risk a duplicate irreversible effect. Only tools that
    opt in via ``LoopTool.replay_on_unknown`` are re-run.
    """


class EffectAttemptsExhausted(EffectDenied):
    """Every attempt in this business key's budget is a recorded failure.

    An :class:`EffectDenied` because that is literally true: the retry budget —
    not policy — refused, and the tool was NOT reached. Being a subclass also
    means every template's existing seam handler parks on it, which is the
    intended behaviour: a permanently-failing effect must escalate to a human,
    not hammer an external system once per tick forever.
    """


class EffectUnavailable(LoopError):
    """The effect's authority was never REACHED, so the effect did not happen.

    The absence case, kept strictly separate from both of its neighbours:

    * it is NOT :class:`EffectStateUnknown` — that means "a request may have
      been issued and its fate is unestablished", and it fails closed forever;
    * it is NOT a failure — nothing about the loop, the arguments or the
      candidate was judged. "We could not ask" is not "the answer was no".

    ``gate_evidence.GateWorkspaceUnusable`` is the rule this copies, and
    ``routines_settle``'s Class P docstring is where it is written down: an
    authority that could not be reached settles NULL/NULL, loudly, out of the
    acceptance denominator, and *never* ``gate_passed=0``. Scoring absence as
    failure is the defect that auto-paused four routines on 2026-07-31.

    Because nothing happened, the receipt guard RELEASES this attempt's claim
    (``receipts._attempt``) instead of recording an outcome: an effect that did
    not occur must leave no receipt behind, or a transient outage would burn a
    business key's whole retry budget.

    Only a side that can PROVE no request left it may raise this. The parent
    seam raises it for a connect failure or a missing credential; the worker
    client raises it when no socket exists or the connection was refused.
    Everything either side cannot prove is ``EffectStateUnknown``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class EvidenceGrade(IntEnum):
    """The CHANNEL a verdict came from, typed and ordered (PLAN-R2-R5-R6 Rule E).

    "Verify through a different channel than the actor" is only a slogan until
    the channel is a value you can compare. The ladder:

    * ``ACTOR_NARRATIVE`` — the actor's own account of itself: an API's 200, a
      tool's ``{"success": true}``, a crew's prose report. **Never a verdict.**
    * ``LOCAL_ARTIFACT`` — a path the loop itself wrote; existence and size only.
    * ``INDEPENDENT_DECODER`` — a third-party parser confirms properties the
      loop never asserted (image dimensions from ``sips``/Pillow, duration from
      ``ffprobe``).
    * ``SYSTEM_OF_RECORD`` — launchd, Gmail, GitHub, a URL served by someone
      else: an authority with no stake in the loop's report.

    Registration-time enforcement of a per-tier floor is R2-T1's ``probes.py``.
    What this slice uses it for is narrower and already load-bearing: an image
    verifier reports the grade of the channel it actually reached, and refuses
    to answer at all when only ``ACTOR_NARRATIVE`` is available.
    """

    ACTOR_NARRATIVE = 0
    LOCAL_ARTIFACT = 1
    INDEPENDENT_DECODER = 2
    SYSTEM_OF_RECORD = 3


class UnknownToolError(LoopError):
    """A node asked for a tool the instance was not granted. Fail-closed."""


def _capped_add(left: list[Any], right: list[Any]) -> list[Any]:
    """``operator.add`` with a newest-N cap (see :data:`LOG_CAP`)."""
    return (list(left) + list(right))[-LOG_CAP:]


class LoopState(TypedDict, total=False):
    """The one state schema every template shares.

    Domain data lives in ``data`` so templates stay small and the bridge's
    machinery (gate tokens, receipts, statuses) has a fixed, auditable shape.
    """

    instance_id: str
    template: str
    params: dict[str, Any]
    tick: int
    #: Per-tick scratch. A fresh tick supplies ``data={}``, which REPLACES the
    #: channel — each tick starts from a clean slate.
    data: dict[str, Any]
    #: Cross-tick memory. Deliberately absent from :func:`initial_state`, so a
    #: fresh tick leaves the channel untouched and a multi-tick template (await
    #: a swarm card, watch a deadline) keeps its handle.
    memo: dict[str, Any]
    status: str
    error: str | None
    #: gate tokens keyed by effect node name — written by policy_gate nodes,
    #: re-verified against the approvals table by the execution seam.
    gates: dict[str, Any]
    log: Annotated[list[str], _capped_add]
    effects: Annotated[list[dict[str, Any]], _capped_add]


def initial_state(instance_id: str, template: str, params: dict[str, Any]) -> LoopState:
    """A fresh input for one tick of *instance_id*."""
    return LoopState(
        instance_id=instance_id,
        template=template,
        params=dict(params or {}),
        data={},
        status=LoopStatus.RUNNING.value,
        error=None,
        gates={},
        log=[],
        effects=[],
    )


@dataclass(frozen=True)
class RunReport:
    """JSON-serializable outcome of one worker invocation."""

    instance_id: str
    template: str
    status: LoopStatus
    detail: str = ""
    effects: list[dict[str, Any]] = field(default_factory=list)
    approval_id: str | None = None
    resumed: bool = False
    #: The node the tick was in when it reached this status. Free of semantics
    #: for the floor, but it is the difference between an operator reading
    #: "parked" and reading "parked at draft_approve_send/approve".
    stage: str = ""

    @property
    def outcome(self) -> str:
        """Three-valued class of this tick: favourable / neutral / adverse."""
        if self.status in FAVOURABLE_STATUSES:
            return "favourable"
        if self.status in NEUTRAL_STATUSES:
            return "neutral"
        return "adverse"

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "template": self.template,
            "status": self.status.value,
            "detail": self.detail,
            "effects": self.effects,
            "approval_id": self.approval_id,
            "resumed": self.resumed,
            "stage": self.stage,
            "outcome": self.outcome,
            # Acceptance is FAVOURABLE only. A parked or idle tick is a
            # non-result: reported here as outcome="neutral" and excluded from
            # the acceptance denominator upstream, never counted as a success.
            "accepted": self.status in FAVOURABLE_STATUSES,
        }


__all__ = [
    "ADVERSE_STATUSES",
    "APPROVAL_FLOOR_TIER",
    "FAVOURABLE_STATUSES",
    "LOG_CAP",
    "NEUTRAL_STATUSES",
    "NON_ADVERSE_STATUSES",
    "TIER_ACTION_CLASS",
    "BlockedLoopError",
    "EffectAttemptsExhausted",
    "EffectDenied",
    "EffectNotApproved",
    "EffectStateUnknown",
    "EffectUnavailable",
    "EvidenceGrade",
    "LoopError",
    "LoopState",
    "LoopStatus",
    "RiskTier",
    "RunReport",
    "TransientLoopError",
    "UnknownToolError",
    "initial_state",
]
