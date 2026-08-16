"""Memory-lifecycle contracts — the shared vocabulary for every memlife lane.

The pipeline these describe: real run records become **episodic events**, events
cluster into **candidates**, candidates accumulate an append-only **decision** log,
and a human graduates a candidate into a **lesson** that ``recall()`` can surface.

Design derived from `codejunkie99/agentic-stack` (Apache-2.0) — see
`third_party/agentic-stack/NOTICE.md`. The *shapes* are theirs; the validation
posture is not. A review of that codebase found eleven places where a non-result is
presented as a favourable result, so every field here is specified so that
**absent, unparseable and unknown are representable and distinct from good**:

- ``result`` has an explicit ``UNKNOWN`` member. A run whose outcome was never
  recorded is not a success, and must not be storable as one. The converse is
  also enforced: ``UNKNOWN`` is reserved for outcomes we genuinely do not have,
  so every value in ``swarm.contracts.ATTEMPT_END_REASONS`` maps to a *named*
  member (``MOVED`` exists for ``split``/``rerouted``). Filing a known
  disposition as unknown is the same defect run backwards, and it is worse than
  it looks: unknown scores ``None``, and SQLite sorts NULL **last** under the
  obvious ``ORDER BY salience DESC``, so the unknown record renders as the least
  salient thing in the store.
- ``cost_usd`` is ``float | None``. Subscription CLIs report tokens with no price;
  today a run spent $6.33 against a $4.00 cap because unknown was read as ``0.0``.
- ``salience`` is ``float | None``. Their ``salience_score`` returns ``0.0`` on a
  missing timestamp, which makes a malformed record maximally decayable — i.e. the
  broken records are the first to be thrown away. Unknown here means *kept*.
- ``pain`` and ``importance`` are ``Score | None``. They default to ``None``, not
  ``0.0``, for the same reason and one sharper one: salience is a *product*, so a
  single unrecorded factor drove the whole score to 0.0. Defaulting them to 0.0
  made every event in the store score exactly 0.0 — a 205-member recurring failure
  ranked identically to a one-off blurb (W2.2; 904 of 904 distinct archived events
  and 210 of 210 candidate rows, measured 2026-08-06T18:36Z).
- ``decisions`` has **no default**. A candidate deserialized from JSON without its
  decision history is refused, not silently treated as never-decided. Their
  ``review_state`` swallows queue-refresh errors and reports graduation success
  without writing the lesson; a missing history is exactly that bug's fingerprint.

Every model is frozen and forbids unknown fields, so a schema drift fails at the
boundary rather than being carried as a silently-ignored key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_STRICT = ConfigDict(frozen=True, extra="forbid")


class EventResult(StrEnum):
    """Outcome of a recorded action.

    ``UNKNOWN`` is a first-class member, not a fallback. Our own attempt rows carry
    ``end_reason = NULL`` while their session exited cleanly, and today the
    dashboard rendered exactly that case as "COMPLETED" for three attempts that had
    in fact been rejected in review.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"  # produced work, rejected in review — NOT a failure, NOT a success
    # The attempt ended because the WORK MOVED ELSEWHERE — split into sub-tasks or
    # rerouted to another provider. Also not a failure and not a success, and
    # distinct from DENIED: no reviewer rejected anything. Added because
    # ``swarm_attempts.end_reason`` carries ``split``/``rerouted`` as fully-known
    # terminal dispositions (``swarm.contracts.ATTEMPT_END_REASONS``,
    # ``intake.service._NON_FAILURE_END_REASONS``) and capture had no member to
    # map them to, so it filed them as UNKNOWN — recording a known outcome as
    # unrecorded, which is this module's own defect class inverted.
    MOVED = "moved"
    UNKNOWN = "unknown"


class CandidateStatus(StrEnum):
    """Lifecycle position of a clustered claim awaiting human judgement."""

    STAGED = "staged"
    GRADUATED = "graduated"
    REJECTED = "rejected"
    REOPENED = "reopened"
    QUARANTINED = "quarantined"  # unparseable/suspect input — retained, never silently dropped


class DecisionAction(StrEnum):
    STAGE = "stage"
    GRADUATE = "graduate"
    REJECT = "reject"
    REOPEN = "reopen"
    QUARANTINE = "quarantine"


class LessonStatus(StrEnum):
    """Only ``ACCEPTED`` lessons may enter a prompt.

    The accepted-only filter is load-bearing: a provisional or retracted lesson
    reaching a planning prompt is an unreviewed claim presented as knowledge.
    """

    PROVISIONAL = "provisional"
    ACCEPTED = "accepted"
    RETRACTED = "retracted"


Score = Annotated[float, Field(ge=0.0, le=10.0)]


class Provenance(BaseModel):
    """Where a memory came from. Ours, not theirs — their episodic records carry no
    link back to the run that produced them, so a lesson cannot be audited to its
    evidence. Without this a graduated claim is unfalsifiable."""

    model_config = _STRICT

    run_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    commit_sha: str | None = None
    source: Literal["swarm_attempt", "manual", "import", "test"] = "swarm_attempt"


class EpisodicEvent(BaseModel):
    """One recorded action, derived from ``swarm_attempts`` joined to ``sessions``.

    The join is not optional: usage lives on the session, and reading attempts alone
    yields NULL cost for every row (``dal.py`` reads
    ``COALESCE(a.usage_source, s.usage_source)``).
    """

    model_config = _STRICT

    id: str
    ts: datetime
    skill: str  # the capability exercised — e.g. "swarm.coder", "review.verify"
    action: str
    result: EventResult
    # Neither is recorded on a swarm_attempts row; both are derived by capture.py.
    # None = no signal to derive it from. NEVER 0.0 as a stand-in — salience is a
    # product and one stand-in zero annihilates recency and recurrence with it.
    pain: Score | None = None  # cost of the failure to the operator
    importance: Score | None = None  # weight of the work the event belongs to
    reflection: str = ""
    model: str | None = None
    cost_usd: float | None = None  # None = genuinely unknown. NEVER 0.0 as a stand-in.
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        # A naive timestamp silently compares wrong against tz-aware ones, which is
        # how decay windows drift. Normalise at the boundary instead.
        return v if v.tzinfo is not None else v.replace(tzinfo=UTC)

    @field_validator("cost_usd")
    @classmethod
    def _cost_not_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("cost_usd must be >= 0 or None (unknown); negative is not a cost")
        return v


class Decision(BaseModel):
    """One entry in a candidate's append-only history.

    Append-only is the point: their state machine can report a graduation that never
    wrote the lesson, and without a history there is no way to detect it after the
    fact.
    """

    model_config = _STRICT

    action: DecisionAction
    at: datetime
    actor: str  # human id, or an agent name for automated staging
    reason: str = ""

    @field_validator("at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo is not None else v.replace(tzinfo=UTC)


class Candidate(BaseModel):
    """A clustered claim awaiting human judgement.

    ``decisions`` has NO default. Deserializing a candidate without its history must
    fail loudly — a candidate that has been through the machine always has at least
    its staging entry, so an absent history means the record is truncated or forged.
    Defaulting it to ``[]`` would present a decided candidate as a fresh one.
    """

    model_config = _STRICT

    id: str
    key: str  # stable dedupe key across cycles
    claim: str
    conditions: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    cluster_size: int = Field(ge=1)
    status: CandidateStatus
    decisions: list[Decision]  # intentionally required — see the docstring
    rejection_count: int = Field(default=0, ge=0)
    salience: float | None = None  # None = not computable (e.g. missing ts). Not 0.0.

    @field_validator("claim")
    @classmethod
    def _claim_not_empty(cls, v: str) -> str:
        # Their cluster extraction can emit a canonical with claim == "", producing a
        # candidate that says nothing and cannot be reviewed meaningfully.
        if not v.strip():
            raise ValueError("claim must be non-empty; an empty claim is not a candidate")
        return v

    @field_validator("decisions")
    @classmethod
    def _history_non_empty(cls, v: list[Decision]) -> list[Decision]:
        if not v:
            raise ValueError("decisions must record at least the staging entry")
        return v


class Lesson(BaseModel):
    """A graduated claim, eligible to be rendered and recalled."""

    model_config = _STRICT

    id: str
    candidate_id: str
    claim: str
    conditions: str = ""
    status: LessonStatus
    graduated_at: datetime
    graduated_by: str
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("graduated_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo is not None else v.replace(tzinfo=UTC)

    @field_validator("claim")
    @classmethod
    def _claim_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claim must be non-empty")
        return v


class CycleStatus(StrEnum):
    """Outcome of one dream cycle.

    ``NO_INPUT`` is distinct from ``COMPLETED`` on purpose. Their cycle reports the
    success path when nothing was read at all, which is indistinguishable from a
    cycle that examined everything and found nothing to promote — the same failure
    as an API returning "0 pending" for a missing store.
    """

    COMPLETED = "completed"
    NO_INPUT = "no_input"
    FAILED = "failed"


class CycleReport(BaseModel):
    """Byte conservation is the decisive property: every input byte is kept,
    archived or quarantined. Nothing is silently dropped, which is precisely what
    their rewrite does to unparseable lines."""

    model_config = _STRICT

    status: CycleStatus
    input_bytes: int = Field(ge=0)
    kept_bytes: int = Field(ge=0)
    archived_bytes: int = Field(ge=0)
    quarantined_bytes: int = Field(ge=0)
    staged: int = Field(default=0, ge=0)
    graduated: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)

    @property
    def bytes_conserved(self) -> bool:
        return (
            self.kept_bytes + self.archived_bytes + self.quarantined_bytes == self.input_bytes
        )


__all__ = [
    "Candidate",
    "CandidateStatus",
    "CycleReport",
    "CycleStatus",
    "Decision",
    "DecisionAction",
    "EpisodicEvent",
    "EventResult",
    "Lesson",
    "LessonStatus",
    "Provenance",
    "Score",
]
