"""Point floors, the ratchet schedule, and Friday pace — POLICY over scoring.

This module answers exactly three questions:

* **What is this week's floor?** ``configs/team_points.yaml`` sets week 1 and
  week 2 explicitly, then raises the floor ``ratchet_pct``% every
  ``ratchet_every_weeks`` weeks. The schedule is arithmetic over config, so a
  policy change is a reviewed YAML edit, never a code change.
* **Is this person on pace for Friday?** Pace compares VERIFIED points so far
  this week (:func:`omniagentos.team.scoring.compute_scores` — the same
  function the report and the scoreboard use, so no second implementation of a
  point exists) against the floor prorated across the Mon-Fri workweek.
* **Is a raise due?** On the announce day (Friday), one line names next week's
  floor when it differs from this week's.

What this module deliberately does NOT do: touch scoring semantics. It never
weights, never rounds a card's points, never re-reads evidence — it consumes
:func:`compute_scores` output and compares it to config. The scoring rules
(verified-only, S=1 M=3 L=8, TARGET_X=10) live in ``scoring.py`` and are
sacred.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from omniagentos.team.contracts import OPERATOR_EMPLOYEE_ID
from omniagentos.team.scoring import ScoreSource, compute_scores

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "PaceStatus",
    "PointsConfig",
    "active_dev_ids",
    "floor_for_week",
    "friday_announcement",
    "load_points_config",
    "pace_line",
    "pace_statuses",
    "utc_today",
    "week_index",
    "week_start",
]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "configs/team_points.yaml"

#: Mon..Fri are the days pace is earned over; the weekend owes the full floor.
_WORKDAYS = 5


@dataclass(frozen=True)
class PointsConfig:
    """The floor policy, as configured. Defaults mirror ``team_points.yaml``."""

    program_start: date = date(2026, 8, 10)
    week1_floor: int = 10
    week2_floor: int = 15
    ratchet_pct: int = 20
    ratchet_every_weeks: int = 2
    announce_day: str = "friday"


@dataclass(frozen=True)
class PaceStatus:
    """One person's Friday pace: measured points against the prorated floor."""

    employee_id: str
    points: int
    floor: int
    prorated_target: float
    on_pace: bool

    @property
    def short_by(self) -> float:
        """How far behind the prorated line this person is (0 when on pace)."""
        return max(0.0, self.prorated_target - float(self.points))


def load_points_config(path: Path | None = None) -> PointsConfig:
    """``configs/team_points.yaml`` as a :class:`PointsConfig`.

    A missing or malformed file degrades to the coded defaults (which mirror
    the shipped YAML) with one stderr line — the pulse must render with a
    default floor rather than lose the whole message to a config typo.
    """
    target = path or DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"team-points: could not load {target}: {exc}; using defaults", file=sys.stderr)
        return PointsConfig()
    if not isinstance(raw, dict):
        print(f"team-points: {target} is not a mapping; using defaults", file=sys.stderr)
        return PointsConfig()
    defaults = PointsConfig()

    def _int(key: str, fallback: int) -> int:
        try:
            value = int(raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    try:
        program_start = date.fromisoformat(str(raw.get("program_start", "")))
    except ValueError:
        program_start = defaults.program_start
    return PointsConfig(
        program_start=program_start,
        week1_floor=_int("week1_floor", defaults.week1_floor),
        week2_floor=_int("week2_floor", defaults.week2_floor),
        ratchet_pct=_int("ratchet_pct", defaults.ratchet_pct),
        ratchet_every_weeks=_int("ratchet_every_weeks", defaults.ratchet_every_weeks),
        announce_day=str(raw.get("announce_day", defaults.announce_day)).lower(),
    )


def utc_today() -> date:
    """Today on the UTC calendar — scoring windows are UTC strings, so pace
    comparisons must not drift with the host timezone."""
    return datetime.now(UTC).date()


def week_start(day: date) -> date:
    """The Monday of ``day``'s week (UTC calendar)."""
    return day - timedelta(days=day.weekday())


def week_index(day: date, config: PointsConfig) -> int:
    """1-based program week for ``day``; days before the program count as week 1."""
    start = week_start(config.program_start)
    return max(1, (week_start(day) - start).days // 7 + 1)


def floor_for_week(week: int, config: PointsConfig) -> int:
    """The verified-point floor for 1-based program ``week``.

    Week 1 and week 2 are configured explicitly; from week 3 on, the week-2
    floor rises ``ratchet_pct``% once every ``ratchet_every_weeks`` weeks,
    rounded to the nearest whole point (a floor is a target someone reads in
    Slack — 21.6 is not a target).
    """
    if week <= 1:
        return config.week1_floor
    if week == 2:
        return config.week2_floor
    ratchets = (week - 3) // config.ratchet_every_weeks + 1
    floor = float(config.week2_floor)
    for _ in range(ratchets):
        floor *= 1.0 + config.ratchet_pct / 100.0
    return round(floor)


def _prorated_target(floor: int, day: date) -> float:
    """The floor prorated to Friday: Mon owes 1/5, Fri (and the weekend) 5/5."""
    elapsed = min(day.weekday() + 1, _WORKDAYS)
    return floor * elapsed / _WORKDAYS


def pace_statuses(
    source: ScoreSource,
    employee_ids: list[str],
    *,
    config: PointsConfig | None = None,
    today: date | None = None,
) -> dict[str, PaceStatus]:
    """Friday pace for each named person, from VERIFIED points this week.

    ``employee_ids`` is the caller's roster slice (the pulse passes its active
    Slack-mapped devs); ids missing from the score map read as 0 points, which
    is the honest answer for someone with no verified card this week.
    """
    cfg = config or load_points_config()
    day = today or utc_today()
    floor = floor_for_week(week_index(day, cfg), cfg)
    target = _prorated_target(floor, day)
    monday = week_start(day)
    scores = compute_scores(source, period_start=monday.isoformat(), period_end=day.isoformat())
    out: dict[str, PaceStatus] = {}
    for employee_id in employee_ids:
        breakdown = scores.get(employee_id)
        points = 0 if breakdown is None else int(breakdown.score)
        out[employee_id] = PaceStatus(
            employee_id=employee_id,
            points=points,
            floor=floor,
            prorated_target=target,
            on_pace=float(points) >= target,
        )
    return out


def pace_line(status: PaceStatus) -> str:
    """One pulse line: ``⚠ emp_bob 4/15 pts, Friday pace short`` (or ✓)."""
    if status.on_pace:
        return f"✓ {status.employee_id} {status.points}/{status.floor} pts, on pace"
    return f"⚠ {status.employee_id} {status.points}/{status.floor} pts, Friday pace short"


def friday_announcement(config: PointsConfig, today: date) -> str | None:
    """The raise line for the announce day, or ``None`` on every other day.

    Emitted only when NEXT week's floor is higher than this week's — an
    announce day inside a flat stretch of the schedule stays quiet rather than
    re-announcing the same floor.
    """
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if weekdays[today.weekday()] != config.announce_day:
        return None
    this_week = floor_for_week(week_index(today, config), config)
    next_week = floor_for_week(week_index(today + timedelta(days=7), config), config)
    if next_week <= this_week:
        return None
    return (
        f"📈 Point floor rises Monday: {this_week} → {next_week} verified pts/week "
        f"(+{config.ratchet_pct}% ratchet)"
    )


def active_dev_ids(employee_ids: list[str]) -> list[str]:
    """The floor-bearing subset of a roster slice: everyone but the operator."""
    return [employee_id for employee_id in employee_ids if employee_id != OPERATOR_EMPLOYEE_ID]
