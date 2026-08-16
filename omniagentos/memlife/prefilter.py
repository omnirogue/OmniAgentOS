"""Drop candidates that near-duplicate already-graduated lessons.

Derived from agentic-stack ``validate.py`` prefilter design; behaviour
modified: a **missing** lessons source is an error, not "no duplicates".
Upstream's ``passed = not reasons`` reports success when the lessons
file is absent — the same non-result-as-favourable-result defect class.

Pure functions only: no file I/O. Callers load lessons and pass them in;
``None`` means the source itself is missing.
"""

from __future__ import annotations

from omniagentos.memlife.cluster import jaccard, normalise_tokens
from omniagentos.memlife.contracts import Candidate, Lesson

DEFAULT_NEAR_DUP_THRESHOLD = 0.3


class LessonsSourceMissing(ValueError):
    """Raised when ``existing_lessons`` is ``None`` — source absent.

    Distinct from an empty list, which means the source is present and
    contains no lessons (every candidate is novel).
    """


def prefilter(
    candidates: list[Candidate],
    existing_lessons: list[Lesson] | None,
    *,
    threshold: float = DEFAULT_NEAR_DUP_THRESHOLD,
) -> list[Candidate]:
    """Return candidates that are not near-duplicates of existing lessons.

    Parameters
    ----------
    candidates
        Staged claims awaiting comparison.
    existing_lessons
        Lessons already graduated. ``None`` means the source is missing
        and raises :class:`LessonsSourceMissing`. An empty list is valid
        (source present, nothing graduated yet).
    threshold
        Jaccard token similarity at or above which a candidate is
        considered a near-duplicate of a lesson and dropped.
    """
    if existing_lessons is None:
        raise LessonsSourceMissing(
            "existing_lessons source is missing; cannot treat as "
            "'no duplicates' (absent is not empty)"
        )

    if not candidates:
        return []

    lesson_token_sets = [normalise_tokens(lesson.claim) for lesson in existing_lessons]

    kept: list[Candidate] = []
    for candidate in candidates:
        cand_tokens = normalise_tokens(candidate.claim)
        is_dup = any(
            jaccard(cand_tokens, lesson_tokens) >= threshold
            for lesson_tokens in lesson_token_sets
        )
        if not is_dup:
            kept.append(candidate)
    return kept


__all__ = [
    "DEFAULT_NEAR_DUP_THRESHOLD",
    "LessonsSourceMissing",
    "prefilter",
]
