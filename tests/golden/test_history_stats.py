"""scripts/golden-suite/history_stats.py -- p50/p90 math, the regression
rule (2-consecutive-nights over a rolling prior-window median), DNF
handling, and JSONL append idempotence per (date, name).

Imported via `importlib.import_module` (not a plain `from ... import`)
because `scripts/golden-suite` is a hyphenated directory name -- matching
the repo's `com.omniagentos.*` launchd-job-name idiom -- which can never be
a valid dotted Python import path (`scripts.golden-suite.x` is a syntax
error). `importlib.import_module`'s string form has no such restriction and
resolves the same module scripts.swarm.launchd/scripts.selfimprove.launchd
neighbors resolve via plain imports elsewhere in this test suite.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

history_stats = importlib.import_module("scripts.golden-suite.history_stats")


# ---------------------------------------------------------------------------
# percentile()
# ---------------------------------------------------------------------------


def test_percentile_empty_is_none() -> None:
    assert history_stats.percentile([], 50) is None


def test_percentile_single_value() -> None:
    assert history_stats.percentile([42.0], 50) == 42.0
    assert history_stats.percentile([42.0], 90) == 42.0


def test_percentile_linear_interpolation() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert history_stats.percentile(values, 50) == 3.0
    # rank = (5-1)*0.9 = 3.6 -> interpolate between index 3 (4.0) and 4 (5.0)
    assert history_stats.percentile(values, 90) == pytest.approx(4.6)


def test_percentile_order_independent() -> None:
    assert history_stats.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0


# ---------------------------------------------------------------------------
# rolling_percentiles()
# ---------------------------------------------------------------------------


def _entry(date: str, name: str, seconds: float | None, ref: str = "x") -> dict:
    return {"date": date, "name": name, "seconds": seconds, "dnf_reason": None, "run_ref": ref}


def test_rolling_percentiles_excludes_dnf_and_other_benchmarks() -> None:
    history = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", None),  # DNF, excluded from percentile math
        _entry("2026-07-03", "trivial", 20.0),
        _entry("2026-07-01", "medium", 999.0),  # different benchmark, excluded
    ]
    rollup = history_stats.rolling_percentiles(history, "trivial")
    assert rollup["n"] == 2
    assert rollup["p50"] == history_stats.percentile([10.0, 20.0], 50)


def test_rolling_percentiles_window_keeps_most_recent_by_date() -> None:
    history = [_entry(f"2026-07-{i:02d}", "trivial", float(i)) for i in range(1, 11)]
    rollup = history_stats.rolling_percentiles(history, "trivial", window=3)
    # last 3 by date: 8, 9, 10 -> p50 == 9
    assert rollup["n"] == 3
    assert rollup["p50"] == 9.0


# ---------------------------------------------------------------------------
# rolling_baseline_median() / is_regression_night()
# ---------------------------------------------------------------------------


def test_rolling_baseline_median_uses_only_prior_entries() -> None:
    entries = [_entry(f"2026-07-{i:02d}", "trivial", 10.0) for i in range(1, 8)]
    # index 7 (an 8th, hypothetical) would look at entries[0:7] -- but here
    # test index 3: prior entries are indices 0,1,2 (all 10.0) -> median 10.0
    assert history_stats.rolling_baseline_median(entries, 3, window=7) == 10.0


def test_rolling_baseline_median_none_with_no_prior_history() -> None:
    entries = [_entry("2026-07-01", "trivial", 10.0)]
    assert history_stats.rolling_baseline_median(entries, 0, window=7) is None


def test_rolling_baseline_median_ignores_dnf_entries() -> None:
    entries = [
        _entry("2026-07-01", "trivial", None),
        _entry("2026-07-02", "trivial", None),
        _entry("2026-07-03", "trivial", 12.0),
    ]
    # index 3 (hypothetical next night) would use entries[0:3]; only one
    # numeric value (12.0) among the DNFs -> median is that value.
    assert history_stats.rolling_baseline_median(entries, 3, window=7) == 12.0


def test_is_regression_night_dnf_always_regresses_with_a_baseline() -> None:
    entries = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", None),  # DNF
    ]
    assert history_stats.is_regression_night(entries, 1) is True


def test_is_regression_night_no_baseline_never_regresses() -> None:
    entries = [_entry("2026-07-01", "trivial", None)]
    assert history_stats.is_regression_night(entries, 0) is False


def test_is_regression_night_threshold_boundary() -> None:
    # Each case gets its OWN single-entry baseline (10.0) so the two
    # comparisons stay independent -- a shared list would let the first
    # test night's value bleed into the second night's rolling median.
    at_threshold = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 12.5),  # exactly +25% -- not STRICTLY over
    ]
    over_threshold = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 12.6),  # just over +25%
    ]
    assert history_stats.is_regression_night(at_threshold, 1, threshold_pct=25.0) is False
    assert history_stats.is_regression_night(over_threshold, 1, threshold_pct=25.0) is True


# ---------------------------------------------------------------------------
# check_regression() -- the 2-consecutive-nights rule
# ---------------------------------------------------------------------------


def test_check_regression_requires_consecutive_nights_not_just_one() -> None:
    # One bad night sandwiched between good ones must never fire.
    history = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 10.0),
        _entry("2026-07-03", "trivial", 30.0),  # regression night
        _entry("2026-07-04", "trivial", 10.0),  # back to baseline -- streak broken
    ]
    assert history_stats.check_regression(history, "trivial", consecutive_nights=2) is False


def test_check_regression_fires_on_two_consecutive_nights() -> None:
    history = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 10.0),
        _entry("2026-07-03", "trivial", 30.0),  # regression night 1
        _entry("2026-07-04", "trivial", 30.0),  # regression night 2 -- fires
    ]
    assert history_stats.check_regression(history, "trivial", consecutive_nights=2) is True


def test_check_regression_dnf_streak_fires() -> None:
    history = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 10.0),
        _entry("2026-07-03", "trivial", None),  # DNF night 1
        _entry("2026-07-04", "trivial", None),  # DNF night 2 -- fires
    ]
    assert history_stats.check_regression(history, "trivial", consecutive_nights=2) is True


def test_check_regression_cold_start_insufficient_entries() -> None:
    history = [_entry("2026-07-01", "trivial", 999.0)]
    assert history_stats.check_regression(history, "trivial", consecutive_nights=2) is False


def test_check_regression_ignores_other_benchmarks() -> None:
    history = [
        _entry("2026-07-01", "trivial", 10.0),
        _entry("2026-07-02", "trivial", 10.0),
        _entry("2026-07-03", "trivial", 30.0),
        _entry("2026-07-04", "trivial", 30.0),
        # `medium` has no history at all -- must not spuriously regress.
    ]
    assert history_stats.check_regression(history, "medium", consecutive_nights=2) is False


# ---------------------------------------------------------------------------
# read_history() / append_history() -- JSONL bookkeeping
# ---------------------------------------------------------------------------


def test_read_history_missing_file_is_empty(tmp_path: Path) -> None:
    assert history_stats.read_history(tmp_path / "nope.jsonl") == []


def test_read_history_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"date": "2026-07-01", "name": "trivial", "seconds": 1.0}\n'
        "not json at all\n"
        "\n"
        '["also", "not", "a", "dict"]\n'
        '{"date": "2026-07-02", "name": "trivial", "seconds": 2.0}\n'
    )
    entries = history_stats.read_history(path)
    assert len(entries) == 2
    assert entries[0]["seconds"] == 1.0
    assert entries[1]["seconds"] == 2.0


def test_append_history_writes_one_line(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    appended = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 5.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    assert appended is True
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["name"] == "trivial"


def test_append_history_idempotent_per_date_name(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    first = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 5.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    # A second append for the SAME (date, name) -- even with different
    # content -- must be a no-op: exactly one line for that key, ever.
    second = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 999.0,
            "dnf_reason": "different",
            "run_ref": {},
        },
    )
    assert first is True
    assert second is False
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["seconds"] == 5.0  # the FIRST write wins; second was a no-op


def test_append_history_allows_different_name_same_date(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 5.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    appended = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "medium",
            "seconds": 50.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    assert appended is True
    assert len(path.read_text().splitlines()) == 2


def test_append_history_allows_same_name_different_date(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history_stats.append_history(
        path,
        {
            "date": "2026-07-23",
            "name": "trivial",
            "seconds": 5.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    appended = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 6.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    assert appended is True
    assert len(path.read_text().splitlines()) == 2


def test_append_history_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "history.jsonl"
    appended = history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "trivial",
            "seconds": 5.0,
            "dnf_reason": None,
            "run_ref": {},
        },
    )
    assert appended is True
    assert path.exists()


def test_append_history_records_dnf_with_null_seconds(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history_stats.append_history(
        path,
        {
            "date": "2026-07-24",
            "name": "swarm",
            "seconds": None,
            "dnf_reason": "timeout",
            "run_ref": {},
        },
    )
    row = json.loads(path.read_text().splitlines()[0])
    assert row["seconds"] is None
    assert row["dnf_reason"] == "timeout"
