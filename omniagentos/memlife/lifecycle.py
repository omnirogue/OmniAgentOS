"""Memory-lifecycle state machine.

Derived from agentic-stack review_state.py; substantially rewritten.

Transitions: stage → graduate | reject → reopen, plus quarantine.
Every transition appends a :class:`Decision` (append-only, never rewritten)
and persists atomically via :class:`~omniagentos.memlife.store.MemlifeStore`.

The decisive property this module exists to enforce: graduation writes **all
three** artifacts — the lesson, the decision entry, and the refreshed queue —
or fails. Upstream ``mark_graduated`` reports success without writing the
lesson and wraps its queue refresh in ``except Exception: pass``. Partial
success is failure here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    Decision,
    DecisionAction,
    Lesson,
    LessonStatus,
    Provenance,
)
from omniagentos.memlife.store import MemlifeStore

# Legal from-status sets for each transition (excluding stage, which creates).
_GRADUATE_FROM = frozenset({CandidateStatus.STAGED, CandidateStatus.REOPENED})
_REJECT_FROM = frozenset({CandidateStatus.STAGED, CandidateStatus.REOPENED})
_REOPEN_FROM = frozenset({CandidateStatus.REJECTED})
_QUARANTINE_FROM = frozenset(
    {
        CandidateStatus.STAGED,
        CandidateStatus.REOPENED,
        CandidateStatus.REJECTED,
    }
)


class IllegalTransition(ValueError):
    """Raised when a lifecycle action is not legal from the candidate's status."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _append_decision(
    candidate: Candidate,
    *,
    action: DecisionAction,
    actor: str,
    reason: str,
    status: CandidateStatus,
    rejection_count: int | None = None,
    at: datetime | None = None,
) -> Candidate:
    decision = Decision(
        action=action,
        at=at or _now(),
        actor=actor,
        reason=reason,
    )
    updates: dict = {
        "status": status,
        "decisions": list(candidate.decisions) + [decision],
    }
    if rejection_count is not None:
        updates["rejection_count"] = rejection_count
    return candidate.model_copy(update=updates)


class Lifecycle:
    """Human-gated candidate lifecycle bound to a :class:`MemlifeStore`."""

    def __init__(self, store: MemlifeStore) -> None:
        self.store = store

    def stage(
        self,
        *,
        id: str,
        key: str,
        claim: str,
        cluster_size: int,
        actor: str,
        reason: str = "",
        conditions: str = "",
        evidence_ids: list[str] | None = None,
        salience: float | None = None,
        at: datetime | None = None,
    ) -> Candidate:
        """Create a staged candidate with its staging decision and refresh the queue."""
        when = at or _now()
        decision = Decision(
            action=DecisionAction.STAGE,
            at=when,
            actor=actor,
            reason=reason,
        )
        candidate = Candidate(
            id=id,
            key=key,
            claim=claim,
            conditions=conditions,
            evidence_ids=list(evidence_ids or []),
            cluster_size=cluster_size,
            status=CandidateStatus.STAGED,
            decisions=[decision],
            rejection_count=0,
            salience=salience,
        )
        self.store.save_candidate(candidate)
        # Queue refresh is part of the commit — never swallowed.
        self.store.refresh_queue()
        return candidate

    def graduate(
        self,
        candidate_id: str,
        actor: str,
        reason: str = "",
        *,
        lesson_id: str | None = None,
        at: datetime | None = None,
        provenance: Provenance | None = None,
    ) -> Lesson:
        """Graduate a staged/reopened candidate into an accepted lesson.

        Writes the lesson, appends the GRADUATE decision, and refreshes the
        queue. If any step fails, rolls back so no lesson remains and the
        candidate is not left GRADUATED without its lesson.
        """
        previous = self.store.load_candidate(candidate_id)
        if previous.status not in _GRADUATE_FROM:
            raise IllegalTransition(
                f"cannot graduate candidate {candidate_id!r} from status "
                f"{previous.status!r}; legal sources: "
                f"{sorted(s.value for s in _GRADUATE_FROM)}"
            )

        when = at or _now()
        updated = _append_decision(
            previous,
            action=DecisionAction.GRADUATE,
            actor=actor,
            reason=reason,
            status=CandidateStatus.GRADUATED,
            at=when,
        )
        lesson = Lesson(
            id=lesson_id or f"les_{previous.id}",
            candidate_id=previous.id,
            claim=previous.claim,
            conditions=previous.conditions,
            status=LessonStatus.ACCEPTED,
            graduated_at=when,
            graduated_by=actor,
            evidence_ids=list(previous.evidence_ids),
            provenance=provenance or Provenance(),
        )
        # All three artifacts or none — store performs the atomic commit.
        self.store.commit_graduation(
            previous=previous,
            updated=updated,
            lesson=lesson,
        )
        return lesson

    def reject(
        self,
        candidate_id: str,
        actor: str,
        reason: str = "",
        *,
        at: datetime | None = None,
    ) -> Candidate:
        previous = self.store.load_candidate(candidate_id)
        if previous.status not in _REJECT_FROM:
            raise IllegalTransition(
                f"cannot reject candidate {candidate_id!r} from status "
                f"{previous.status!r}; legal sources: "
                f"{sorted(s.value for s in _REJECT_FROM)}"
            )
        updated = _append_decision(
            previous,
            action=DecisionAction.REJECT,
            actor=actor,
            reason=reason,
            status=CandidateStatus.REJECTED,
            rejection_count=previous.rejection_count + 1,
            at=at,
        )
        self.store.save_candidate(updated)
        # Queue refresh is part of the commit — never swallowed.
        self.store.refresh_queue()
        return updated

    def reopen(
        self,
        candidate_id: str,
        actor: str,
        reason: str = "",
        *,
        at: datetime | None = None,
    ) -> Candidate:
        previous = self.store.load_candidate(candidate_id)
        if previous.status not in _REOPEN_FROM:
            raise IllegalTransition(
                f"cannot reopen candidate {candidate_id!r} from status "
                f"{previous.status!r}; legal sources: "
                f"{sorted(s.value for s in _REOPEN_FROM)}"
            )
        updated = _append_decision(
            previous,
            action=DecisionAction.REOPEN,
            actor=actor,
            reason=reason,
            status=CandidateStatus.REOPENED,
            at=at,
        )
        self.store.save_candidate(updated)
        # Queue refresh is part of the commit — never swallowed.
        self.store.refresh_queue()
        return updated

    def quarantine(
        self,
        candidate_id: str,
        actor: str,
        reason: str = "",
        *,
        at: datetime | None = None,
    ) -> Candidate:
        """Move a candidate to quarantine — retained, never silently dropped."""
        previous = self.store.load_candidate(candidate_id)
        if previous.status not in _QUARANTINE_FROM:
            raise IllegalTransition(
                f"cannot quarantine candidate {candidate_id!r} from status "
                f"{previous.status!r}; legal sources: "
                f"{sorted(s.value for s in _QUARANTINE_FROM)}"
            )
        updated = _append_decision(
            previous,
            action=DecisionAction.QUARANTINE,
            actor=actor,
            reason=reason,
            status=CandidateStatus.QUARANTINED,
            at=at,
        )
        self.store.save_quarantined(updated)
        self.store.save_candidate(updated)
        # Queue refresh is part of the commit — never swallowed.
        self.store.refresh_queue()
        return updated


__all__ = [
    "IllegalTransition",
    "Lifecycle",
]
