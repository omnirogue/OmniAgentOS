"""PRIORITY 1: the ET calendar-day window (the timezone-bug fix).

The load-bearing proof: an 11 PM Eastern charge on day D must land in day D, not
in D+1. Under the old UTC-midnight window it did not — 11 PM ET is 03:00 UTC the
next day, so it fell outside day D's UTC window and was smeared into D+1.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from omniagentos.goals.collect import EASTERN, _day_bounds, eastern_yesterday

UTC = ZoneInfo("UTC")


def _et_charge_ts(year: int, month: int, day: int, hour: int) -> int:
    """Unix seconds of a charge at a given ET wall-clock time."""
    return int(datetime(year, month, day, hour, tzinfo=EASTERN).timestamp())


def test_11pm_et_charge_lands_in_the_correct_eastern_day() -> None:
    target = date(2026, 7, 10)  # summer -> EDT (-04:00)
    start, end, captured_at = _day_bounds(target)

    charge = _et_charge_ts(2026, 7, 10, 23)  # 11 PM ET on day D
    assert start <= charge <= end, "11 PM ET charge must fall inside day D"

    # The day label the snapshot store dedups on is the ET calendar day.
    assert captured_at[:10] == "2026-07-10"
    # It is an honest offset stamp, never a UTC-labelled ('Z') Eastern time.
    assert captured_at.endswith("-04:00")

    # Regression guard: the OLD UTC-midnight window ended at 23:59:59 UTC on day D,
    # which is 19:59:59 ET — so an 11 PM ET charge (03:00 UTC on D+1) fell OUTSIDE
    # it. Prove the charge is beyond that old boundary, i.e. the fix is real.
    old_utc_end = int(datetime(2026, 7, 10, 23, 59, 59, tzinfo=UTC).timestamp())
    assert charge > old_utc_end


def test_day_boundaries_are_exclusive_at_the_edges() -> None:
    target = date(2026, 7, 10)
    start, end, _ = _day_bounds(target)

    just_after_midnight = _et_charge_ts(2026, 7, 10, 0)
    assert start <= just_after_midnight <= end

    # 11 PM ET the day BEFORE belongs to day D-1, not day D.
    prev_day_late = _et_charge_ts(2026, 7, 9, 23)
    assert prev_day_late < start

    # Midnight of the NEXT day belongs to day D+1, not day D.
    next_midnight = int(datetime(2026, 7, 11, 0, tzinfo=EASTERN).timestamp())
    assert next_midnight > end


def test_winter_day_uses_est_offset() -> None:
    target = date(2026, 1, 15)  # winter -> EST (-05:00)
    start, end, captured_at = _day_bounds(target)
    assert captured_at.endswith("-05:00")
    charge = _et_charge_ts(2026, 1, 15, 23)
    assert start <= charge <= end


def test_eastern_yesterday_is_one_day_before_today_et() -> None:
    today_et = datetime.now(EASTERN).date()
    assert eastern_yesterday() == today_et - timedelta(days=1)
