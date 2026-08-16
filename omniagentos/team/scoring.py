"""Verified-output scoring for the Team Work OS.

The whole point of this module is what it REFUSES to count. A productivity
number is only worth reporting if the cheapest way to raise it is to finish
verified work, so every quantity that can be manufactured without finishing
anything is absent here — not weighted low, ABSENT:

* **no commit or LOC term** — a commit is a keystroke, not an outcome;
* **no session/token/run term** — spawning a session costs nothing;
* **no PR count term** — an opened PR is a request, not a result;
* **no task-count term** — splitting one card into twelve is one card's work;
* **no status-transition term** — moving a card back and forth is free.

None of those numbers is even READ into a score. They live in
:mod:`omniagentos.team.diagnostics`, which is one-directional: scoring feeds
diagnostics, diagnostics never feeds scoring.

What DOES count is one thing: a **top-level card, owned by a person, done, and
verified inside the period**, worth its SIZE (S=1, M=3, L=8). Subtasks are worth
zero — their completion enables the parent, and the parent's size already prices
the whole job — so the split-farming move (one M card becomes twelve done
subtasks) moves the number by exactly nothing.

Every point is traceable. :class:`ScoreBreakdown` carries the card behind each
point and the evidence refs behind each card, plus an ``excluded`` list naming
every card and artifact that was refused AND WHY — a number nobody can audit is
a number nobody should act on, and a silent exclusion is indistinguishable from
a bug.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from omniagentos.collab.contracts import BASELINE_SOURCE as _BASELINE_SOURCE
from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.team.contracts import TASK_ADHOC_SOURCE

# Bump this ONLY with the rules. Every persisted breakdown embeds it, and
# ``omniagentos.team.report`` refuses to mix versions inside one day — a
# scoreboard whose rows were computed by two different rulesets is a comparison
# of two different questions.
SCORE_VERSION = "v1"

# Size -> points. The only conversion from work to number in the system.
POINTS_BY_SIZE: dict[str, int] = {"S": 1, "M": 3, "L": 8}

# The seeded pre-launch week (P7's BASE-* cards). Those cards are DONE and
# VERIFIED, but they are a RECORD OF A PRIOR PERIOD, not output of the period
# being scored — their verified_at is the import timestamp, which lands inside
# the first live window. Counting them would hand everyone a free 1.0x on day
# one, so the period score excludes them (visibly, in ``excluded``) and
# :func:`baseline_points` reads exactly them.
#
# The literal lives in ``collab.contracts`` because the collab store enforces
# baseline immutability and cannot import this package. One definition, two
# readers.
BASELINE_SOURCE = _BASELINE_SOURCE

# The destination every ratio is measured against. 10x is the stated goal, so
# "% to 10x" is the number the report leads with — and the scoreboard API serves
# the SAME arithmetic rather than a second copy of it (a dashboard and a report
# that round differently are two answers to one question).
TARGET_X = 10

# Evidence that records the work happened AND that it did not land. It can never
# add to a score, and a card whose ONLY evidence is of this kind cannot be
# counted at all.
NON_COUNTING_GATES: frozenset[str] = frozenset({"rejected", "reverted", "excessive_attempts"})

PASSING_GATE = "pass"

_DONE = BoardTaskStatus.DONE.value


class _HasConnection(Protocol):
    """Anything that exposes a live sqlite3 connection (TeamStore, SqliteStore)."""

    @property
    def _connection(self) -> sqlite3.Connection: ...


ScoreSource = sqlite3.Connection | _HasConnection


def connection_for(source: ScoreSource) -> sqlite3.Connection:
    """The connection to read from, whether handed a store or a raw connection.

    Resolved LIVE (never cached): ``SqliteStore`` hands out one connection per
    thread, and a handle captured elsewhere would read another thread's
    transaction.
    """
    if isinstance(source, sqlite3.Connection):
        return source
    connection = source._connection
    if not isinstance(connection, sqlite3.Connection):  # pragma: no cover - defensive
        raise TypeError(f"{type(source).__name__} does not expose a sqlite3 connection")
    return connection


def _normalize(value: str) -> str:
    """A timestamp in the repo's canonical ``...Z`` form, for string comparison.

    Bounds and stored stamps are compared as STRINGS (fast, index-friendly), and
    that is only sound if both sides spell UTC the same way: ``+00:00`` sorts
    BEFORE ``Z`` lexicographically, so an offset-spelled stamp would silently
    fall outside a window it belongs in. Anything unparseable is returned
    unchanged rather than guessed at.
    """
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def start_bound(value: str) -> str:
    """Inclusive lower bound. A bare ``YYYY-MM-DD`` means that day's first second."""
    text = str(value).strip()
    if len(text) == 10:
        return f"{text}T00:00:00Z"
    return _normalize(text)


def end_bound(value: str) -> str:
    """Inclusive upper bound. A bare ``YYYY-MM-DD`` means that day's last second."""
    text = str(value).strip()
    if len(text) == 10:
        return f"{text}T23:59:59Z"
    return _normalize(text)


def _within(value: str | None, start: str, end: str) -> bool:
    if value is None:
        return False
    stamp = _normalize(str(value))
    return start <= stamp <= end


@dataclass
class ScoreBreakdown:
    """One person's period score, plus the audit trail behind it.

    ``counted`` is every point that was awarded, with the card and the evidence
    refs that back it. ``excluded`` is every card or artifact that was refused,
    with a machine-readable reason — the two lists together are the answer to
    "why is this number what it is", which is the only form of a productivity
    number anyone should be asked to accept.
    """

    employee_id: str
    score: int = 0
    counted: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)

    def add(self, entry: dict[str, Any]) -> None:
        self.counted.append(entry)
        self.score += int(entry["points"])

    def exclude(self, reason: str, **identity: Any) -> None:
        self.excluded.append({**identity, "reason": reason})

    def as_dict(self) -> dict[str, Any]:
        """Serializable form. Carries ``score_version`` wherever it is persisted."""
        return {
            "score_version": SCORE_VERSION,
            "employee_id": self.employee_id,
            "score": self.score,
            "counted": [dict(entry) for entry in self.counted],
            "excluded": [dict(entry) for entry in self.excluded],
        }


#: The people scoring/reporting speaks for: ACTIVE roster members, plus anyone
#: — active or not — who still owns a live card. A departed employee is marked
#: ``status='inactive'`` and drops out of every view, UNLESS they left open
#: work behind: those cards must stay visible in someone's queue until they are
#: reassigned, because a card that no view can show is a card nobody finishes.
ROSTER_SQL = """
    SELECT id FROM employees WHERE status = 'active'
    UNION
    SELECT DISTINCT owner_employee_id FROM board_tasks
    WHERE owner_employee_id IS NOT NULL
      AND status NOT IN ('done', 'cancelled')
      AND archived_at IS NULL
    ORDER BY id ASC
"""


def _roster(connection: sqlite3.Connection) -> list[str]:
    return [str(row["id"]) for row in connection.execute(ROSTER_SQL)]


def employee_names(source: ScoreSource) -> dict[str, str]:
    """``employee_id -> display name`` for the whole roster."""
    connection = connection_for(source)
    return {
        str(row["id"]): str(row["name"])
        for row in connection.execute("SELECT id, name FROM employees ORDER BY id ASC")
    }


_TASK_COLUMNS = (
    "id, ref, title, size, status, owner_employee_id, verified_at, verified_by, "
    "acceptance_criteria, source, updated_at"
)


def _top_level_owned_tasks(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every parentless, human-owned card. Subtasks are not selected AT ALL.

    The ``parent_task_id IS NULL`` filter is the split-farming defense, and it
    lives in the QUERY rather than in a later branch on purpose: a subtask never
    enters the scoring path, so no future edit can accidentally give one points.

    Ad-hoc **Tasks** (``source='task-adhoc'``, the operator's v4 ruling 2026-08-13) are
    excluded HERE, at the card-gathering stage, for the same reason: a Task is
    worth ZERO points by definition, so it never scores, never appears in the
    refusal (``excluded``) listings, and never moves pace or floors — it does
    not even enter the scoring path. Migration 123 pins ``source NOT NULL``,
    so the ``<>`` comparison cannot silently drop a NULL-source card.
    """
    return list(
        connection.execute(
            f"SELECT {_TASK_COLUMNS} FROM board_tasks "
            "WHERE parent_task_id IS NULL AND owner_employee_id IS NOT NULL "
            "AND source <> ? "
            "ORDER BY verified_at ASC, id ASC",
            (TASK_ADHOC_SOURCE,),
        ).fetchall()
    )


def _evidence_by_task(connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """All attributed evidence, grouped by card, in one read (never per-card)."""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(
        "SELECT id, task_id, kind, ref, quality_gate FROM task_evidence "
        "WHERE task_id IS NOT NULL ORDER BY created_at ASC, rowid ASC"
    ):
        grouped.setdefault(str(row["task_id"]), []).append(row)
    return grouped


def task_points(size: Any) -> int:
    """Points for a card's size. An unknown size is worth NOTHING, never a default.

    Migration 123 CHECKs the vocabulary, so this branch is unreachable through
    the store; if it is ever reached, awarding a guessed 3 would be inventing
    output that nobody sized.
    """
    return POINTS_BY_SIZE.get(str(size or "").upper().strip(), 0)


def _grade(
    task: sqlite3.Row, evidence: list[sqlite3.Row]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Grade ONE done+verified top-level card. The single scoring decision.

    Both :func:`compute_scores` and :func:`baseline_points` come through here, so
    a baseline card is scored by exactly the rules a live card is — a baseline
    computed by a softer path would make ``production_x`` a comparison between
    two different measurements.

    Returns ``(counted_entry_or_None, exclusions)``. Evidence-level exclusions
    are returned even when the card COUNTS: a card that landed despite a
    reverted commit should show both facts.
    """
    exclusions: list[dict[str, Any]] = []
    task_id = str(task["id"])
    counting: list[sqlite3.Row] = []
    for row in evidence:
        gate = str(row["quality_gate"])
        if gate == PASSING_GATE:
            counting.append(row)
            continue
        if gate in NON_COUNTING_GATES:
            exclusions.append(
                {
                    "evidence_id": str(row["id"]),
                    "task_id": task_id,
                    "ref": str(row["ref"]),
                    "kind": str(row["kind"]),
                    "reason": f"evidence_{gate}",
                }
            )

    # Defense in depth. ``TeamStore.verify_task`` already polices which evidence
    # can carry a verification, but a stamp can also be applied by a human, and a
    # card whose whole evidence trail is rejected/reverted work is not output no
    # matter who stamped it. A card with NO evidence at all is a different case
    # (zero-commit work is real work) and stays countable on its verifier's word.
    if evidence and not counting:
        exclusions.append({"task_id": task_id, "ref": task["ref"], "reason": "no_passing_evidence"})
        return None, exclusions

    points = task_points(task["size"])
    if points == 0:
        exclusions.append({"task_id": task_id, "ref": task["ref"], "reason": "unknown_size"})
        return None, exclusions

    entry = {
        "task_id": task_id,
        "ref": task["ref"],
        "title": str(task["title"]),
        "size": str(task["size"]),
        "points": points,
        "verified_at": task["verified_at"],
        "verified_by": task["verified_by"],
        "evidence_refs": [str(row["ref"]) for row in counting],
    }
    return entry, exclusions


def compute_scores(
    source: ScoreSource, *, period_start: str, period_end: str
) -> dict[str, ScoreBreakdown]:
    """Every person's verified-output score for ``[period_start, period_end]``.

    Bounds accept ``YYYY-MM-DD`` (whole UTC day, inclusive at both ends) or a
    full ISO timestamp.

    Every employee on the roster gets a breakdown, including one who produced
    nothing: an absent person and a zero-point person are different answers, and
    only one of them should make somebody disappear from the report.
    """
    connection = connection_for(source)
    start = start_bound(period_start)
    end = end_bound(period_end)

    breakdowns: dict[str, ScoreBreakdown] = {
        employee_id: ScoreBreakdown(employee_id=employee_id) for employee_id in _roster(connection)
    }
    evidence = _evidence_by_task(connection)

    for task in _top_level_owned_tasks(connection):
        owner = str(task["owner_employee_id"])
        breakdown = breakdowns.get(owner)
        if breakdown is None:
            # Migration 123's foreign key makes an off-roster owner unpersistable;
            # if one ever exists, report it rather than dropping its work.
            breakdown = ScoreBreakdown(employee_id=owner)
            breakdowns[owner] = breakdown

        if str(task["status"]) != _DONE:
            continue

        if task["verified_at"] is None:
            # Done is a claim; verified is the claim standing up. The exclusion is
            # scoped to cards that MOVED in this period so the list stays the
            # period's story rather than the whole board's history.
            if _within(task["updated_at"], start, end):
                breakdown.exclude("done_not_verified", task_id=str(task["id"]), ref=task["ref"])
            continue

        if not _within(task["verified_at"], start, end):
            continue

        if str(task["source"] or "") == BASELINE_SOURCE:
            breakdown.exclude("baseline_period", task_id=str(task["id"]), ref=task["ref"])
            continue

        entry, exclusions = _grade(task, evidence.get(str(task["id"]), []))
        for item in exclusions:
            breakdown.excluded.append(item)
        if entry is not None:
            breakdown.add(entry)

    return breakdowns


def baseline_points(source: ScoreSource, employee_id: str) -> int:
    """The person's pre-launch baseline, scored by the SAME rules as a live week.

    Reads the seeded ``BASE-*`` cards (``source='baseline-2026-08-03'``, done and
    verified) and sums their sizes through :func:`_grade`. No period filter: the
    baseline card IS the period.

    Returns 0 when the person has no baseline card — which
    :func:`production_x` reads as "unmeasurable", never as "infinitely
    productive".
    """
    connection = connection_for(source)
    evidence = _evidence_by_task(connection)
    total = 0
    for task in connection.execute(
        f"SELECT {_TASK_COLUMNS} FROM board_tasks "
        "WHERE parent_task_id IS NULL AND owner_employee_id = ? AND status = ? "
        "AND verified_at IS NOT NULL AND source = ? ORDER BY id ASC",
        (employee_id, _DONE, BASELINE_SOURCE),
    ).fetchall():
        entry, _ = _grade(task, evidence.get(str(task["id"]), []))
        if entry is not None:
            total += int(entry["points"])
    return total


def production_x(person_points: int | float, baseline: int | float | None) -> float | None:
    """``points / baseline``, or None when there is no baseline to divide by.

    None is the honest answer for a person with no measured baseline week. A
    fabricated 1.0x (or a divide-by-zero crash at 07:00) would both be worse:
    one lies, the other loses the whole report.
    """
    if baseline is None or float(baseline) <= 0:
        return None
    return float(person_points) / float(baseline)


def pct_to_target(ratio: float | None) -> int | None:
    """``ratio`` as a whole-percent share of :data:`TARGET_X`, or None.

    The ONE implementation of "% to 10x". The 07:00 report and
    ``GET /api/team/scoreboard`` both call it, so the strip on the dashboard and
    the line in Slack can never round the same week differently. ``None`` in
    (no baseline) stays ``None`` out — never a fabricated 0%.
    """
    return None if ratio is None else round(ratio / TARGET_X * 100)


# ---------------------------------------------------------------------------
# fleet production + the OVERALL blend
# ---------------------------------------------------------------------------

#: The baseline week every 1x in this system refers to (Mon–Sun, inclusive
#: days). Humans carry it as verified BASE-* cards; the fleet's is counted from
#: the same ledger the live number comes from, over these bounds.
BASELINE_WEEK_START = "2026-08-03T00:00:00Z"
BASELINE_WEEK_END = "2026-08-10T00:00:00Z"

#: OVERALL = a declared 50/50 blend of the two ratios. Weights are policy, not
#: arithmetic — they live here so the report can print them and a change is a
#: reviewed code change, never a silent retune.
OVERALL_WEIGHTS = {"humans": 0.5, "fleet": 0.5}


def fleet_production(ledger_path: str, period_start: str, period_end: str) -> int | None:
    """Gate-passed landings (``merged`` events) in the loopqueue ledger, in-window.

    The fleet's verified outcome is a candidate that PASSED the merge gate and
    landed on main — exactly the evidence bar humans meet with verified cards.
    Unique by artifact id, so a re-emitted event cannot double-count. ``None``
    (never 0) when the ledger is absent/unreadable: "the factory produced
    nothing" and "I cannot see the factory" are different answers.

    Bounds may be full ``...T..Z`` timestamps or bare ``YYYY-MM-DD`` days — a
    bare start day means from its midnight (inclusive), a bare end day means
    through its whole day (exclusive at the NEXT midnight), matching the
    report's whole-day window resolution.
    """
    if "T" not in period_start:
        period_start = f"{period_start}T00:00:00Z"
    if "T" not in period_end:
        next_day = date.fromisoformat(period_end) + timedelta(days=1)
        period_end = f"{next_day.isoformat()}T00:00:00Z"
    try:
        handle = open(ledger_path, encoding="utf-8")
    except OSError:
        return None
    merged: set[str] = set()
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("event") != "merged":
                continue
            ts = str(row.get("ts") or "")
            if not ts or not (period_start <= ts < period_end):
                continue
            merged.add(str(row.get("id") or ts))
    return len(merged)


def overall_production_x(humans_x: float | None, fleet_x: float | None) -> float | None:
    """The one company number: the :data:`OVERALL_WEIGHTS` blend of both ratios.

    A missing component (no baseline, unreadable ledger) renormalizes onto the
    other rather than pretending it was 0x — and when neither is measurable the
    answer is None, never an invented figure.
    """
    parts = [
        (OVERALL_WEIGHTS["humans"], humans_x),
        (OVERALL_WEIGHTS["fleet"], fleet_x),
    ]
    known = [(weight, ratio) for weight, ratio in parts if ratio is not None]
    if not known:
        return None
    total_weight = sum(weight for weight, _ratio in known)
    return sum(weight * ratio for weight, ratio in known) / total_weight


__all__ = [
    "BASELINE_SOURCE",
    "BASELINE_WEEK_END",
    "BASELINE_WEEK_START",
    "NON_COUNTING_GATES",
    "OVERALL_WEIGHTS",
    "POINTS_BY_SIZE",
    "ROSTER_SQL",
    "SCORE_VERSION",
    "TARGET_X",
    "ScoreBreakdown",
    "baseline_points",
    "compute_scores",
    "connection_for",
    "employee_names",
    "end_bound",
    "fleet_production",
    "overall_production_x",
    "pct_to_target",
    "production_x",
    "start_bound",
    "task_points",
]
