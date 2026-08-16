"""The ONE canonical daily production report (07:00), and its Slack post.

There is exactly one production report in the estate and this is it. It
supersedes the 07:45 ``com.omni.daily-dev-scoreboard`` job (which scored commits
and PR counts — the two numbers that are cheapest to manufacture) and it takes
over the operational content the 07:30 briefing used to carry. Two daily reports
disagreeing about how the week is going is worse than either one alone.

Three properties this file exists to hold:

* **No LLM anywhere.** :func:`render` is a template fill. Every number it prints
  was computed in :func:`gather` and is present in the gathered dict — which
  ``tests/team_scoring/test_report_render.py`` asserts mechanically, token by
  token. A report that can invent a figure is a report nobody can act on.
* **The file is written BEFORE the post.** Slack is the delivery channel, not
  the record. A token failure at 07:00 must never mean the day has no report;
  it means the day has a report that did not get delivered, and an exit code
  that says so.
* **One score version per day.** If a snapshot for this day was computed by a
  different ruleset, this refuses to write or post (exit 2) rather than mix two
  answers to one question inside one table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.team import commitments as team_commitments
from omniagentos.team.contracts import (
    AUTOMATION_SLOTS_PER_DAY,
    NORTH_STAR,
    OPEN_COMMITMENT_STATUSES,
    READY_QUEUE_FLOOR,
)
from omniagentos.team.diagnostics import Diagnostics, compute_diagnostics
from omniagentos.team.scoring import (
    BASELINE_WEEK_END,
    BASELINE_WEEK_START,
    OVERALL_WEIGHTS,
    SCORE_VERSION,
    TARGET_X,
    ScoreBreakdown,
    baseline_points,
    compute_scores,
    connection_for,
    employee_names,
    fleet_production,
    overall_production_x,
    pct_to_target,
    production_x,
)
from omniagentos.team.store import TeamStore

# The rolling window the headline number is computed over.
WINDOW_DAYS = 7

# The destination the report measures against (``scoring.TARGET_X``, re-exported
# for readers of this module): 10x is the stated goal, so the report prints
# distance to it rather than an unanchored ratio. The arithmetic itself lives in
# ``scoring.pct_to_target`` because the scoreboard API serves the same number.

# Bottleneck rule parameters.
BLOCKED_FLOOR = 2  # >= this many blocked cards is the bottleneck
REVIEW_LATENCY_HOURS = 24  # a review item older than this is the bottleneck
NO_OUTPUT_WINDOW_HOURS = 48  # no verified output in this window is the bottleneck
READY_FLOOR = READY_QUEUE_FLOOR  # fewer ready cards than this is starvation

# The one place a bottleneck class turns into an instruction. Static text, never
# generated: an LLM-written "recommendation" in a 07:00 report is an unreviewed
# instruction to three people.
RECOMMENDATIONS: dict[str, str] = {
    "blocked": "unblock first — name the owner of each blocker and get a decision today",
    "review_latency": "review the oldest card before starting anything new",
    "queue_starvation": f"groom this queue back above {READY_FLOOR} ready cards before standup",
    "no_verified_output": "verify what is already done, or split the in-flight card into "
    "something finishable today",
    "none": "keep going",
}

# Severity, worst first. Used only to break a tie when two bottleneck classes are
# equally common across the team.
SEVERITY: tuple[str, ...] = (
    "blocked",
    "review_latency",
    "no_verified_output",
    "queue_starvation",
    "none",
)

SLACK_ENV_FILE = os.path.expanduser("~/.config/omni/connections.env")
SLACK_URL = "https://slack.com/api/chat.postMessage"

#: Where the AI fleet's landings are recorded (loopqueue ledger, append-only).
#: Env-overridable because the ledger lives beside the SERVING checkout's var,
#: not under runtime, and tests point it at a fixture.
FLEET_LEDGER_ENV = "OMNI_TEAM_FLEET_LEDGER"


def _default_fleet_ledger() -> str:
    # env_keys=() + environ={} on purpose: this default deliberately anchors the
    # SERVING checkout's var (see the docstring above) — never runtime, and
    # never a simulation root — and it resolves LAZILY so importing this module
    # can never raise under OMNIAGENTOS_SIM_MODE (review B2, 2026-08-14).
    # OMNI_TEAM_FLEET_LEDGER remains the only override, in fleet_ledger_path().
    return str(resolve_var_root(env_keys=(), environ={}, leaf=("loopqueue", "ledger.jsonl")))


def fleet_ledger_path() -> str:
    return os.environ.get(FLEET_LEDGER_ENV) or _default_fleet_ledger()


DEFAULT_CHANNEL = "C0000EXAMPLE"  # #dev-agentic-alerts
CHANNEL_ENV = "OMNI_TEAM_REPORT_CHANNEL"

_AWAITING = BoardTaskStatus.AWAITING_APPROVAL.value


# ---------------------------------------------------------------------------
# gather
# ---------------------------------------------------------------------------


def _day_bounds(day: str) -> tuple[str, str]:
    """The 7-day rolling window ENDING on ``day`` (inclusive at both ends)."""
    end = date.fromisoformat(day)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _recent_bounds(day: str) -> tuple[str, str]:
    """The ``NO_OUTPUT_WINDOW_HOURS`` window ending at the end of ``day``.

    Whole-day resolution: both bounds are inclusive days, so 48h is
    ``[day-1 00:00:00, day 23:59:59]`` — exactly two days.
    """
    end = date.fromisoformat(day)
    start = end - timedelta(hours=NO_OUTPUT_WINDOW_HOURS) + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _fmt_x(value: float | None) -> str:
    return "no baseline" if value is None else f"{value:.1f}x"


def _headline(ratio: float | None, points: int) -> str:
    """The ``1.4x (14% to 10x)`` fragment — or an honest ``no baseline``.

    A person with no measured baseline week gets their raw points and the words
    "no baseline", never a ratio invented from a zero denominator.
    """
    if ratio is None:
        return f"no baseline ({points} pts)"
    return f"{_fmt_x(ratio)} ({_pct_to_target(ratio)}% to {TARGET_X}x)"


def _fmt_rate(value: float | None) -> str:
    """A percentage, or ``n/a`` when the rate had no samples. Never a fake 0%."""
    return "n/a" if value is None else f"{round(value * 100)}%"


def _fmt_float(value: float | None) -> str:
    """One decimal place — except that a small positive value never prints ``0.0``.

    Mean concurrency over a 7-day window is a small number by construction (one
    three-hour session is 3/168). Rounding that to ``0.0`` beside "verified
    outcomes: 1" reads as an instrument failure, so a real-but-small value says
    ``<0.1`` and only a genuine zero prints ``0.0``.
    """
    if value is None:
        return "n/a"
    if 0 < value < 0.05:
        return "<0.1"
    return f"{value:.1f}"


#: Local alias of the shared helper (see ``scoring.pct_to_target``).
_pct_to_target = pct_to_target


def _major_contribution(breakdown: ScoreBreakdown) -> dict[str, Any] | None:
    """The biggest counted card: most points, latest verified on a tie."""
    if not breakdown.counted:
        return None
    best = max(
        breakdown.counted,
        key=lambda entry: (int(entry["points"]), str(entry.get("verified_at") or "")),
    )
    return {
        "task_id": best["task_id"],
        "ref": best.get("ref"),
        "title": best["title"],
        "points": best["points"],
    }


def _label(ref: Any, title: str) -> str:
    return f"{ref} — {title}" if ref else title


def _review_ages(source: Any, day: str) -> dict[str, list[dict[str, Any]]]:
    """Awaiting-approval cards per owner with their age in hours, oldest first.

    Read here rather than off ``team_queues`` because a queue card deliberately
    carries no timestamp — and "how long has this been waiting" is the entire
    review-latency rule.
    """
    connection = connection_for(source)
    now = datetime.fromisoformat(f"{day}T23:59:59+00:00")
    ages: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT id, ref, title, owner_employee_id, updated_at FROM board_tasks "
        "WHERE status = ? AND owner_employee_id IS NOT NULL AND archived_at IS NULL "
        "ORDER BY updated_at ASC, id ASC",
        (_AWAITING,),
    ).fetchall():
        try:
            updated = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover - defensive against a hand-edited row
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        hours = (now - updated).total_seconds() / 3600
        ages.setdefault(str(row["owner_employee_id"]), []).append(
            {
                "task_id": str(row["id"]),
                "ref": row["ref"],
                "title": str(row["title"]),
                "age_hours": round(hours, 1),
            }
        )
    return ages


def _bottleneck(
    *,
    queue: dict[str, int],
    blocked_refs: list[str],
    reviews: list[dict[str, Any]],
    recent_outcomes: int,
) -> dict[str, Any]:
    """The FIRST matching rule wins. Order is the priority, and it is fixed.

    A person with two blockers and a starved queue has ONE problem worth naming
    at 07:00 — the blockers. A report that lists every true statement is a report
    nobody reads to the end.
    """
    if len(blocked_refs) >= BLOCKED_FLOOR:
        return {"class": "blocked", "text": f"blocked: {', '.join(blocked_refs)}"}
    stale = [item for item in reviews if float(item["age_hours"]) > REVIEW_LATENCY_HOURS]
    if stale:
        oldest = stale[0]
        return {
            "class": "review_latency",
            "text": f"review latency: {oldest['ref'] or oldest['task_id']}",
        }
    if queue["ready"] < READY_FLOOR:
        return {
            "class": "queue_starvation",
            "text": f"queue starvation ({queue['ready']} ready)",
        }
    if recent_outcomes == 0:
        return {
            "class": "no_verified_output",
            "text": f"no verified output {NO_OUTPUT_WINDOW_HOURS}h",
        }
    return {"class": "none", "text": "none"}


def _team_bottleneck(people: list[dict[str, Any]]) -> dict[str, Any]:
    """The most common person-bottleneck class; severity breaks a tie."""
    counts: dict[str, int] = {}
    texts: dict[str, str] = {}
    for person in people:
        name = str(person["bottleneck"]["class"])
        if name == "none":
            continue
        counts[name] = counts.get(name, 0) + 1
        texts.setdefault(name, str(person["bottleneck"]["text"]))
    if not counts:
        return {"class": "none", "text": "none", "people": 0}
    worst = min(counts, key=lambda name: (-counts[name], SEVERITY.index(name)))
    return {"class": worst, "text": texts[worst], "people": counts[worst]}


def _person_display(person: dict[str, Any]) -> dict[str, str]:
    """Every string :func:`render` will print. Formatting happens HERE, once."""
    diagnostics = person["diagnostics"]
    queue = person["queue"]
    major = person["major_contribution"]
    commitments = person["commitments"]
    return {
        "rank": str(person["rank"]),
        "name": str(person["name"]).upper(),
        "headline": _headline(person["production_x"], int(person["points"])),
        "major_contribution": "none" if major is None else _label(major["ref"], major["title"]),
        "verified_outcomes": str(diagnostics["verified_outcomes"]),
        "avg_sessions": _fmt_float(diagnostics["avg_active_sessions"]),
        "first_pass": _fmt_rate(diagnostics["first_pass_success"]),
        "active": str(queue["active"]),
        "ready": str(queue["ready"]),
        "blocked": str(queue["blocked"]),
        "review": str(queue["review"]),
        "done_today": str(queue["done_today"]),
        "merged_prs": str(diagnostics["merged_prs"]),
        "sessions": str(diagnostics["session_count"]),
        "bottleneck": str(person["bottleneck"]["text"]),
        "recommendation": str(person["recommendation"]),
        "commitments_line": str(commitments["line"]),
        "commitments_missed": ", ".join(str(ref) for ref in commitments["missed"]),
    }


# ---------------------------------------------------------------------------
# daily commitments (M3): resolved and rendered as "Yesterday: ..." per person
# ---------------------------------------------------------------------------


def _targets_line(prefix: str = "YOUR TARGETS") -> str:
    """The one north-star line every dev-facing surface renders (2026-08-14).

    Byte-identical to ``notify._targets_line`` (a report -> notify import
    would cycle, since ``notify`` already imports from this module, so the
    three-line function is duplicated rather than shared). ``NORTH_STAR``
    already carries its own leading 🎯; this strips it before re-prefixing so
    the glyph never doubles.
    """
    return f"🎯 {prefix}: {NORTH_STAR.removeprefix('🎯').strip()}"


def _commitments_yesterday(day: str) -> str:
    """The LOCAL day the report's "Yesterday: ..." commitments line covers —
    the day before the one the report is FOR."""
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _resolve_commitments(store: TeamStore, day: str) -> bool:
    """Resolve yesterday's commitments before reading them. Returns success.

    Fail-safe but never silently favourable (WP-B contract): an exception
    here must never abort the 07:00 report — ``gather`` still returns a full
    report — but it must not read as "no commitments recorded" either, so the
    failure is surfaced through the return value and every person's line
    renders the explicit "commitments unresolved" state instead.
    ``resolve_day`` is itself idempotent (WP-A), so a report re-run after the
    06:55 job already resolved this day changes nothing — this call is just
    the report's own guarantee that it never reads a stale unresolved row.
    """
    try:
        team_commitments.resolve_day(store, day)
    except Exception as exc:  # defensive: a commitments outage must not block the report
        print(f"team-report: commitments resolution failed for {day}: {exc}", file=sys.stderr)
        return False
    return True


def _commitment_ref(store: TeamStore, task_id: Any) -> str | None:
    """Best-effort card REF for a task commitment's linked card. Never raises."""
    if not task_id:
        return None
    try:
        row = (
            connection_for(store)
            .execute("SELECT ref FROM board_tasks WHERE id = ?", (str(task_id),))
            .fetchone()
        )
    except Exception:
        return None
    return None if row is None else row["ref"]


def _person_commitments(
    store: TeamStore, employee_id: str, day: str, *, resolved: bool
) -> dict[str, Any]:
    """The three-state "Yesterday: ..." commitments summary for one person.

    THREE distinguishable states (WP-B brief, never silently favourable):
    real numbers, an explicit "no commitments recorded" (genuinely zero rows
    — nothing was ever generated for this person that day), and an explicit
    "commitments unresolved" (``resolve_day`` raised — an instrument outage,
    never allowed to read as an honest zero).

    ``carried``/leftover ``committed`` rows are NOT delivered, and they are not
    MISSED either — they are OPEN. On the scheduled 07:00 path resolution has
    already run, so no open row survives and this distinction costs nothing;
    but ``gather`` is also called as a pure read (the preview route), and there
    a day still in progress is full of open rows. Bucketing those as "missed"
    told a reader that work had FAILED when nobody had judged it yet — an
    accusation invented by the renderer. So there are three buckets: delivered
    (counted), missed (named — only ``status == 'missed'``), and pending
    (counted, and only mentioned when there are any). The pending clause is
    APPENDED, leaving the delivered/improvement phrase byte-identical to what
    the 07:00 report has always emitted.

    Automation slots (the operator's ruling 2026-08-14) get their OWN clause,
    ``· automations N/AUTOMATION_SLOTS_PER_DAY``, appended the same way — N
    is the delivered count. A day with no automation rows at all (a
    pre-migration day, or nothing was ever generated) OMITS the clause
    entirely rather than printing a ``0/3`` that would read as a judged
    miss on a day nobody measured. A day where the slots exist but are
    still OPEN (the same pure-read-of-an-in-progress-day case the pending
    bucket above exists for) reads ``· automations pending`` — never a
    ratio for slots nobody has judged yet.
    """
    if not resolved:
        return {"line": "Yesterday: commitments unresolved ⚠", "missed": []}
    rows = store.list_commitments(day=day, employee_id=employee_id)
    if not rows:
        return {"line": "Yesterday: no commitments recorded", "missed": []}
    tasks = [row for row in rows if str(row["kind"]) == "task"]
    improvement = next((row for row in rows if str(row["kind"]) == "improvement"), None)
    automations = [row for row in rows if str(row["kind"]) == "automation"]
    delivered = sum(1 for row in tasks if str(row["status"]) == "delivered")
    total = len(tasks)
    pending = sum(1 for row in tasks if str(row["status"]) in OPEN_COMMITMENT_STATUSES)
    improvement_status = None if improvement is None else str(improvement["status"])
    # An improvement slot nobody has judged yet is not a failed one. ✗ is a
    # verdict; on a pure read of a day in progress there is no verdict to give.
    if improvement_status in OPEN_COMMITMENT_STATUSES:
        improvement_glyph = "pending"
    else:
        improvement_glyph = "✓" if improvement_status == "delivered" else "✗"
    automations_clause = ""
    if automations:
        if any(str(row["status"]) in OPEN_COMMITMENT_STATUSES for row in automations):
            automations_clause = " · automations pending"
        else:
            automations_delivered = sum(
                1 for row in automations if str(row["status"]) == "delivered"
            )
            automations_clause = (
                f" · automations {automations_delivered}/{AUTOMATION_SLOTS_PER_DAY}"
            )
    line = (
        f"Yesterday: delivered {delivered}/{total} commitments · improvement {improvement_glyph}"
        f"{automations_clause}"
    )
    if pending:
        line += f" · {pending} pending"
    missed = [
        str(_commitment_ref(store, row["task_id"]) or row["task_id"] or row["title"])
        for row in tasks
        if str(row["status"]) == "missed"
    ]
    return {"line": line, "missed": missed}


def gather(store: TeamStore, day: str, *, resolve: bool = False) -> dict[str, Any]:
    """Everything the report says, computed once, as plain data.

    The split between this and :func:`render` is load-bearing: gather does all
    the arithmetic (including the rounded DISPLAY strings), render does none. A
    number that appears in the text and not in this dict would be a number the
    report invented.

    ``resolve`` defaults to FALSE and gathering is therefore a pure READ. Only
    the scheduled 07:00 run passes ``resolve=True``. Resolving is a WRITE — it
    freezes commitment rows to delivered/missed and mints carries — and it used
    to happen on every call, which meant ``GET /api/team/report/preview``
    mutated the record for whatever day it was handed. A preview that changes
    the thing it previews is not a preview, and on an arbitrary ``?day=`` it is
    a way to resolve a day that is not over yet (Sol review, item 1).

    A pure read still renders REAL numbers, not the "unresolved ⚠" state: the
    rows are readable, they are simply as the last resolution left them. The
    warning state means "resolution was attempted and FAILED", which is a
    different fact and must stay distinguishable from both a clean read and an
    empty one.
    """
    window_start, window_end = _day_bounds(day)
    recent_start, recent_end = _recent_bounds(day)

    scores = compute_scores(store, period_start=window_start, period_end=window_end)
    recent = compute_scores(store, period_start=recent_start, period_end=recent_end)
    diagnostics = compute_diagnostics(
        store,
        period_start=window_start,
        period_end=window_end,
        verified_outcomes={
            employee_id: len(breakdown.counted) for employee_id, breakdown in scores.items()
        },
    )
    queues = store.team_queues(today=day)
    names = employee_names(store)
    reviews = _review_ages(store, day)
    commitments_day = _commitments_yesterday(day)
    commitments_resolved = _resolve_commitments(store, commitments_day) if resolve else True

    people: list[dict[str, Any]] = []
    for employee_id, breakdown in scores.items():
        baseline = baseline_points(store, employee_id)
        ratio = production_x(breakdown.score, baseline)
        bucket = queues.get(employee_id)
        counts = (
            bucket.counts
            if bucket is not None
            else {"ready": 0, "active": 0, "blocked": 0, "review": 0, "done_today": 0}
        )
        # SORTED, not queue order. The queue orders by created_at with a uuid
        # tiebreak, and two cards blocked in the same second would otherwise
        # render in a different order on every run — a report whose text moves
        # without its facts moving is a report nobody can diff.
        blocked_refs = (
            sorted(str(card.ref or card.id) for card in bucket.blocked)
            if bucket is not None
            else []
        )
        person_diagnostics: Diagnostics = diagnostics.get(
            employee_id, Diagnostics(employee_id=employee_id)
        )
        bottleneck = _bottleneck(
            queue=counts,
            blocked_refs=blocked_refs,
            reviews=reviews.get(employee_id, []),
            recent_outcomes=len(recent[employee_id].counted) if employee_id in recent else 0,
        )
        person: dict[str, Any] = {
            "employee_id": employee_id,
            "name": names.get(employee_id, employee_id),
            "rank": 0,
            "points": breakdown.score,
            "baseline_points": baseline,
            "production_x": ratio,
            "pct_to_10x": _pct_to_target(ratio),
            "counted": breakdown.counted,
            "excluded": breakdown.excluded,
            "major_contribution": _major_contribution(breakdown),
            "diagnostics": person_diagnostics.as_dict(),
            "queue": counts,
            "recent_verified_outcomes": (
                len(recent[employee_id].counted) if employee_id in recent else 0
            ),
            "bottleneck": bottleneck,
            "recommendation": RECOMMENDATIONS[str(bottleneck["class"])],
            "breakdown": breakdown.as_dict(),
            "commitments": _person_commitments(
                store, employee_id, commitments_day, resolved=commitments_resolved
            ),
        }
        people.append(person)

    people.sort(key=lambda entry: (-int(entry["points"]), str(entry["employee_id"])))
    for rank, person in enumerate(people, start=1):
        person["rank"] = rank
        person["display"] = _person_display(person)

    team_points = sum(int(person["points"]) for person in people)
    team_baseline = sum(
        int(person["baseline_points"]) for person in people if person["baseline_points"] is not None
    )
    team_ratio = production_x(team_points, team_baseline)
    team_bottleneck = _team_bottleneck(people)

    ledger = fleet_ledger_path()
    fleet_merged = fleet_production(ledger, window_start, window_end)
    fleet_baseline = fleet_production(ledger, BASELINE_WEEK_START, BASELINE_WEEK_END)
    fleet_ratio = production_x(fleet_merged, fleet_baseline) if fleet_merged is not None else None
    overall_ratio = overall_production_x(team_ratio, fleet_ratio)
    overall: dict[str, Any] = {
        "humans_x": team_ratio,
        "fleet_merged": fleet_merged,
        "fleet_baseline_merged": fleet_baseline,
        "fleet_x": fleet_ratio,
        "weights": dict(OVERALL_WEIGHTS),
        "production_x": overall_ratio,
        "pct_to_10x": pct_to_target(overall_ratio),
        "display": {
            "headline": _headline(overall_ratio, team_points),
            "humans": _fmt_x(team_ratio),
            "fleet": _fmt_x(fleet_ratio),
            "fleet_merged": "unreadable" if fleet_merged is None else str(fleet_merged),
        },
    }
    team: dict[str, Any] = {
        "points": team_points,
        "baseline_points": team_baseline,
        "production_x": team_ratio,
        "pct_to_10x": _pct_to_target(team_ratio),
        "bottleneck": team_bottleneck,
        "recommendation": RECOMMENDATIONS[str(team_bottleneck["class"])],
        "display": {
            "headline": _headline(team_ratio, team_points),
            "bottleneck": str(team_bottleneck["text"]),
            "recommendation": RECOMMENDATIONS[str(team_bottleneck["class"])],
        },
    }

    return {
        "day": day,
        "generated_at": utc_now_iso(),
        "score_version": SCORE_VERSION,
        "window": {"start": window_start, "end": window_end, "days": WINDOW_DAYS},
        "recent_window": {
            "start": recent_start,
            "end": recent_end,
            "hours": NO_OUTPUT_WINDOW_HOURS,
        },
        # The report's own parameters, carried in the data so that EVERY number
        # in the rendered text traces back to this dict — including the ones
        # that come from the template ("10x", "#1", "48h").
        "parameters": {
            "target_x": TARGET_X,
            "bottleneck_rank": 1,
            "ready_floor": READY_FLOOR,
            "blocked_floor": BLOCKED_FLOOR,
            "review_latency_hours": REVIEW_LATENCY_HOURS,
            "no_output_window_hours": NO_OUTPUT_WINDOW_HOURS,
        },
        "people": people,
        "team": team,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def render(gathered: dict[str, Any]) -> str:
    """The report text. Deterministic, template-only, no arithmetic, no LLM."""
    rank_marker = gathered["parameters"]["bottleneck_rank"]
    lines = [f"DAILY PRODUCTION — {gathered['day']}", _targets_line()]
    for person in gathered["people"]:
        display = person["display"]
        lines.append(f"{display['rank']}. {display['name']} — {display['headline']}")
        lines.append(f"   major contribution: {display['major_contribution']}")
        lines.append(
            f"   verified outcomes: {display['verified_outcomes']} · "
            f"avg sessions: {display['avg_sessions']} · "
            f"first-pass: {display['first_pass']}"
        )
        lines.append(
            f"   queue: {display['active']} active / {display['ready']} ready / "
            f"{display['blocked']} blocked / {display['review']} in review"
        )
        lines.append(f"   {display['commitments_line']}")
        if display["commitments_missed"]:
            lines.append(f"   missed: {display['commitments_missed']}")
        lines.append(
            f"   #{rank_marker} bottleneck: {display['bottleneck']} / "
            f"recommended: {display['recommendation']}"
        )
    team = gathered["team"]["display"]
    lines.append("")
    lines.append(
        f"TEAM — {team['headline']} · "
        f"#{rank_marker} bottleneck: {team['bottleneck']} · "
        f"recommended action: {team['recommendation']}"
    )
    overall = gathered["overall"]["display"]
    lines.append(
        f"OVERALL — {overall['headline']} · humans {overall['humans']} · "
        f"fleet {overall['fleet']} ({overall['fleet_merged']} landed)"
    )
    return "\n".join(lines)


_RANK_MEDALS = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}  # 🥇 🥈 🥉


def _progress_bar(pct: int | None, *, slots: int = 10) -> str:
    """``▓▓▓░░░░░░░`` — one slot per 10% toward the 10x target. Template only:
    the pct is gathered data, this just paints it."""
    if pct is None:
        return "░" * slots
    filled = max(0, min(slots, int(pct) // slots))
    return "▓" * filled + "░" * (slots - filled)


def _slack_person_visible(person: dict[str, Any]) -> bool:
    """Roster members with no work at all (no cards, no points, no baseline)
    are omitted from the Slack rendering — an all-zero row for someone who is
    not using the system yet is noise, not signal. They stay in the plain-text
    file and the snapshots, so nothing is hidden from the record."""
    queue = person["queue"]
    if any(int(queue[key]) for key in ("active", "ready", "blocked", "review", "done_today")):
        return True
    if int(person["points"]) > 0:
        return True
    return person["baseline_points"] not in (None, 0)


def render_slack(gathered: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The Slack presentation: (fallback_text, Block Kit blocks).

    Same contract as :func:`render` — template only, no arithmetic, every
    number read from ``gathered``'s display strings. The plain :func:`render`
    text remains the durable file format; this is the delivery skin.
    """
    rank_marker = gathered["parameters"]["bottleneck_rank"]
    target_x = gathered["parameters"]["target_x"]
    overall = gathered["overall"]
    overall_display = overall["display"]
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"\U0001f4ca Daily Production — {gathered['day']}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{_targets_line()}*"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"\U0001f3af *OVERALL — {overall_display['headline']}*\n"
                    f"`{_progress_bar(overall['pct_to_10x'])}` toward {target_x}x\n"
                    f"\U0001f465 humans {overall_display['humans']} · "
                    f"\U0001f916 fleet {overall_display['fleet']} "
                    f"({overall_display['fleet_merged']} landed this week)"
                ),
            },
        },
        {"type": "divider"},
    ]
    for person in gathered["people"]:
        if not _slack_person_visible(person):
            continue
        display = person["display"]
        medal = _RANK_MEDALS.get(int(person["rank"]), "▫️")
        lines = [
            f"{medal} *{display['name']}* — *{display['headline']}*",
            (
                f"✅ {display['verified_outcomes']} verified · "
                f"\U0001f500 {display['merged_prs']} PRs merged · "
                f"\U0001f916 {display['sessions']} sessions "
                f"(avg {display['avg_sessions']}) · "
                f"\U0001f3af first-pass {display['first_pass']}"
            ),
            (
                f"\U0001f4cb {display['active']} active · {display['ready']} ready · "
                f"{display['blocked']} blocked · {display['review']} review · "
                f"{display['done_today']} done today"
            ),
            f"\U0001f3c6 top: {display['major_contribution']}",
            (
                f"\U0001f4c5 {display['commitments_line']}"
                + (
                    f"\n   missed: {display['commitments_missed']}"
                    if display["commitments_missed"]
                    else ""
                )
            ),
            (
                f"⛔ #{rank_marker} bottleneck: {display['bottleneck']}\n"
                f"\U0001f4a1 {display['recommendation']}"
            ),
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    team = gathered["team"]["display"]
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"\U0001f3ed *TEAM* — *{team['headline']}*\n"
                    f"⛔ #{rank_marker} bottleneck: {team['bottleneck']}\n"
                    f"\U0001f4a1 {team['recommendation']}"
                ),
            },
        }
    )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"score {gathered['score_version']} · target {target_x}x · "
                        "every number traces to evidence"
                    ),
                }
            ],
        }
    )
    return render(gathered), blocks


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------


def load_slack_env(path: str = SLACK_ENV_FILE) -> None:
    """Read ``connections.env`` into the environment (launchd has no shell profile).

    ``setdefault``: an explicitly exported token always wins over the file.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post(
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
    channel: str | None = None,
    timeout: int = 30,
) -> bool:
    """Post to Slack. Returns success; NEVER raises, so a delivery failure cannot
    take down a run that has already written the report to disk. ``text`` is
    the notification/accessibility fallback when ``blocks`` are given."""
    try:
        load_slack_env()
    except OSError as exc:
        # An unreadable connections.env must degrade to not-posted, not take
        # down the caller — load_slack_env sat outside this contract until now.
        print(f"team-report: slack env unreadable ({exc}) — not posted", file=sys.stderr)
        return False
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("team-report: no SLACK_BOT_TOKEN — not posted", file=sys.stderr)
        return False
    target = channel or os.environ.get(CHANNEL_ENV) or DEFAULT_CHANNEL
    payload_dict: dict[str, Any] = {
        "channel": target,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        payload_dict["blocks"] = blocks
    body = json.dumps(payload_dict).encode("utf-8")
    request = urllib.request.Request(
        SLACK_URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"team-report: slack post failed: {exc}", file=sys.stderr)
        return False
    if not payload.get("ok"):
        print(f"team-report: slack error: {payload.get('error')}", file=sys.stderr)
        return False
    return True


def report_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    return Path(resolve_var_root(leaf=("team-reports",)))


def write_report_file(text: str, day: str, *, out_dir: str | None = None) -> Path:
    """Persist the report BEFORE any delivery attempt. The file is the record."""
    directory = report_dir(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.md"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def version_conflicts(store: TeamStore, day: str) -> list[tuple[str, str]]:
    """Snapshot rows for ``day`` written by a DIFFERENT score version.

    A row with no recorded version is not a conflict — it predates versioning and
    can be recomputed. A row that names another version is: two rulesets in one
    day's table would make the table's own numbers incomparable.
    """
    conflicts: list[tuple[str, str]] = []
    for row in store.list_snapshots(day=day):
        try:
            breakdown = json.loads(str(row["breakdown_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(breakdown, dict):
            continue
        version = breakdown.get("score_version")
        if version and str(version) != SCORE_VERSION:
            conflicts.append((str(row["employee_id"]), str(version)))
    return conflicts


def write_snapshots(store: TeamStore, gathered: dict[str, Any]) -> int:
    """Upsert one ``prod_snapshots`` row per person for the day. Idempotent."""
    day = str(gathered["day"])
    for person in gathered["people"]:
        diagnostics = person["diagnostics"]
        breakdown = dict(person["breakdown"])
        breakdown["window"] = gathered["window"]
        breakdown["diagnostics"] = diagnostics
        breakdown["bottleneck"] = person["bottleneck"]
        breakdown["recommendation"] = person["recommendation"]
        breakdown["baseline_points"] = person["baseline_points"]
        store.upsert_snapshot(
            day=day,
            employee_id=str(person["employee_id"]),
            verified_points=int(person["points"]),
            verified_outcomes=int(diagnostics["verified_outcomes"]),
            avg_active_sessions=diagnostics["avg_active_sessions"],
            peak_sessions=diagnostics["peak_sessions"],
            merged_prs=diagnostics["merged_prs"],
            first_pass_rate=diagnostics["first_pass_success"],
            production_x=person["production_x"],
            breakdown=breakdown,
        )
    return len(gathered["people"])


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m omniagentos.team.report``. Exit 0 ok, 1 unposted, 2 version pin."""
    parser = argparse.ArgumentParser(description="The 07:00 team production report.")
    parser.add_argument("--day", default=None, help="UTC day (YYYY-MM-DD). Default: today.")
    parser.add_argument("--db", default=None, help="Control-plane database path.")
    parser.add_argument("--out-dir", default=None, help="Override var/team-reports.")
    parser.add_argument("--channel", default=None, help="Slack channel id override.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and print only: no file, no snapshot, no post.",
    )
    parser.add_argument(
        "--no-post", action="store_true", help="Write the file and snapshots; skip Slack."
    )
    args = parser.parse_args(argv)

    day = args.day or utc_now_iso()[:10]
    store = TeamStore(args.db or default_db_path())
    # The scheduled run is the ONE path allowed to resolve yesterday's
    # commitments; every other caller (the preview route) reads.
    gathered = gather(store, day, resolve=True)
    text = render(gathered)

    if args.dry_run:
        print(text)
        return 0

    conflicts = version_conflicts(store, day)
    if conflicts:
        detail = ", ".join(f"{employee}={version}" for employee, version in sorted(conflicts))
        print(
            f"team-report: refusing to write {day}: snapshots exist at another score version "
            f"({detail}); this build is {SCORE_VERSION}. Recompute or archive those rows first.",
            file=sys.stderr,
        )
        return 2

    path = write_report_file(text, day, out_dir=args.out_dir)
    write_snapshots(store, gathered)
    print(str(path))

    if args.no_post:
        return 0
    fallback, blocks = render_slack(gathered)
    if not post(fallback, blocks=blocks, channel=args.channel):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m``
    raise SystemExit(main())


__all__ = [
    "RECOMMENDATIONS",
    "TARGET_X",
    "WINDOW_DAYS",
    "gather",
    "main",
    "post",
    "render",
    "render_slack",
    "version_conflicts",
    "write_report_file",
    "write_snapshots",
]
