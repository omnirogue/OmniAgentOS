"""The frozen vocabulary of :mod:`selfloop` — every name two modules would both need.

This module imports nothing from the rest of the package and nothing outside the
standard library, and every other module in ``selfloop`` may import it. That is
not an aesthetic preference: an earlier decomposition of this package put the
tool types in ``tools.py`` and the learning types in ``learn.py``, and the result
was a genuine import cycle (``tools`` needed ``receipts.guarded``; ``receipts``
needed ``LoopTool`` to call ``tool.verify``). The rule that came out of that is
the rule this file exists to enforce: **if two modules both need a type, the type
lives here.**

What is deliberately NOT here:

* No behaviour that touches the world. Nothing in this file opens a socket, reads
  a file, spawns a process, or asks a clock what time it is.
* No sealing of tool callables. :class:`LoopTool` carries the *raw* callable and
  :meth:`ToolRegistry.register` runs it through the module-level ``_SEALER`` hook,
  which the execution-seam module installs at import time via
  :func:`install_sealer`. Keeping the seal out of ``__post_init__`` is what lets
  this module stay dependency-free while the seam still owns the seal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypedDict

# ---------------------------------------------------------------------------
# Risk tiers and action classes
# ---------------------------------------------------------------------------


class RiskTier(IntEnum):
    """Operator-facing risk tier of a loop tool.

    An ``IntEnum`` because the ordering is trust-significant and the gate
    compares it with ``>=``: **T2 and above always park for a human**, whatever
    the caller's :class:`PolicyPort` would allow on its own. A policy tuned for
    an interactive session can reasonably auto-execute a CONSEQUENTIAL action —
    there is a human watching the terminal. An unattended loop has nobody
    watching, so it takes the stricter of the two rules and the floor lives in
    this package rather than in the caller's policy adapter.
    """

    T0 = 0  # read-only / local inspection
    T1 = 1  # reversible internal mutation (local write, allowlisted restart)
    T2 = 2  # externally visible effect (outbound send, dispatch, publish)
    T3 = 3  # irreversible / money / customer-facing


#: Tier at (and above) which an effect ALWAYS parks for a human, regardless of
#: what the caller's policy adapter returned. A ``PolicyPort`` can make T0/T1
#: stricter; it can never lower this, because this constant is read by
#: ``selfloop.policy`` and not by the adapter.
APPROVAL_FLOOR_TIER = RiskTier.T2


class ActionClass(StrEnum):
    """What KIND of act a tool performs, independent of who is asking.

    Vendored rather than imported so that ``selfloop`` has no dependency on the
    host system it was extracted from. Five members, down from the source
    system's six: ``sandboxed_creation`` and ``external_reversible`` were dropped
    because in the tier ladder they mapped to exactly the same consequence as
    their neighbours, and a distinction that never changes a decision is a
    distinction an integrator has to learn for nothing.

    ``ALWAYS_HUMAN`` is the addition: it is the class for an act that must be
    decided by a person even when the loop is otherwise trusted, and it exists
    so a tool can declare that property directly instead of an operator having
    to encode it as a tier-and-policy conspiracy.
    """

    READ_ONLY = "read_only"
    INTERNAL_REVERSIBLE = "internal_reversible"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"
    ALWAYS_HUMAN = "always_human"


#: Default :class:`ActionClass` for a tier. A tool may override it
#: (``LoopTool.action_class``) when it knows better, but it can only ever make
#: the consequence heavier: the gate takes the STRICTER of the tier floor and
#: the policy verdict, so declaring ``READ_ONLY`` on a T2 tool buys nothing.
TIER_ACTION_CLASS: Mapping[RiskTier, ActionClass] = MappingProxyType(
    {
        RiskTier.T0: ActionClass.READ_ONLY,
        RiskTier.T1: ActionClass.INTERNAL_REVERSIBLE,
        RiskTier.T2: ActionClass.CONSEQUENTIAL,
        RiskTier.T3: ActionClass.IRREVERSIBLE,
    }
)


class ApprovalState(StrEnum):
    """Lifecycle of one approval row.

    ``EXPIRED`` is a state and not a derived view of a timestamp because the
    distinction matters at read time: an approval row may read ``APPROVED``
    while its expiry has passed, and the reader must treat that as *not
    approved*. Expiry binds even over an explicit approval — a human authorised
    an act at a moment, not a standing permission.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Tick status and the three disjoint outcome sets
# ---------------------------------------------------------------------------


class LoopStatus(StrEnum):
    """Terminal-ish status of one loop tick, as CLAIMED by the loop itself.

    Nothing here is evidence. A tick that reports ``COMPLETED`` has said what it
    believes; ``selfloop.outcome`` composes that claim against an independently
    executed gate before anything is recorded as accepted.
    """

    RUNNING = "running"
    IDLE = "idle"  # nothing to do this tick (cheap-check monitors exit here)
    COMPLETED = "completed"
    PARKED = "parked"  # waiting on a human approval; NOT a failure
    BLOCKED = "blocked"  # cannot proceed, for a cause the SYSTEM owns; adverse
    ABORTED = "aborted"  # policy denied / approval rejected or expired
    FAILED = "failed"  # execution error


#: The tick produced the result the loop exists to produce.
FAVOURABLE_STATUSES = frozenset({LoopStatus.COMPLETED})

#: "The tick behaved, but produced no judgeable result."
#:
#: Neutral is its own bucket because both ways of collapsing it are defects that
#: have actually shipped. Counting a non-result as a REJECTION auto-paused four
#: production routines in one night — nothing was broken, they simply had
#: nothing to do, and the acceptance floor read a run of idle ticks as a run of
#: failures. Counting a non-result as an ACCEPTANCE was worse: a loop that
#: parked the same approval every single tick reported 100% acceptance across
#: ten self-graded runs while healing nothing at all, and that number was then
#: fed back in as its own training signal.
#:
#: The acceptance floor therefore removes these from BOTH the numerator and the
#: denominator, and the learning pass refuses to mine evidence from them.
NEUTRAL_STATUSES = frozenset({LoopStatus.IDLE, LoopStatus.PARKED})

#: Statuses that count AGAINST the acceptance floor.
#:
#: ``BLOCKED`` is the distinction that makes this set honest. A loop whose
#: credential has been revoked does no work, which looks *identical* to
#: ``IDLE`` — but it is a non-result the system caused and can act on, so it
#: must trip the floor and reach an operator instead of idling green forever.
#: The rule for a poll-type loop: a transient fault (network blip, 5xx, rate
#: limit) is ``IDLE`` and neutral; a persistent authorization failure (401/403,
#: revoked grant, expired refresh token) is ``BLOCKED`` and adverse.
ADVERSE_STATUSES = frozenset({LoopStatus.ABORTED, LoopStatus.BLOCKED, LoopStatus.FAILED})

#: Derived, so it can never drift from the three sets above. Retained only
#: because "must not be scored unfavourably" is a phrase readers reach for.
NON_ADVERSE_STATUSES = FAVOURABLE_STATUSES | NEUTRAL_STATUSES


def outcome_class(status: LoopStatus | str) -> Literal["favourable", "neutral", "adverse"]:
    """Three-valued class of a tick status. Fails CLOSED on anything unrecognised.

    An unknown status string is ``adverse``, never ``neutral``. The temptation is
    to treat "I do not recognise this" as "it does not count", but a status the
    package cannot classify is a status the package cannot vouch for, and a loop
    whose statuses have drifted must trip the floor rather than quietly leave the
    denominator.
    """
    if status in FAVOURABLE_STATUSES:
        return "favourable"
    if status in NEUTRAL_STATUSES:
        return "neutral"
    return "adverse"


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class LoopError(RuntimeError):
    """Base class for every failure this runtime raises deliberately."""


class TransientLoopError(LoopError):
    """A retryable fault: a network blip, lock contention, a 5xx, a rate limit.

    The engine's per-node retry allowlist is strict on purpose — only errors a
    caller has explicitly declared transient are retried, because "retry
    anything" turns one broken dependency into an outage of the loop's own
    making.
    """


class BlockedLoopError(LoopError):
    """The loop cannot proceed, and retrying will not help.

    The counterpart to :class:`TransientLoopError`, and the reason both exist: a
    poll-type loop must be able to say WHICH kind of "no work happened" it just
    had. A dead credential, a revoked grant or an expired refresh token is
    blocked — the system caused it, only the system can fix it, and it must
    count against the acceptance floor so an operator is told, instead of an
    operator watching a green loop do nothing for a week.

    The rule the source system's own loop instances violated, and the reason
    this class is in the vocabulary rather than in a template: a dead credential
    must RAISE this, never ``return []``. An empty list renders identically to
    an empty inbox.

    ``cause`` is a short machine-readable slug (``"authorization"``,
    ``"quota"``) that survives into the run's detail; ``detail`` is the human
    sentence.
    """

    def __init__(self, detail: str, *, cause: str = "blocked") -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


class EffectDenied(LoopError):
    """Policy refused the effect. The tool was NOT executed.

    Raised strictly before the callable is reached, so an operator reading this
    in a ledger knows the external system was never touched.
    """


class EffectNotApproved(LoopError):
    """A T2+ effect reached the execution seam without a valid human approval.

    The last line of defence, and duplicated with the gate node on purpose: the
    gate exists so a loop parks *early* and legibly, and this exists so a
    template that has lost its gate — by refactor, by bug, or by a counterfeit
    mutation — still executes zero unauthorised effects.
    """


class EffectStateUnknown(LoopError):
    """A receipt exists without a result — the external effect MAY have run.

    The most important distinction in this taxonomy, and it is the one people
    get wrong. This is not a failure and it is not an absence. It means a
    request may have left the process and its fate was never established: a
    timeout, a partial send, a crash between claim and completion, a verify
    predicate that raised.

    The runtime fails closed on it forever. It refuses to re-execute rather than
    risk a duplicate irreversible effect, and the only ways out are a tool that
    has opted in via ``LoopTool.replay_on_unknown`` (forbidden at T2+) or a human
    running ``reconcile`` after establishing what actually happened.
    """


class EffectUnavailable(LoopError):
    """The effect's authority was never REACHED, so the effect did not happen.

    The absence case, kept strictly separate from both of its neighbours:

    * It is NOT :class:`EffectStateUnknown`. That means "a request may have been
      issued and its fate is unestablished"; this means "provably nothing left
      this process".
    * It is NOT a failure. Nothing about the loop, its arguments or its
      candidate was judged. "We could not ask" is not "the answer was no".

    Only a side that can PROVE no request left it may raise this: no socket,
    connection refused, DNS failure, a missing credential detected before the
    first request byte. A server that ANSWERED is a refusal — decisive and
    adverse — not an absence. Once any billable work has occurred, no later
    failure may be classified here.

    Scoring absence as failure is the defect that auto-paused four production
    routines in one night. The receipt layer therefore records a terminal
    ``unavailable`` outcome for this, which frees the next attempt slot without
    consuming the business key's retry budget — one durable write, and no
    crash window between "release the claim" and "record why".
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class EffectAttemptsExhausted(EffectDenied):
    """Every attempt in this business key's budget is a recorded failure.

    An :class:`EffectDenied` because that is literally true: the retry budget —
    not policy — refused, and the tool was NOT reached. Being a subclass also
    means every template's existing denial handler parks on it, which is the
    intended behaviour. A permanently-failing effect must escalate to a human,
    not hammer an external system once per tick forever.
    """


class UnknownToolError(LoopError):
    """A node asked for a tool this instance was not granted. Absence is denial.

    The message names what IS granted, because the overwhelmingly common cause
    is a typo or a template that outgrew its instance's grant, and an operator
    should not have to read the registry's source to find that out.
    """


class SeamBypass(LoopError):
    """A tool callable was invoked outside the one execution seam.

    Raised by the sealed handle that ``selfloop.tools`` installs over every
    registered tool. See :func:`install_sealer` for what this package does and
    does not promise about that seal.
    """


class PolicyError(LoopError):
    """A :class:`PolicyPort` could not classify an action.

    It must deny. "Could not classify" is never "assume read-only" — that
    reasoning is how an unclassified action class becomes the cheapest way to
    escape the gate.
    """


class LeaseHeld(LoopError):
    """Another process holds this instance's lease. Raised, never blocked on.

    Blocking would build a queue of processes all intending to run the same
    tick, which is the double-execution this lease exists to prevent, merely
    delayed. ``holder`` carries whatever diagnostics the lease backend could read
    (pid, host, acquired-at) so an operator can tell "a peer is working" from "a
    stale file is in the way" — but note that the shipped lease deliberately
    offers NO age-based reclaim, because a threshold that can *take* a lease is
    the double-acquire race wearing a costume.
    """

    def __init__(self, detail: str, *, holder: Mapping[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.holder: Mapping[str, Any] = MappingProxyType(dict(holder or {}))


class RecursionExceeded(LoopError):
    """A tick executed more nodes than its declared budget allowed.

    Carries the path it took, because the failure this replaces — an opaque
    "recursion limit reached" from a graph library — told an operator that a
    loop had misbehaved without telling them where, and the answer was always in
    the cycle the loop had been going round.
    """

    def __init__(
        self,
        detail: str,
        *,
        steps: int = 0,
        max_steps: int = 0,
        path: tuple[str, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.steps = steps
        self.max_steps = max_steps
        self.path = path


class GateUnavailable(LoopError):
    """The independent verifier could not be RUN. It did not return a verdict.

    Distinct from a failing gate for the same reason :class:`EffectUnavailable`
    is distinct from a failed effect: a refusal that settles as "failed" feeds
    the auto-pause floor, and absence of evidence is not evidence of failure. A
    dirty or missing workspace, an unusable interpreter, a gate binary that is
    not installed — all of these are unavailability.

    A ``GateRunner`` MUST also raise this when the gate ran but collected zero
    checks. See :class:`GateReceipt`.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class EvidenceGrade(IntEnum):
    """The CHANNEL a verdict came from, typed and ordered.

    "Verify through a different channel than the actor" is a slogan until the
    channel is a value you can compare, which is the entire reason this is an
    ordered enum and not a string on a comment. The ladder:

    * ``ACTOR_NARRATIVE`` — the actor's own account of itself: an API's 200, a
      tool's ``{"success": true}``, a model's prose report. **Never a verdict.**
    * ``LOCAL_ARTIFACT`` — a path the loop itself wrote; existence and size only.
    * ``INDEPENDENT_DECODER`` — a third-party parser confirms properties the loop
      never asserted (image dimensions from a decoder, duration from a probe).
    * ``SYSTEM_OF_RECORD`` — a process supervisor, a mail provider, a URL served
      by somebody else: an authority with no stake in the loop's report.

    A verifier that can only reach ``ACTOR_NARRATIVE`` should refuse to answer
    rather than answer weakly, because a weak answer is indistinguishable from a
    strong one once it is a boolean in a ledger.
    """

    ACTOR_NARRATIVE = 0
    LOCAL_ARTIFACT = 1
    INDEPENDENT_DECODER = 2
    SYSTEM_OF_RECORD = 3


# ---------------------------------------------------------------------------
# The shared loop state
# ---------------------------------------------------------------------------

#: Newest-N cap for the accumulating audit channels in :class:`LoopState`. A loop
#: instance is long-lived — one thread id, ticked forever — so an uncapped
#: ``operator.add`` reducer grows the checkpoint without limit until the row is
#: too large to read.
LOG_CAP = 50


def _capped_add(left: list[Any], right: list[Any]) -> list[Any]:
    """``operator.add`` with a newest-N cap. See :data:`LOG_CAP`."""
    return (list(left) + list(right))[-LOG_CAP:]


class LoopState(TypedDict, total=False):
    """The one state schema every template shares.

    Domain data lives in ``data`` so that templates stay small and the runtime's
    own machinery — gate tokens, effect records, statuses — keeps a fixed,
    auditable shape that the engine, the kit and the ledger can all rely on
    without knowing anything about the template.
    """

    instance_id: str
    template: str
    params: dict[str, Any]
    tick: int
    #: Per-tick scratch. A fresh tick supplies ``data={}``, which REPLACES the
    #: channel, so every tick starts from a clean slate.
    data: dict[str, Any]
    #: Cross-tick memory. Deliberately absent from :func:`initial_state`, so a
    #: fresh tick leaves the channel untouched and a multi-tick template — one
    #: awaiting an external card, or accumulating a proposal trajectory across
    #: ticks — keeps its handle instead of restarting from zero every time.
    memo: dict[str, Any]
    status: str
    error: str | None
    #: Gate tokens keyed by effect node name: written by the gate nodes, and
    #: re-verified against the approvals store by the execution seam. The seam
    #: never trusts this channel — a token here is a hint that an approval was
    #: minted, not authority to act.
    gates: dict[str, Any]
    log: Annotated[list[str], _capped_add]
    effects: Annotated[list[dict[str, Any]], _capped_add]


def initial_state(instance_id: str, template: str, params: Mapping[str, Any]) -> LoopState:
    """A fresh input for one tick of *instance_id*.

    Note what is absent: ``memo`` and ``tick``. Both are owned by the durable
    checkpoint, and including either here would have a fresh tick overwrite the
    channel that exists precisely to survive a fresh tick.
    """
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
    """JSON-serialisable outcome of one process invocation. A claim, not a verdict.

    The CLI prints exactly one of these per tick and exits 0 unless the tick
    FAILED: a tick reports, it does not crash. Whether the claim in ``status`` is
    accepted is decided later, by ``selfloop.outcome.compose`` against a gate
    this report knows nothing about.
    """

    instance_id: str
    template: str
    status: LoopStatus
    detail: str = ""
    effects: list[dict[str, Any]] = field(default_factory=list)
    approval_id: str | None = None
    resumed: bool = False
    #: The node the tick was in when it reached this status. It carries no
    #: meaning for the acceptance floor, but it is the difference between an
    #: operator reading "parked" and reading "parked at draft_approve_send/send".
    stage: str = ""
    #: Identity of this invocation, and the join key for attribution: a
    #: ``LessonUse`` row written before the run carries it, and the outcome
    #: written after the run carries it, which is what makes the comparison an
    #: attribution rather than a correlation.
    run_id: str = ""
    tick: int = 0

    @property
    def outcome(self) -> Literal["favourable", "neutral", "adverse"]:
        """Three-valued class of this tick. See :func:`outcome_class`."""
        return outcome_class(self.status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "template": self.template,
            "status": LoopStatus(self.status).value,
            "detail": self.detail,
            "effects": list(self.effects),
            "approval_id": self.approval_id,
            "resumed": self.resumed,
            "stage": self.stage,
            "run_id": self.run_id,
            "tick": self.tick,
            "outcome": self.outcome,
            # Acceptance is FAVOURABLE only. A parked or idle tick is a
            # non-result: reported as outcome="neutral", excluded from the
            # acceptance denominator upstream, and never counted as a success.
            "accepted": self.status in FAVOURABLE_STATUSES,
        }


# ---------------------------------------------------------------------------
# Policy and gate verdicts
# ---------------------------------------------------------------------------

#: The three things the tier gate may decide.
#:
#: ``"park"`` and not ``"approve"``. The rename is not cosmetic: the gate's core
#: line reads ``if decision.requires_approval or tool.tier >= APPROVAL_FLOOR_TIER``
#: and with the old spelling its consequent read ``approve`` — which parses as
#: "grant authority" when it means the exact opposite, "stop and require a
#: human". A reviewer read it as an auto-approve and flagged it as a literal
#: misimplementation risk. No reading of ``park`` can be mistaken for granting
#: authority.
Decision = Literal["allow", "park", "deny"]


@dataclass(frozen=True)
class PolicyDecision:
    """What a caller's policy adapter said about one action class.

    Three fields, replacing a validated-model class and its dependency. It is
    deliberately weak: an adapter can only make T0/T1 *stricter*, because the T2
    floor is applied by ``selfloop.policy`` after this value is read and is not
    reachable from here.
    """

    requires_approval: bool
    reason: str = ""
    action_class: ActionClass = ActionClass.READ_ONLY


@dataclass(frozen=True)
class GateVerdict:
    """What the tier gate decided about one ``(tool, args)`` pair.

    Pure with respect to the world: computing one writes nothing, mints no
    approval row, and reaches no external system.
    """

    decision: Decision
    tier: RiskTier
    action_class: ActionClass
    reason: str

    @property
    def parks(self) -> bool:
        """True when this effect must stop and wait for a human."""
        return self.decision == "park"

    @property
    def allows(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "tier": int(self.tier),
            "tier_name": self.tier.name,
            "action_class": self.action_class.value,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Tools: verification, digests, the tool record, and the grant registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    """The verdict of an effect's verification predicate. ``ok`` is the gate.

    ``detail`` is what an operator reads when the effect is filed as failed, so
    it should name the evidence it actually looked at — "process supervisor
    reports com.example.api: not running" — never restate the intent.
    """

    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


#: An effect's verification predicate: ``(result, args) -> ok``.
#:
#: This is the "close the effect against external evidence" seam. It receives
#: what the tool RETURNED and the arguments the tool was CALLED WITH, and it is
#: expected to look somewhere the tool does not control:
#:
#: * a service restart -> ask the supervisor whether the service is running, NOT
#:   "the subprocess exited 0";
#: * a render or export -> ``Path(args["out"]).stat().st_size > 0``;
#: * a row write        -> ``store.get(args["id"]) is not None``;
#: * an HTTP call       -> ``200 <= result["status"] < 300 and
#:   result["body"]["id"] == args["id"]``.
#:
#: It may return a ``bool``, a :class:`Verification`, or a mapping carrying an
#: ``ok`` / ``verified`` / ``success`` key. It runs ONCE per executed attempt,
#: after the tool returns and outside the execution seam, and never on a replay —
#: so its cost is one probe per real effect, not one per tick.
#:
#: A predicate that RAISES is not a failure verdict. The effect ran and its
#: outcome could not be established, which is :class:`EffectStateUnknown` — fail
#: closed, exactly as a crash between claim and completion does.
VerifyFn = Callable[[Any, Mapping[str, Any]], "Verification | bool | Mapping[str, Any]"]

#: A tool's idempotency-key function maps its *arguments* to a business key — the
#: stable identity of the effect ("message 42 answered", "api restarted at
#: 12:30"), never a random id. Two ticks that mean the same effect MUST produce
#: the same key or the receipt cannot protect anything.
#:
#: Kept as a published alias because ``kit.add_effect(key_fn=...)`` takes one.
#: It is deliberately NOT a field on :class:`LoopTool`: it was one in the source
#: system, with zero call sites anywhere in the tree, because the business key
#: always came from the node that knew the business — and a required constructor
#: argument that nothing reads is the first wart a new integrator trips over.
IdempotencyKeyFn = Callable[[Mapping[str, Any]], str]

#: Attempt budget for one business key when a tool does not set its own. Below
#: the approval floor (T0/T1: local, reversible, cheap to repeat) three attempts.
#: At T2+ exactly ONE, because a tool that reports failure on an outbound send
#: has not necessarily failed to send it. Raising it is an explicit, per-tool,
#: reviewable decision.
DEFAULT_MAX_ATTEMPTS = 3
APPROVAL_FLOOR_MAX_ATTEMPTS = 1
#: Hard ceiling on any tool's declared budget. "Retry until it works" is how an
#: automated loop turns one broken dependency into an outage of its own.
MAX_ATTEMPTS_CEILING = 10


def coerce_verification(value: Any) -> Verification:
    """Normalise whatever a :data:`VerifyFn` returned into a :class:`Verification`.

    Fail-CLOSED on every shape that carries no verdict — ``None``, an empty
    mapping, a mapping with none of the recognised keys. A predicate that
    answers nothing has verified nothing, and the alternative reading ("it
    didn't complain, so it's fine") is how a verifier that was silently broken
    for a month kept signing off on effects that never happened.
    """
    if isinstance(value, Verification):
        return value
    if isinstance(value, bool):
        return Verification(ok=value)
    if value is None:
        return Verification(ok=False, detail="verification returned None")
    if isinstance(value, Mapping):
        for flag in ("ok", "verified", "success"):
            if flag in value:
                return Verification(
                    ok=bool(value[flag]),
                    detail=str(value.get("detail") or value.get("error") or ""),
                )
        return Verification(ok=False, detail=f"verification returned no verdict: {value!r}")
    return Verification(ok=bool(value))


def digest_key(*parts: Any) -> str:
    """Stable short digest for composing business keys.

    ``sort_keys`` and ``default=str`` make the digest insensitive to dict
    ordering and tolerant of values JSON cannot represent, so the same effect
    described twice produces the same key. The 24-character truncation is for
    legibility in a ledger; collision resistance at that width is far beyond
    what a per-instance business-key namespace needs.
    """
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def args_digest(args: Mapping[str, Any]) -> str:
    """Digest of the EXACT arguments an effect will be invoked with.

    Stored on the approval row at creation and re-checked at the execution seam,
    so an approval authorises the payload a human actually saw and nothing else.
    Change one argument between the decision and the execution and the approval
    stops matching — a human approves an ACTION, never a slot.
    """
    return digest_key(dict(args))


def _no_seal(tool: LoopTool) -> LoopTool:
    """The default sealer: identity.

    ``contracts`` must stay dependency-free, so it cannot itself wrap a callable
    in the execution seam's guard. Importing ``selfloop.tools`` replaces this.
    """
    return tool


#: The hook the execution seam installs. Read only by :meth:`ToolRegistry.register`.
_SEALER: Callable[[LoopTool], LoopTool] = _no_seal


def install_sealer(sealer: Callable[[LoopTool], LoopTool]) -> Callable[[LoopTool], LoopTool]:
    """Install the function that seals a tool's callable at registration time.

    ``selfloop.tools`` calls this at import, handing over a sealer that replaces
    ``tool.call`` with a closure whose only handle to the implementation is a
    cell — no attribute, no dataclass field, no ``__wrapped__`` (``functools.wraps``
    is deliberately not used, because it would re-add one). The predecessor of
    that design exposed three routes to the raw callable and a reviewer executed
    an irreversible effect through each of them.

    Read the honest limits before you rely on this. The seal is a strong
    convention plus a mechanical review suite; it is **not** an in-process memory
    boundary. ``guarded.__closure__[0].cell_contents`` still reaches the
    implementation, as do ``gc.get_referrers`` and ``ctypes``, and a module that
    kept its own name for the callable never lost it. Python offers no wrapper
    that closes those. What the seal does guarantee is that within this package
    every registered tool is invocable only through the one seam, which is
    enforced by an AST suite over ``selfloop/`` and by the counterfeit corpus.
    For untrusted tool code, run effects in a separate process.

    Returns the previous sealer so a test can restore it.
    """
    global _SEALER
    previous = _SEALER
    _SEALER = sealer
    return previous


def current_sealer() -> Callable[[LoopTool], LoopTool]:
    """The sealer that :meth:`ToolRegistry.register` will apply."""
    return _SEALER


@dataclass(frozen=True)
class LoopTool:
    """name + risk tier + the underlying callable, plus how to judge its effect.

    ``call`` is the *existing* implementation — a client library call, a sender,
    or plain stdlib. This runtime never reimplements a connector and never
    handles a credential itself.

    **The callable stored here is raw.** Sealing happens in
    :meth:`ToolRegistry.register`, which runs the tool through the module-level
    sealer that ``selfloop.tools`` installs (see :func:`install_sealer`). That
    split exists so this module can stay import-free; the practical consequence
    for a reader is that ``LoopTool(...).call`` is the implementation, while
    ``registry.get(name).call`` is the sealed guard. Construct tools, register
    them, and only ever use what ``register`` handed back.
    """

    name: str
    tier: RiskTier
    call: Callable[..., Any]
    action_class: ActionClass | None = None
    description: str = ""
    #: Opt-in only. True means "re-running this effect is harmless", which lets
    #: the receipt guard retry an UNKNOWN-state effect instead of failing closed.
    #: Forbidden at T2+ and rejected in the constructor — never an outbound send,
    #: a payment, or a merge.
    replay_on_unknown: bool = False
    #: External evidence that the effect took effect (see :data:`VerifyFn`).
    #: When declared it is checked BEFORE the receipt may be marked succeeded,
    #: and it can only make success harder: the tool's own report and this
    #: predicate must BOTH be non-adverse. A weak verifier therefore cannot
    #: launder a self-declared failure into a completed receipt.
    verify: VerifyFn | None = None
    #: Attempts allowed per business key before the loop escalates instead of
    #: retrying. ``None`` takes the tier default.
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("a tool needs a non-empty name; it is the audit trail's join key")
        if not isinstance(self.tier, RiskTier):
            raise ValueError(f"tool {self.name!r}: tier must be a RiskTier")
        if not callable(self.call):
            raise ValueError(f"tool {self.name!r}: call must be callable")
        if self.max_attempts is not None and not (
            1 <= int(self.max_attempts) <= MAX_ATTEMPTS_CEILING
        ):
            raise ValueError(
                f"tool {self.name!r}: max_attempts must be between 1 and "
                f"{MAX_ATTEMPTS_CEILING} (got {self.max_attempts!r}) — an unbounded "
                "retry is an outage generator, not a repair"
            )
        if self.verify is not None and not callable(self.verify):
            raise ValueError(f"tool {self.name!r}: verify must be callable")
        if self.replay_on_unknown and self.tier >= APPROVAL_FLOOR_TIER:
            raise ValueError(
                f"tool {self.name!r}: replay_on_unknown is forbidden at tier "
                f"{self.tier.name} — a T2+ effect must never be blind-retried, "
                "because 'unknown' at that tier means a human-visible act may "
                "already have happened"
            )

    def resolved_action_class(self) -> ActionClass:
        """The declared class, or the tier's default. Never lower than the tier's."""
        return self.action_class or TIER_ACTION_CLASS[self.tier]

    def resolved_max_attempts(self) -> int:
        """How many times ONE business key may be attempted, ever."""
        if self.max_attempts is not None:
            return int(self.max_attempts)
        if self.tier >= APPROVAL_FLOOR_TIER:
            return APPROVAL_FLOOR_MAX_ATTEMPTS
        return DEFAULT_MAX_ATTEMPTS


@dataclass
class ToolRegistry:
    """The tools ONE loop instance is granted. Absence is denial.

    Not a global registry and not a plugin system: one of these belongs to one
    :class:`~selfloop.context.LoopContext`, and a tool that is not in it cannot
    be reached by any node of that instance's templates, whatever the template
    asks for.
    """

    tools: dict[str, LoopTool] = field(default_factory=dict)

    def register(self, tool: LoopTool) -> LoopTool:
        """Grant *tool* to this instance, sealing its callable, and return the seal.

        The returned tool — not the one you passed in — is what the registry
        holds and what every caller must use. Re-registering a name is refused
        rather than merged: silently replacing a granted tool would let a later
        import quietly widen an instance's authority.
        """
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name!r} is already granted to this instance")
        sealed = _SEALER(tool)
        if not isinstance(sealed, LoopTool) or sealed.name != tool.name:
            # A sealer that hands back something else, or something under a
            # different name, has replaced the tool rather than wrapped it —
            # which is the bypass the seal exists to close.
            raise SeamBypass(
                f"the installed sealer did not return a LoopTool named {tool.name!r}; "
                "refusing to grant a tool whose identity changed during sealing"
            )
        self.tools[tool.name] = sealed
        return sealed

    def get(self, name: str) -> LoopTool:
        """Return the granted tool, or fail closed.

        A node asking for an ungranted tool never reaches a callable, so the
        underlying implementation's execution count stays zero. The message
        names what IS granted because the cause is almost always a typo or a
        template that outgrew its instance's grant.
        """
        tool = self.tools.get(name)
        if tool is None:
            raise UnknownToolError(
                f"tool {name!r} is not granted to this loop instance "
                f"(granted: {sorted(self.tools)})"
            )
        return tool

    def names(self) -> frozenset[str]:
        return frozenset(self.tools)


# ---------------------------------------------------------------------------
# Gates: what to run, and what running it produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSpec:
    """A command to EXECUTE. Never a verdict to record.

    That single constraint is what makes the outcome ledger trustworthy, and it
    costs nothing: a ``GateRunner`` takes one of these and runs it. There is no
    field on this class through which a caller can supply ``passed=True``, which
    is deliberate — the source system's evidence directory contains a
    hand-written prose blob with a ``gate_verdict`` key sitting next to the
    signed records, and the only reason it never counted is that the loader
    refuses anything it did not mint itself.

    ``env`` is an OVERLAY on the runner's environment, not a replacement, and it
    is frozen on construction: a spec whose environment can be edited after the
    receipt is minted is not a specification of anything.
    """

    command: tuple[str, ...]
    cwd: str = ""
    timeout_s: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    label: str = ""

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("a GateSpec needs a command; an empty gate verifies nothing")
        object.__setattr__(self, "command", tuple(str(part) for part in self.command))
        frozen_env = MappingProxyType({str(k): str(v) for k, v in self.env.items()})
        object.__setattr__(self, "env", frozen_env)
        if self.timeout_s <= 0:
            raise ValueError(
                f"GateSpec timeout_s must be positive (got {self.timeout_s!r}); "
                "a gate with no deadline is a loop that never reports"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "timeout_s": self.timeout_s,
            "env": dict(self.env),
            "label": self.label or " ".join(self.command),
        }


@dataclass(frozen=True)
class GateReceipt:
    """What running a :class:`GateSpec` actually produced.

    The four check counts are not telemetry. They exist so a vacuous pass is
    *refusable*: a gate that collected zero checks and exited 0 is not a passing
    gate, it is a gate that did not test anything, and it is strictly worse than
    having no gate at all — no gate settles a tick as ``neutral/uncorroborated``
    and is visibly unverified, whereas a vacuous gate settles it as accepted and
    is invisibly unverified.

    A ``GateRunner`` MUST raise :class:`GateUnavailable` when
    ``checks_collected == 0``, rather than returning a receipt with
    ``passed=True``. The source system's default gate command named a test file
    that passed regardless of what the loop had produced, and every loop seeded
    without an explicit gate settled favourable over garbage, forever.
    :attr:`is_vacuous` is provided so that check reads the same everywhere.

    ``ran_at`` is a record stamp from ``Clock.now_iso``; ``duration_s`` comes
    from ``Clock.elapsed`` deltas, because a freshness check that reads a
    wall-clock stamp can be defeated by a clock that steps.
    """

    passed: bool
    checks_collected: int = 0
    checks_passed: int = 0
    checks_skipped: int = 0
    checks_deselected: int = 0
    detail: str = ""
    ran_at: str = ""
    duration_s: float = 0.0

    @property
    def is_vacuous(self) -> bool:
        """True when the gate collected nothing, whatever its exit status said."""
        return self.checks_collected <= 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "checks_collected": self.checks_collected,
            "checks_passed": self.checks_passed,
            "checks_skipped": self.checks_skipped,
            "checks_deselected": self.checks_deselected,
            "detail": self.detail,
            "ran_at": self.ran_at,
            "duration_s": self.duration_s,
        }


# ---------------------------------------------------------------------------
# Learning: signals in, lessons out
# ---------------------------------------------------------------------------


class RecordKind(StrEnum):
    """The ``kind`` argument of every :class:`~selfloop.ports.RecordStore` call.

    One generic store keyed by ``(kind, id)`` is what makes "add a new learning
    signal" a zero-schema edit, but it buys that flexibility with an untyped
    join key — and an untyped join key spelled slightly differently in two
    modules does not raise. It creates a second, parallel namespace in which
    every ``put`` succeeds and every ``query`` from the other spelling returns
    nothing, forever, silently. That is the precise shape of the starvation this
    package exists to refuse, so the spellings are enumerated once, here, where
    the ledger, the learning pass, the CLI and the tests all read the same ones.

    Adapters store the string value; nothing requires a caller to pass a member.
    Passing one is how you find out at import time that you got it wrong instead
    of finding out at promotion time that you got nothing.
    """

    #: One attempt's terminal envelope, mirrored out of the receipt store so the
    #: ledger can be read without the receipt store's key discipline.
    RECEIPT = "receipt"
    #: An approval requested or decided, or a lease refused — anything where the
    #: system chose between proceeding and stopping.
    DECISION = "decision"
    #: The composed three-valued verdict for one run. The loop's only training
    #: label, and the record ``put_once`` protects most: a run must not be able
    #: to overwrite its own report card.
    OUTCOME = "outcome"
    #: A :class:`GateReceipt` bound to the exact content it judged.
    EVIDENCE = "evidence"
    #: A :class:`LearningSignal` mined from the settled record.
    SIGNAL = "signal"
    #: A :class:`Lesson` at any point in its lifecycle.
    LESSON = "lesson"
    #: A lesson injected into a specific run — written BEFORE that run produces
    #: an outcome, which is what makes the later comparison an attribution
    #: rather than a correlation.
    LESSON_USE = "lesson_use"
    #: Why a promoted lesson stopped being promoted.
    RETIREMENT = "retirement"
    #: A human's declaration of what actually happened to an effect whose state
    #: the machine could not establish. The only way out of a fail-closed
    #: unknown, and it is recorded as its own record so the escape is auditable.
    RECONCILIATION = "reconciliation"
    #: The learning pass's high-water mark in the event log. Advanced only after
    #: a pass completes, so a crash re-mines rather than skips.
    CURSOR = "cursor"


class LessonStatus(StrEnum):
    """Lifecycle of one candidate lesson."""

    STAGED = "staged"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    RETIRED = "retired"
    #: Promotion reached the approval gate and is waiting for a human. Not a
    #: decision — the next tick retries the promotion — which is why it is
    #: absent from :data:`DECIDED_LESSON_STATUSES`.
    PARKED = "parked"


#: Statuses a key may never be resurrected FROM.
#:
#: Staging is idempotent by id, but idempotence is not enough on its own: a key
#: that a human rejected, or that the machine retired for regression, must not
#: come back as ``staged`` the next time the same failure recurs and the same
#: cluster forms. Without this set the retirement loop is a revolving door.
DECIDED_LESSON_STATUSES = frozenset(
    {LessonStatus.PROMOTED, LessonStatus.REJECTED, LessonStatus.RETIRED}
)


def lesson_fingerprint(scope: str, claim: str, guidance: str) -> str:
    """Content fingerprint binding a promotion verdict to exact lesson content.

    The bind that closes a time-of-check/time-of-use hole with about twenty
    lines. Staging computes this and stores it on the row. Promotion re-reads
    the row, recomputes it, and SKIPS on any mismatch; recall recomputes it again
    at read time and refuses a row whose content has drifted from what was
    promoted.

    Accepting by id alone leaves a window in which the row's content changes
    between the check and the use — an evidence-append path that also touches
    the text, two learning passes overlapping, an operator editing guidance in
    the store — and unvalidated content then applies under a validated id. The
    source system had exactly that window in its proposal pipeline.

    ``\\x00`` separators, because ``scope="a"`` + ``claim="bc"`` and
    ``scope="ab"`` + ``claim="c"`` must not collide, and a NUL cannot appear in
    any of the three inputs.
    """
    payload = f"{scope}\x00{claim}\x00{guidance}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class LearningSignal:
    """One piece of after-the-fact evidence that something went wrong.

    Mined from the append-only record AFTER a tick has settled, never from a
    hook on the hot path — that is what makes the extraction pass re-runnable
    and idempotent, and it is why ``cursor`` (a monotonic event id) is a field
    and a timestamp is not.

    Two constructor rules, both enforced, both bought expensively:

    * ``scope`` and ``failure_tag`` must be non-empty. Clustering partitions by
      ``(scope, failure_tag)`` *first* and only runs token similarity within a
      partition; a signal with no structured tag cannot be partitioned, so at a
      similarity threshold of 0.3 it joins whatever trash cluster the words
      "error", "failed" and "line" have already formed, and the lesson that
      emerges is an amalgamation of contradictory fixes.
    * No signal may be derived from a NEUTRAL tick. An idle or parked tick is
      working as designed, and "a stage produced no artifact" fires on every one
      of them. Sources are restricted to: an effect that self-declared success
      but failed its independent verifier, a recorded effect failure, and an
      adverse outcome carrying a failure tag. That rule cannot be enforced in a
      constructor, so it is enforced in the sources and pinned by a counterfeit.

    And the rule that is not negotiable anywhere: no signal may be derived from
    the agent's own output text. The source system promoted knowledge by
    matching ``output_text LIKE '%fact_id=<id>%'``, so an agent that wrote
    ``fact_id=42`` into its own prose promoted fact 42.
    """

    id: str
    scope: str
    failure_tag: str
    text: str
    run_id: str
    cursor: int
    evidence_grade: EvidenceGrade = EvidenceGrade.LOCAL_ARTIFACT

    def __post_init__(self) -> None:
        if not self.scope:
            raise ValueError("a LearningSignal needs a scope; clustering partitions by it")
        if not self.failure_tag:
            raise ValueError(
                "a LearningSignal needs a failure_tag: clustering partitions by "
                "(scope, failure_tag) before it compares tokens, and an untagged "
                "signal joins every other untagged signal into one meaningless cluster"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "failure_tag": self.failure_tag,
            "text": self.text,
            "run_id": self.run_id,
            "cursor": self.cursor,
            "evidence_grade": int(self.evidence_grade),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LearningSignal:
        """Rebuild a signal from a :class:`~selfloop.ports.RecordStore` row."""
        return cls(
            id=str(payload["id"]),
            scope=str(payload["scope"]),
            failure_tag=str(payload["failure_tag"]),
            text=str(payload.get("text", "")),
            run_id=str(payload.get("run_id", "")),
            cursor=int(payload.get("cursor", 0)),
            evidence_grade=EvidenceGrade(int(payload.get("evidence_grade", 1))),
        )


@dataclass(frozen=True)
class Lesson:
    """A candidate or promoted lesson: the durable unit this package learns.

    The two metric families on this record are computed from different ledgers
    and MUST NOT be confused, because confusing them is the bug that starved the
    system this package was extracted from — 207 candidates staged, zero
    promoted, forever:

    * ``support`` and ``evidence_ids`` come from the PRE-injection ledger, and
      they are the only inputs to promotion admission.
    * ``used`` and ``helped`` come from POST-injection attribution, and they feed
      recall ranking and regression retirement only.

    The dead version required ``wilson_lower_bound(helped, used) >= threshold``
    to promote. But ``helped`` and ``used`` are written by attribution, which
    only runs after a lesson has been promoted and injected — so at first
    promotion ``used == 0``, Wilson returns 0.0, and the condition is
    unsatisfiable. A gate that is correctly wired and mathematically always
    closed. ``promote()`` must never read either counter, and there is a
    counterfeit entry that re-adds the check and requires the liveness test to
    go red.

    ``id`` is derived from the stable content ``key`` ALONE. Deriving it from the
    key plus the evidence ids — as the predecessor did — changes the id every
    time a new run contributes evidence, so an insert-once store mints a fresh
    row each time and no candidate ever accumulates support. Evidence ids are an
    appended set inside the payload, written through a compare-and-set.
    """

    id: str
    key: str
    scope: str
    claim: str
    guidance: str
    status: LessonStatus = LessonStatus.STAGED
    #: Count of DISTINCT runs contributing evidence. Not a count of signals: ten
    #: signals from one bad run are one run's worth of evidence, and treating
    #: them as ten is how a single flaky night promotes a lesson.
    support: int = 0
    evidence_ids: tuple[str, ...] = ()
    fingerprint: str = ""
    #: Risk tier of this lesson's SCOPE. T0/T1 auto-promotes on evidence; T2+
    #: routes through the same approval machinery an outbound send uses, and the
    #: tick parks. That is the package's central claim made literal: the
    #: promotion gate IS the effect gate.
    tier: RiskTier = RiskTier.T0
    created_at: str = ""
    promoted_at: str | None = None
    #: The scope's acceptance bound at the moment of promotion. Attribution
    #: compares the post-promotion bound against this and auto-retires on
    #: regression, which is a real per-lesson pre/post comparison rather than the
    #: source's global "if five runs failed anywhere, roll everything back".
    baseline: float | None = None
    used: int = 0
    helped: int = 0
    last_used_at: str | None = None

    @property
    def expected_fingerprint(self) -> str:
        """The fingerprint this row's CURRENT content implies."""
        return lesson_fingerprint(self.scope, self.claim, self.guidance)

    @property
    def fingerprint_intact(self) -> bool:
        """False when the content has drifted from what was fingerprinted.

        A row that fails this must be skipped, not repaired: recomputing the
        fingerprint to match is exactly the bind being defeated.
        """
        return bool(self.fingerprint) and self.fingerprint == self.expected_fingerprint

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "scope": self.scope,
            "claim": self.claim,
            "guidance": self.guidance,
            "status": LessonStatus(self.status).value,
            "support": self.support,
            "evidence_ids": list(self.evidence_ids),
            "fingerprint": self.fingerprint,
            "tier": int(self.tier),
            "created_at": self.created_at,
            "promoted_at": self.promoted_at,
            "baseline": self.baseline,
            "used": self.used,
            "helped": self.helped,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Lesson:
        """Rebuild a lesson from a :class:`~selfloop.ports.RecordStore` row."""
        baseline = payload.get("baseline")
        return cls(
            id=str(payload["id"]),
            key=str(payload["key"]),
            scope=str(payload["scope"]),
            claim=str(payload.get("claim", "")),
            guidance=str(payload.get("guidance", "")),
            status=LessonStatus(str(payload.get("status", LessonStatus.STAGED.value))),
            support=int(payload.get("support", 0)),
            evidence_ids=tuple(str(e) for e in payload.get("evidence_ids", ())),
            fingerprint=str(payload.get("fingerprint", "")),
            tier=RiskTier(int(payload.get("tier", 0))),
            created_at=str(payload.get("created_at", "")),
            promoted_at=payload.get("promoted_at"),
            baseline=None if baseline is None else float(baseline),
            used=int(payload.get("used", 0)),
            helped=int(payload.get("helped", 0)),
            last_used_at=payload.get("last_used_at"),
        )


__all__ = [
    "ADVERSE_STATUSES",
    "APPROVAL_FLOOR_MAX_ATTEMPTS",
    "APPROVAL_FLOOR_TIER",
    "DECIDED_LESSON_STATUSES",
    "DEFAULT_MAX_ATTEMPTS",
    "FAVOURABLE_STATUSES",
    "LOG_CAP",
    "MAX_ATTEMPTS_CEILING",
    "NEUTRAL_STATUSES",
    "NON_ADVERSE_STATUSES",
    "TIER_ACTION_CLASS",
    "ActionClass",
    "ApprovalState",
    "BlockedLoopError",
    "Decision",
    "EffectAttemptsExhausted",
    "EffectDenied",
    "EffectNotApproved",
    "EffectStateUnknown",
    "EffectUnavailable",
    "EvidenceGrade",
    "GateReceipt",
    "GateSpec",
    "GateUnavailable",
    "GateVerdict",
    "IdempotencyKeyFn",
    "LeaseHeld",
    "LearningSignal",
    "Lesson",
    "LessonStatus",
    "LoopError",
    "LoopState",
    "LoopStatus",
    "LoopTool",
    "PolicyDecision",
    "PolicyError",
    "RecordKind",
    "RecursionExceeded",
    "RiskTier",
    "RunReport",
    "SeamBypass",
    "ToolRegistry",
    "TransientLoopError",
    "UnknownToolError",
    "Verification",
    "VerifyFn",
    "args_digest",
    "coerce_verification",
    "current_sealer",
    "digest_key",
    "initial_state",
    "install_sealer",
    "lesson_fingerprint",
    "outcome_class",
]
