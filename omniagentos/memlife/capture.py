"""Episodic capture — turn real swarm attempt rows into ``EpisodicEvent``s.

Reads ``swarm_attempts`` **joined** to ``sessions``. Usage lives on the session;
``dal.py`` resolves cost/source as ``COALESCE(a.usage_source, s.usage_source)``
and ``COALESCE(a.cost_usd, s.cost_usd)``. Reading attempts alone yields NULL
cost for nearly every row, and a wrong run-key column (``run_id`` instead of
``swarm_run_id``) makes sqlite return empty output rather than an error.

Governing rule: unknown, absent and unparseable must stay representable and
must never render as good. A NULL ``end_reason`` is ``EventResult.UNKNOWN``,
not success. A tokens-only usage marker is ``cost_usd=None``, never ``0.0``.
An outcome we cannot classify carries ``pain=None``, and an attempt with no
tier carries ``importance=None`` — never a 0.0 we did not measure.

The rule has a second edge, and it bites just as hard: a *known* outcome must
not be filed as unknown either. ``UNKNOWN`` costs the event its pain, which
costs its cluster a score, which renders it as NULL — and SQLite sorts NULL
last, so an over-eager unknown demotes the record instead of preserving it.
Every value in ``swarm.contracts.ATTEMPT_END_REASONS`` (the closed vocabulary
the migration-044 CHECK enforces on the column) therefore maps to a named
``EventResult``; ``UNKNOWN`` is reserved for NULL, empty, and values outside
that vocabulary. :func:`_map_result` is pinned to the full vocabulary by
``tests/memlife/test_capture.py::TestEndReasonVocabularyIsFullyClassified``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.memlife.contracts import EpisodicEvent, EventResult, Provenance

# Terminal end_reasons that mean the attempt did not land *because something
# broke*. Explicit set so an unrecognised value falls through to UNKNOWN rather
# than being flattered.
_FAILURE_REASONS = frozenset(
    {
        "crashed",
        "timeout",
        "killed",
        "rate_limited",
        "auth_failed",
        "blocked",
        "budget",
    }
)

# Terminal end_reasons that mean the attempt ended because the WORK MOVED, not
# because it failed: the scheduler decomposed the task (``split``, written at
# ``swarm/scheduler.py`` ``_dal.close_attempt(attempt_id, "split", ...)``) or
# handed it to another provider (``rerouted``). ``intake/service.py``
# ``_NON_FAILURE_END_REASONS`` is this codebase's existing reading of exactly
# these values, and memlife now agrees with it instead of contradicting it.
#
# These are the only two members of ``swarm.contracts.ATTEMPT_END_REASONS`` that
# used to reach the UNKNOWN fallthrough, and the cost of that was measured, not
# theoretical (live runtime DB, 2026-08-06T18:36Z): all 30 live ``split``
# attempts sit in ONE 69-member cluster (39 ``timeout`` + 30 ``split``, the
# parked-approval timeout story), and one unknown member is enough to null the
# whole candidate — it was the only NULL among 210 active candidates, ranked
# below every one-off by ``ORDER BY salience DESC``.
#
# ``superseded`` is intake's third non-failure reason but is deliberately absent:
# migration 044's CHECK does not permit it on ``swarm_attempts``, so mapping it
# here would be classifying a value this column cannot hold.
_MOVED_REASONS = frozenset({"split", "rerouted"})

# SQL mirrors dal.SwarmStore.attempts_with_usage: attempt wins where present,
# session backfills the rest. Column is swarm_run_id — not run_id.
#
# ``a.ended_at IS NOT NULL`` is load-bearing, not an optimisation. Without it an
# **in-flight** attempt is captured as ``EventResult.UNKNOWN`` with ``pain=None``
# and staged as a candidate with ``salience=NULL``. When that attempt later
# completes, the terminal row carries the same ``a.id``, so it derives the same
# event id and the same cluster id — and ``db.stage_candidate``'s
# ``ON CONFLICT(id) DO NOTHING`` then refuses the row that finally has a real
# score. Unknown becomes PERMANENT: a later correct value cannot repair the
# wrong one. An episodic memory records what *happened*; an attempt that has not
# finished has not happened yet, so it is not captured at all.
_CAPTURE_SQL = """
SELECT
    a.id              AS attempt_id,
    a.swarm_run_id    AS swarm_run_id,
    a.board_task_id   AS board_task_id,
    a.seq             AS seq,
    a.session_id      AS session_id,
    a.provider        AS provider,
    a.model           AS model,
    a.tier            AS tier,
    a.started_at      AS started_at,
    a.ended_at        AS ended_at,
    a.end_reason      AS end_reason,
    a.detail          AS detail,
    COALESCE(a.cost_usd, s.cost_usd)           AS cost_usd,
    COALESCE(a.usage_source, s.usage_source)   AS usage_source
FROM swarm_attempts a
LEFT JOIN sessions s ON s.id = a.session_id
WHERE a.ended_at IS NOT NULL
ORDER BY a.started_at ASC, a.board_task_id ASC, a.seq ASC
"""

_TOKENS_ONLY = "cli-report-tokens-only"

# ---------------------------------------------------------------------------
# pain / importance — derived, because no attempt row records them
# ---------------------------------------------------------------------------
#
# ``swarm_attempts`` has no pain or importance column, and this module used to
# populate neither. Every captured event therefore inherited the old contract
# default of 0.0, and because salience is a product that zero annihilated the
# whole score. Measured on the live store and runtime DB, 2026-08-06T18:36Z:
# 904 of 904 distinct archived events (910 archive lines, 6 of them duplicate
# ids) and 210 of 210 candidate rows scored exactly 0.0. There are 210 candidate
# rows and 210 ``cand_*.json`` files; the 211th file in ``candidates/`` is
# ``queue.json``, which is not a candidate. An earlier denominator counted it,
# which wrongly implied one candidate had escaped the collapse. None did.
#
# Two rules govern the derivation below.
#
#   1. Only signals the row actually carries may be used, and where the row
#      carries no signal the value is ``None``. A derived number we cannot
#      justify from the row is a fabrication, and fabricating one is how this
#      defect was created in the first place.
#   2. The numbers claim ORDERING, not precision. ``contracts.Score`` fixes the
#      scale at 0–10 and salience is INTENDED to be consumed only for ranking —
#      as of 2026-08-06T18:36Z nothing consumes it at all (see the note on
#      :func:`_resolve_importance`) — so each ladder is spaced evenly (2/5/8)
#      about the scale midpoint, with headroom at both ends for a finer future
#      classification to slot in without reordering.
#
# The two ladders are MULTIPLIED, so the bands cross between tiers: a success on
# complex work (8 x 2 = 16) outranks a denial on simple work (2 x 5 = 10), and a
# denial on complex work outranks a failure on simple work. Within a single tier
# the outcome ordering is strict. That crossing is the intended meaning of a
# multiplicative importance weight, not a leak: it says a lesson drawn from
# consequential work earns its place in a brief over a rejection on a trivial
# one. A model where outcome always dominates would need importance as a
# tie-breaker rather than a factor — that is a scoring redesign, not this fix.

_PAIN_BY_RESULT: dict[EventResult, float] = {
    # A landed attempt still spent tokens and wall-clock: least painful known
    # outcome, but not costless. It must also not be 0.0 — the product would
    # annihilate the score, and the reference design's own hook is tuned so
    # that repeated successes can still graduate (AGENTIC-STACK-PLAN.md:52-55).
    EventResult.SUCCESS: 2.0,
    # Produced work, rejected in review: machine cost plus a reviewer's time.
    EventResult.DENIED: 5.0,
    # Work moved elsewhere (split/rerouted): machine cost spent, nothing landed
    # from this attempt, and the work still has to be done — but no reviewer time
    # and nothing broke. Deliberately the SAME band as DENIED: the claim is that
    # the two are comparable in operator cost, not that they are identical. The
    # ladder has headroom either side (3/4, 6/7) if evidence ever justifies
    # separating them; inventing a distinction now would be a fabricated number,
    # which is the failure mode this module exists to avoid.
    EventResult.MOVED: 5.0,
    # Nothing landed: full cost, no output, and the task must be retried.
    EventResult.FAILURE: 8.0,
    # EventResult.UNKNOWN is deliberately absent — see _resolve_pain.
}

# ``swarm_attempts.tier`` is the swarm complexity band
# (``omniagentos/swarm/scheduler.py`` ``TIER_LADDER``). Named here rather than
# imported to keep memlife free of a dependency on the scheduler.
#
# Why tier and not ``board_tasks.swarm_json -> risk_class``, which is the better
# fit semantically. Measured against the live runtime DB
# (``var/runtime/state.sqlite3``) on 2026-08-06T17:45Z, weighted by ATTEMPT
# because that is what capture actually joins to — 904 attempts over 466 tasks:
#
#     risk_class:  none 878 (97.1%) | deploy 25 (2.8%) | external 1 (0.1%)
#     tier:        complex 640 (70.8%) | simple 200 (22.1%) | standard 64 (7.1%)
#
# risk_class IS populated — an earlier reading of the wrong database said it was
# empty, and that reading was wrong. It is rejected for a different and still
# decisive reason: it does not discriminate. 97.1% of attempts share one value,
# so as an importance factor it would be a near-constant multiplier and would
# contribute nothing to the ordering of 878 of 904 events. tier populates three
# bands with no NULLs and actually separates the corpus. If the planner ever
# starts assigning risk_class with real variety it becomes the better signal and
# this decision should be re-measured, not assumed.
#
# Caveat, stated rather than hidden: the ladder also *escalates* on retry, so a
# thrice-retried simple task reports tier=complex. That conflates declared
# complexity with escalation depth. Both readings point the same way — work the
# system had to throw more at matters more — but it is not a clean measurement.
_IMPORTANCE_BY_TIER: dict[str, float] = {
    "simple": 2.0,
    "standard": 5.0,
    "complex": 8.0,
}


def capture_events(
    db_path: str | Path,
    since: datetime | str | None = None,
) -> list[EpisodicEvent]:
    """Load episodic events from **terminal** ``swarm_attempts`` ⋈ ``sessions``.

    In-flight attempts (``ended_at IS NULL``) are not events yet and are
    excluded — see the note on ``_CAPTURE_SQL``.

    Parameters
    ----------
    db_path:
        Path to the runtime (or fixture) SQLite database.
    since:
        Optional **inclusive** lower bound on event time (``ended_at``). Naive
        datetimes are treated as UTC. ISO-8601 strings are accepted. ``None``
        means no lower bound.

        Inclusive is deliberate. The only production caller passes the capture
        watermark, i.e. the timestamp of the newest event already captured, so
        an exclusive bound looks tempting — but attempt timestamps are stored at
        *second* resolution and 50 of 904 live terminal attempts share a second
        with another attempt, across 20 colliding seconds (re-measured
        2026-08-06T18:36Z on a ``VACUUM INTO`` copy of the runtime DB). An
        exclusive bound would silently drop the co-timed siblings of the
        boundary row, trading duplicate capture for permanent data loss.
        Duplicates are suppressed by event **id** instead, in
        ``dream.run_dream_cycle``, which cannot lose a distinct attempt.
    """
    path = Path(db_path)
    since_dt = _parse_since(since)

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_CAPTURE_SQL).fetchall()
    finally:
        conn.close()

    events: list[EpisodicEvent] = []
    for row in rows:
        event = _row_to_event(dict(row))
        if since_dt is not None and event.ts < since_dt:
            continue
        events.append(event)
    return events


def _row_to_event(row: dict[str, Any]) -> EpisodicEvent:
    attempt_id = str(row["attempt_id"])
    end_reason = row.get("end_reason")
    if end_reason is not None:
        end_reason = str(end_reason) if end_reason != "" else None

    ts = _event_ts(row.get("ended_at"), row.get("started_at"))
    provider = str(row["provider"] or "unknown")
    skill = f"swarm.{provider}"
    action = str(end_reason) if end_reason else "attempt"
    model = row.get("model")
    model_str = str(model) if model is not None else None
    detail = row.get("detail") or ""

    result = _map_result(end_reason)

    return EpisodicEvent(
        id=attempt_id,
        ts=ts,
        skill=skill,
        action=action,
        result=result,
        pain=_resolve_pain(result),
        importance=_resolve_importance(row.get("tier")),
        reflection=str(detail) if detail else "",
        model=model_str,
        cost_usd=_resolve_cost(row.get("cost_usd"), row.get("usage_source")),
        provenance=Provenance(
            run_id=_optional_str(row.get("swarm_run_id")),
            attempt_id=attempt_id,
            session_id=_optional_str(row.get("session_id")),
            source="swarm_attempt",
        ),
    )


def _map_result(end_reason: str | None) -> EventResult:
    """Map attempt ``end_reason`` without flattering unknowns into success — or
    filing knowns as unknown.

    - ``completed`` → SUCCESS
    - ``review_denied`` → DENIED (produced work, rejected — not failure)
    - crashed / timeout / killed (and other explicit failure modes) → FAILURE
    - ``split`` / ``rerouted`` → MOVED (the work went elsewhere — not failure)
    - NULL / absent / outside the column's vocabulary → UNKNOWN

    The first four bullets exhaust ``swarm.contracts.ATTEMPT_END_REASONS``, so
    UNKNOWN now means what it says. That exhaustiveness is a ratchet, not a
    coincidence: ``test_capture.py`` fails if a new end_reason is added to the
    swarm contract without a classification here, because the silent cost of
    missing one is a permanently NULL cluster.
    """
    if end_reason is None:
        return EventResult.UNKNOWN
    if end_reason == "completed":
        return EventResult.SUCCESS
    if end_reason == "review_denied":
        return EventResult.DENIED
    if end_reason in _FAILURE_REASONS:
        return EventResult.FAILURE
    if end_reason in _MOVED_REASONS:
        return EventResult.MOVED
    # Outside the closed vocabulary the column is allowed to hold: genuinely
    # unclassifiable, and certainly not success.
    return EventResult.UNKNOWN


def _resolve_pain(result: EventResult) -> float | None:
    """Operator cost of the attempt's outcome, or None when the outcome is unknown.

    The outcome is the only signal on an attempt row that speaks to what the
    failure cost, so it is the only thing pain is derived from.
    ``EventResult.UNKNOWN`` means we do not know *whether* it failed, so we
    cannot know what it cost: that is ``None``, and the event's salience is
    then unknown-and-kept rather than a flattering 0.0.

    Because ``_map_result`` classifies every value the column may hold, this
    returns ``None`` only for an attempt whose ``end_reason`` is NULL/empty or
    outside migration 044's CHECK — never for a disposition the scheduler
    actually recorded.
    """
    return _PAIN_BY_RESULT.get(result)


def _resolve_importance(tier: Any) -> float | None:
    """Weight of the work from ``swarm_attempts.tier``, or None when absent.

    The column is nullable, though no live row currently uses that (0 NULL
    tiers across 904 terminal attempts, re-measured 2026-08-06T18:36Z on a
    ``VACUUM INTO`` copy of ``var/runtime/state.sqlite3``). An unrecognised
    or absent tier is no signal — not the bottom of the scale. Ranking a
    tierless event last would be a claim we have no basis for.

    .. note:: Salience has **no consumer yet** (measured 2026-08-06T18:36Z:
       ``store.refresh_queue`` sorts pending by candidate id,
       ``api/routes/memlife.py`` contains no ``ORDER BY`` and no reference to
       salience, and neither does the dashboard). Every claim here about how the
       number will be read is forward-looking. **Contract for the first
       consumer:** it must NOT rank with a bare ``ORDER BY salience DESC``.
       SQLite sorts NULL LAST, so an unknown score would render as the least
       salient record in the store — the precise inversion this lane exists to
       remove. Use ``ORDER BY salience IS NULL, salience DESC`` (or surface
       unknowns in their own band); unknown means *unranked*, not *worst*.
    """
    if tier is None:
        return None
    key = str(tier).strip().lower()
    return _IMPORTANCE_BY_TIER.get(key)


def _resolve_cost(cost_usd: Any, usage_source: Any) -> float | None:
    """Dollar cost, or None when the provider never reported a price.

    ``cli-report-tokens-only`` means tokens were reported without a dollar
    figure. A legacy ``cost_usd=0.0`` left on the attempt must not become a
    claim that the run was free — that is the F-3 hole this field exists to
    close.
    """
    source = str(usage_source) if usage_source is not None else None
    if source == _TOKENS_ONLY:
        return None
    if cost_usd is None:
        return None
    return float(cost_usd)


def _event_ts(ended_at: Any, started_at: Any) -> datetime:
    raw = ended_at if ended_at not in (None, "") else started_at
    if raw in (None, ""):
        # No timestamp at all — represent as epoch-UTC rather than inventing
        # "now", which would look like a fresh successful observation.
        return datetime(1970, 1, 1, tzinfo=UTC)
    return _parse_iso(str(raw))


def _parse_since(since: datetime | str | None) -> datetime | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    return _parse_iso(str(since))


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = ["capture_events"]
