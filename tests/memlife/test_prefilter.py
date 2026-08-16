"""L2 acceptance: prefilter against already-graduated lessons.

Decisive: near-duplicates of existing lessons are dropped; novel
candidates pass through.

Named counterfeit: if the lessons source is MISSING (``None``), that is
an error — not "no duplicates". Upstream's ``passed = not reasons``
reports success when the file is absent. We raise an explicit error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    Decision,
    DecisionAction,
    Lesson,
    LessonStatus,
)
from omniagentos.memlife.prefilter import LessonsSourceMissing, prefilter

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _decision() -> Decision:
    return Decision(action=DecisionAction.STAGE, at=NOW, actor="cluster")


def _candidate(claim: str, *, cand_id: str = "cand_1") -> Candidate:
    return Candidate(
        id=cand_id,
        key=f"key/{cand_id}",
        claim=claim,
        cluster_size=2,
        status=CandidateStatus.STAGED,
        decisions=[_decision()],
    )


def _lesson(claim: str, *, lesson_id: str = "les_1") -> Lesson:
    return Lesson(
        id=lesson_id,
        candidate_id="cand_prior",
        claim=claim,
        status=LessonStatus.ACCEPTED,
        graduated_at=NOW,
        graduated_by="owner",
    )


class TestDecisiveNearDuplicateDrop:
    def test_near_duplicate_of_lesson_is_dropped(self) -> None:
        lessons = [
            _lesson("Agents cannot commit inside a sandboxed worktree"),
        ]
        candidates = [
            _candidate(
                "Agents cannot commit inside sandboxed worktrees",
                cand_id="dup",
            ),
            _candidate(
                "Always pin PYTHONHASHSEED=0 for deterministic test hashes",
                cand_id="novel",
            ),
        ]
        kept = prefilter(candidates, lessons)
        assert len(kept) == 1
        assert kept[0].id == "novel"

    def test_empty_lessons_list_passes_all_candidates(self) -> None:
        """Present-but-empty is not missing — every candidate is novel."""
        candidates = [
            _candidate("Prefer clones over worktrees", cand_id="a"),
            _candidate("Fail closed on unknown cost", cand_id="b"),
        ]
        kept = prefilter(candidates, [])
        assert [c.id for c in kept] == ["a", "b"]

    def test_identical_claim_is_dropped(self) -> None:
        claim = "Unknown timestamps must yield None not zero"
        kept = prefilter([_candidate(claim)], [_lesson(claim)])
        assert kept == []


class TestCounterfeitMissingLessonsSource:
    """THE named counterfeit for prefilter.

    Upstream treats a missing LESSONS.md as "no duplicates" because
    ``passed = not reasons`` and no reasons are collected when the file
    cannot be read. Missing must be an error here.
    """

    def test_missing_lessons_source_is_an_error_not_no_duplicates(self) -> None:
        candidates = [_candidate("Anything novel-looking")]
        with pytest.raises(LessonsSourceMissing):
            prefilter(candidates, None)  # type: ignore[arg-type]

    def test_missing_source_does_not_silently_pass_candidates(self) -> None:
        """Even more explicit: the counterfeit success path must not occur."""
        candidates = [_candidate("Would incorrectly pass if source ignored")]
        try:
            result = prefilter(candidates, None)  # type: ignore[arg-type]
        except LessonsSourceMissing:
            return  # correct
        # If we got here without raising, the bug is live.
        raise AssertionError(
            "prefilter must not return success when lessons source is missing; "
            f"got {result!r}"
        )
