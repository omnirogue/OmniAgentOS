"""Read-only work-shape diagnostics. NEVER an input to a score.

These are the numbers that explain HOW a period went — how many sessions ran,
how many ran at once, how often a merged PR passed its gate first time. Every
one of them is trivially inflatable (spawn thirty sessions, open fifty PRs), so
none of them may ever reach :mod:`omniagentos.team.scoring`. The dependency
points ONE way and only one way:

    scoring  --verified outcomes-->  diagnostics
    diagnostics  --X-->  scoring

That is why ``verified_outcomes`` is a PARAMETER of :func:`compute_diagnostics`
rather than something this module derives: the count of real outcomes is the
scorer's answer, and diagnostics only divides by it. What this module borrows
from :mod:`~omniagentos.team.scoring` is plumbing only — connection resolution
and the shared window bounds, so both modules agree on what "the period" means.
It never calls a scoring function, and scoring imports nothing from here.

Unmeasured stays None. A person with no sessions did not have an average
concurrency of 0.0 — the quantity was not measured, and a rate computed from
zero samples is a favourable fiction. Every rate here is ``None`` when its
denominator is empty, matching ``prod_snapshots``' nullable columns.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.team.scoring import ScoreSource, connection_for, end_bound, start_bound

# Concurrency is sampled on an HOUR grid. Finer resolution would imply a
# precision the started_at/ended_at stamps (second-resolution, collected by a
# sweep) do not have.
BUCKET = timedelta(hours=1)

# ``meta.state`` values that mean the PR actually landed. An OPEN or CLOSED PR is
# a request or a refusal, never a merge.
MERGED_STATES: frozenset[str] = frozenset({"MERGED"})

# Gate attempts at or below this count are a first-pass success.
FIRST_PASS_ATTEMPTS = 1


@dataclass
class Diagnostics:
    """One person's period work-shape. Descriptive only — never scored."""

    employee_id: str
    session_count: int = 0
    avg_active_sessions: float | None = None
    peak_sessions: int | None = None
    verified_outcomes: int = 0
    outcomes_per_session: float | None = None
    merged_prs: int = 0
    merged_prs_per_session: float | None = None
    first_pass_success: float | None = None
    first_pass_known: int = 0
    first_pass_unknown: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "session_count": self.session_count,
            "avg_active_sessions": self.avg_active_sessions,
            "peak_sessions": self.peak_sessions,
            "verified_outcomes": self.verified_outcomes,
            "outcomes_per_session": self.outcomes_per_session,
            "merged_prs": self.merged_prs,
            "merged_prs_per_session": self.merged_prs_per_session,
            "first_pass_success": self.first_pass_success,
            "first_pass_known": self.first_pass_known,
            "first_pass_unknown": self.first_pass_unknown,
        }


def _parse(value: str, *, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return default
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _ratio(numerator: int, denominator: int) -> float | None:
    """A rate, or None when nothing was sampled. Never 0.0-by-default."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_merged(meta: Mapping[str, Any]) -> bool:
    if str(meta.get("state") or "").strip().upper() in MERGED_STATES:
        return True
    return bool(meta.get("merged_at") or meta.get("merged"))


def _gate_attempts(meta: Mapping[str, Any]) -> int | None:
    """``meta.gate_attempts`` as an int, or None when the collector did not record it.

    None is UNKNOWN, and unknown is excluded from the first-pass denominator
    entirely. Treating a missing field as 1 would report a perfect first-pass
    rate for a collector that simply never measured it.
    """
    raw = meta.get("gate_attempts")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _session_rows(
    connection: sqlite3.Connection, start_iso: str, end_iso: str
) -> list[sqlite3.Row]:
    """Task sessions OVERLAPPING the window, joined to the card's human owner.

    ``task_sessions`` has no person column — the attempt ledger records a
    harness, not a human — so ownership comes from the card it ran against. An
    attempt on an unowned (pure agent) card belongs to nobody's diagnostics.

    Overlap, not "started inside": a session that spans the window is a session
    the person was running during it, and excluding it would make the same
    session vanish from every window it crosses.
    """
    try:
        return list(
            connection.execute(
                "SELECT s.id AS id, s.board_task_id AS board_task_id, s.session_id AS session_id, "
                "s.harness AS harness, s.model AS model, s.started_at AS started_at, "
                "s.ended_at AS ended_at, s.end_reason AS end_reason, "
                "t.owner_employee_id AS owner_employee_id "
                "FROM task_sessions s JOIN board_tasks t ON t.id = s.board_task_id "
                "WHERE t.owner_employee_id IS NOT NULL AND s.started_at <= ? "
                "AND (s.ended_at IS NULL OR s.ended_at >= ?) "
                "ORDER BY s.started_at ASC, s.id ASC",
                (end_iso, start_iso),
            ).fetchall()
        )
    except sqlite3.OperationalError:  # pragma: no cover - table exists from 043
        return []


def _concurrency(
    spans: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> tuple[float | None, int | None]:
    """Mean and peak concurrent sessions over the window, sampled hourly."""
    if not spans:
        return None, None
    counts: list[int] = []
    cursor = start
    while cursor < end:
        edge = cursor + BUCKET
        counts.append(sum(1 for began, finished in spans if began < edge and finished > cursor))
        cursor = edge
    if not counts:  # pragma: no cover - a window shorter than one bucket
        return None, None
    return sum(counts) / len(counts), max(counts)


def compute_diagnostics(
    source: ScoreSource,
    *,
    period_start: str,
    period_end: str,
    verified_outcomes: Mapping[str, int] | None = None,
) -> dict[str, Diagnostics]:
    """Work-shape numbers per person for ``[period_start, period_end]``.

    ``verified_outcomes`` is the SCORER's count of counted cards per person; it
    is passed in rather than derived so the arrow between the two modules stays
    one-directional and visible at the call site.
    """
    connection = connection_for(source)
    start_iso = start_bound(period_start)
    end_iso = end_bound(period_end)
    start = _parse(start_iso, default=datetime.now(UTC))
    end = _parse(end_iso, default=datetime.now(UTC))
    outcomes = dict(verified_outcomes or {})

    from omniagentos.team.scoring import ROSTER_SQL

    roster = [str(row["id"]) for row in connection.execute(ROSTER_SQL)]
    result: dict[str, Diagnostics] = {
        employee_id: Diagnostics(
            employee_id=employee_id, verified_outcomes=int(outcomes.get(employee_id, 0))
        )
        for employee_id in roster
    }
    for employee_id, count in outcomes.items():
        if employee_id not in result:  # pragma: no cover - FK keeps owners on the roster
            result[employee_id] = Diagnostics(employee_id=employee_id, verified_outcomes=int(count))

    spans: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in _session_rows(connection, start_iso, end_iso):
        owner = str(row["owner_employee_id"])
        if owner not in result:  # pragma: no cover - FK keeps owners on the roster
            result[owner] = Diagnostics(employee_id=owner)
        began = _parse(str(row["started_at"]), default=start)
        # A row with no ended_at is still running as far as the ledger knows;
        # clamping it to the window end reports that without letting one
        # never-closed session dominate every future window.
        finished = end if row["ended_at"] is None else _parse(str(row["ended_at"]), default=end)
        spans.setdefault(owner, []).append((began, max(finished, began)))

    for employee_id, entries in spans.items():
        diagnostics = result[employee_id]
        diagnostics.session_count = len(entries)
        diagnostics.avg_active_sessions, diagnostics.peak_sessions = _concurrency(
            entries, start, end
        )

    merged: dict[str, int] = {}
    first_pass_hits: dict[str, int] = {}
    first_pass_known: dict[str, int] = {}
    first_pass_unknown: dict[str, int] = {}
    for row in connection.execute(
        "SELECT e.id AS id, e.meta_json AS meta_json, t.owner_employee_id AS owner_employee_id "
        "FROM task_evidence e JOIN board_tasks t ON t.id = e.task_id "
        "WHERE e.kind = 'pr' AND e.quality_gate = 'pass' "
        "AND t.owner_employee_id IS NOT NULL AND e.created_at >= ? AND e.created_at <= ? "
        "ORDER BY e.created_at ASC, e.rowid ASC",
        (start_iso, end_iso),
    ).fetchall():
        meta = _meta(row["meta_json"])
        if not _is_merged(meta):
            continue
        owner = str(row["owner_employee_id"])
        if owner not in result:  # pragma: no cover - FK keeps owners on the roster
            result[owner] = Diagnostics(employee_id=owner)
        merged[owner] = merged.get(owner, 0) + 1
        attempts = _gate_attempts(meta)
        if attempts is None:
            first_pass_unknown[owner] = first_pass_unknown.get(owner, 0) + 1
            continue
        first_pass_known[owner] = first_pass_known.get(owner, 0) + 1
        if attempts <= FIRST_PASS_ATTEMPTS:
            first_pass_hits[owner] = first_pass_hits.get(owner, 0) + 1

    for employee_id, diagnostics in result.items():
        diagnostics.merged_prs = merged.get(employee_id, 0)
        diagnostics.first_pass_known = first_pass_known.get(employee_id, 0)
        diagnostics.first_pass_unknown = first_pass_unknown.get(employee_id, 0)
        diagnostics.outcomes_per_session = _ratio(
            diagnostics.verified_outcomes, diagnostics.session_count
        )
        diagnostics.merged_prs_per_session = _ratio(
            diagnostics.merged_prs, diagnostics.session_count
        )
        diagnostics.first_pass_success = _ratio(
            first_pass_hits.get(employee_id, 0), diagnostics.first_pass_known
        )

    return result


__all__ = ["Diagnostics", "compute_diagnostics"]
