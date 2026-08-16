"""Day-of-week ``7`` must be Sunday everywhere it can appear, not just alone.

crontab(5) allows ``7`` as a second spelling of Sunday, so ``6-7`` (weekends),
``1-7`` (every day) and ``*/7`` are ordinary schedules. They were normalised by
a regex substitution on the RAW field text, which rewrote the endpoint of a
range: ``6-7`` became ``6-0``, an interval no value satisfies, so the field
matched nothing and the routine — accepted as ``active`` by
``validate_routine``, which only counts fields — never fired at all.

The regression these pin is silent: a dead routine keeps ``status='active'``,
``last_fired=None`` and ``next_run=None``, which is also what a healthy
event-triggered routine looks like on the API surface.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.routines import compute_next_run, cron_is_due
from omniagentos.scheduler.system_jobs import Schedule, _cron_fire_times, next_fire

# Mon 2026-08-10 .. Sun 2026-08-16, at the scheduled hour.
_WEEK_START = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)


def _days_fired(cron_expr: str) -> list[str]:
    """Weekday abbreviations on which *cron_expr* is due, over one full week."""
    return [
        day.strftime("%a")
        for day in (_WEEK_START + timedelta(days=offset) for offset in range(7))
        if cron_is_due(cron_expr, day, None)
    ]


@pytest.mark.parametrize(
    ("cron_expr", "expected"),
    [
        # A range whose endpoint is the 7-spelling of Sunday.
        ("0 3 * * 6-7", ["Sat", "Sun"]),
        ("0 3 * * 5-7", ["Fri", "Sat", "Sun"]),
        ("0 3 * * 1-7", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
        ("0 3 * * 0-7", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
        # A step whose stride is 7: every seventh day from the field floor.
        ("0 3 * * */7", ["Sun"]),
        # A range WITH a stride, where the stride lands on the 7-spelling.
        ("0 3 * * 5-7/2", ["Fri", "Sun"]),
    ],
)
def test_dow_seven_is_sunday_inside_ranges_and_steps(cron_expr: str, expected: list[str]) -> None:
    assert _days_fired(cron_expr) == expected


@pytest.mark.parametrize(
    ("cron_expr", "expected"),
    [
        # The shapes that already worked must keep working: 7 alone, 0 alone,
        # the comma form, and a range that must NOT be widened to include Sunday.
        ("0 3 * * 7", ["Sun"]),
        ("0 3 * * 0", ["Sun"]),
        ("0 3 * * 6,7", ["Sat", "Sun"]),
        ("0 3 * * 1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("0 3 * * *", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
    ],
)
def test_dow_shapes_that_already_worked_are_unchanged(cron_expr: str, expected: list[str]) -> None:
    assert _days_fired(cron_expr) == expected


def test_a_dow_range_ending_in_seven_has_a_next_run() -> None:
    """``next_run=None`` is how a permanently dead cron routine hides.

    ``compute_next_run`` returns ``None`` both for "not a cron routine" and for
    "no match inside the 400-day search bound", so a dead schedule is
    indistinguishable from an event trigger on ``GET /api/routines``.
    """
    routine = {"trigger_type": "cron", "trigger_config": {"cron": "0 3 * * 6-7"}}
    assert compute_next_run(routine, now=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)) == (
        "2026-08-15T03:00:00Z"  # the coming Saturday
    )


def test_system_jobs_derives_fire_times_for_a_dow_range_ending_in_seven() -> None:
    """The third call site: the health surface reads expected fires from this."""
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    fires = _cron_fire_times("0 3 * * 6-7", now - timedelta(days=7), now + timedelta(days=7))
    assert len(fires) == 4  # two weekends in the 14-day window
    assert next_fire(Schedule(kind="cron", cron="0 3 * * 6-7"), now, None) is not None


def test_no_module_normalises_a_cron_dow_field_by_rewriting_its_text() -> None:
    """Glob the call sites instead of listing them.

    The defect was a textual substitution applied to a cron field before it was
    parsed. Listing the three known sites would have the same failure mode as
    the substitution it replaces, so this asserts the SHAPE is absent from the
    package: no regex ``.sub`` may be applied to a day-of-week field anywhere.
    """
    package_root = Path(__file__).resolve().parents[2] / "omniagentos"
    # A ``.sub(...)`` call whose argument is a cron day-of-week field variable.
    textual_rewrite = re.compile(r"\.sub\([^)]*\bdow\w*\b[^)]*\)")
    offenders = [
        f"{path.relative_to(package_root)}:{lineno}: {line.strip()}"
        for path in sorted(package_root.rglob("*.py"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if textual_rewrite.search(line)
    ]
    assert not offenders, (
        "a cron day-of-week field is being normalised by rewriting its text, which "
        "corrupts range endpoints (6-7 -> 6-0, an empty interval); normalise the "
        "parsed VALUE inside the shared matcher instead:\n  " + "\n  ".join(offenders)
    )
