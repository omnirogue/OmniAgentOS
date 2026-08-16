"""Salience scoring for episodic events.

Derived from agentic-stack ``salience.py`` design shape
(``recency × (pain/10) × (importance/10) × min(recurrence, 3)``);
behaviour modified: any missing or unusable **factor** returns ``None``
(unknown → kept), never ``0.0``. Upstream's ``0.0`` makes malformed
records the first to be decayed away — destroying the evidence that
something is wrong.

That rule covers all three of ``ts``, ``pain`` and ``importance``. It
originally covered only ``ts``: ``pain`` and ``importance`` were read as
``float(getattr(event, "pain", 0.0) or 0.0)``, writing an unrecorded score
down as a measured zero. Because the score is a **product**, that single
zero annihilated recency and recurrence too, so every event in the live
store scored exactly 0.0 and nothing could be ranked at all — 904 of 904
distinct archived events (910 archive lines, 6 of them duplicate ids) and
210 of 210 candidate rows, measured 2026-08-06T18:36Z on a ``VACUUM INTO``
copy of ``var/runtime/state.sqlite3`` plus the live memlife store.

.. note:: The product combinator is itself suspect and deliberately left
   alone in this lane. See :func:`salience_score` for the argument and the
   follow-up.

.. note:: ``None`` means *unranked*, not *worst*. As of 2026-08-06T18:36Z
   nothing consumes salience, so the first consumer sets the convention:
   it must not use a bare ``ORDER BY salience DESC``, because SQLite sorts
   NULL last and would render every unknown as the least salient record in
   the store — reintroducing this defect one layer up.

Pure function only: no I/O, no clock reads (``now`` is an argument).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

# 90-day linear decay window (matches the agentic-stack decay horizon).
_DECAY_DAYS = 90.0

# Bounds of ``contracts.Score``. A value outside them did not come from our
# scale, so it is unusable — the same posture as a non-datetime ``ts``.
_SCORE_MIN = 0.0
_SCORE_MAX = 10.0


def _as_aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _usable_timestamp(event: Any) -> datetime | None:
    """Return a usable datetime, or None when absent/unparseable.

    Unknown is representable and distinct from good: callers keep the
    record rather than treating it as maximally decayable.
    """
    if not hasattr(event, "ts"):
        return None
    ts = event.ts
    if ts is None:
        return None
    if not isinstance(ts, datetime):
        return None
    return _as_aware(ts)


def _usable_score(event: Any, field: str) -> float | None:
    """Return a usable ``contracts.Score``, or None when absent/unknown/unusable.

    Exactly the posture of :func:`_usable_timestamp`, applied to the two sibling
    factors that sit beside ``ts`` in the same expression. Absent attribute,
    ``None``, non-numeric and out-of-scale all mean *unknown*.

    A **measured** ``0.0`` is not unknown and is returned as ``0.0``: fixing
    "unknown reads as zero" must not ship "zero reads as unknown" in its place.
    """
    if not hasattr(event, field):
        return None
    raw = getattr(event, field)
    if raw is None:
        return None
    # bool is an int subclass; True/False is not a score.
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    # NaN fails both comparisons, so it is rejected here too.
    if not (_SCORE_MIN <= value <= _SCORE_MAX):
        return None
    return value


# Timestamps passed to this function must be timezone-aware.
def salience_score(
    event: Any,
    now: datetime,
    *,
    recurrence: int = 1,
) -> float | None:
    """Score how much an event should resist decay.

    Parameters
    ----------
    recurrence
        How many times this claim was observed. Callers that score a *cluster*
        must pass the cluster size; the default of 1 is for scoring a single
        standalone event. ``dream._attach_salience`` passes ``cluster_size``.

    Returns
    -------
    float
        Product of recency, pain/10, importance/10, and min(recurrence, 3).
    None
        Any factor missing or unusable — unknown means *kept*, not 0.0.

    Notes
    -----
    The product combinator is retained here but is not obviously correct: any
    zero factor annihilates the score, so a genuine measured ``pain = 0.0``
    (a clean success) discards recency, importance and recurrence entirely and
    is indistinguishable in rank from the least significant record in the store.
    Changing it is a scoring redesign with its own acceptance criteria, so it is
    left as a documented follow-up rather than smuggled into a defect fix.
    """
    ts = _usable_timestamp(event)
    if ts is None:
        return None

    pain = _usable_score(event, "pain")
    importance = _usable_score(event, "importance")
    if pain is None or importance is None:
        # A factor we never measured is not a factor of zero. Returning 0.0 here
        # is what made every record in the store equally decayable.
        return None

    now_aware = _as_aware(now)
    age_days = max(0.0, (now_aware - ts).total_seconds() / 86400.0)
    recency = max(0.0, 1.0 - age_days / _DECAY_DAYS)

    capped_recurrence = min(max(int(recurrence), 0), 3)

    return recency * (pain / 10.0) * (importance / 10.0) * capped_recurrence


def candidate_salience(
    events_by_id: Mapping[str, Any],
    evidence_ids: Sequence[str],
    *,
    cluster_size: int,
    now: datetime,
) -> float | None:
    """Aggregate a candidate's salience from its member events.

    The mean of the member scores, or ``None`` when any member is missing or
    unscorable. One unknown member makes the candidate unknown — the candidate
    is still *kept*, it is simply not ranked on evidence we do not have.

    This is the single implementation shared by the live dream cycle
    (``dream._attach_salience``) and the backlog re-score (``backfill``). They
    must not drift: a backfill that scored differently from the live pipeline
    would put a ranking discontinuity between old and new candidates, which is
    exactly the kind of silent inconsistency this module exists to avoid.
    """
    scores: list[float] = []
    for eid in evidence_ids:
        event = events_by_id.get(eid)
        if event is None:
            return None
        score = salience_score(event, now, recurrence=cluster_size)
        if score is None:
            return None
        scores.append(score)
    if not scores:
        return None
    return sum(scores) / len(scores)


__all__ = ["candidate_salience", "salience_score"]
