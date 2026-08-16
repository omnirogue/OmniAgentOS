"""Reader unit tests: honesty semantics first.

Absence, present-but-unusable sources, malformed rows, sidecars, UTC bucketing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omniagentos.testobs import (
    read_diagnostics,
    read_memory,
    read_northstar,
    read_suite,
    snapshot,
    weakspot_rank,
)
from omniagentos.testobs.readers import _clear_cache as clear_cache
from tests.testobs.conftest import (
    FAIL_REASON,
    add_eval_row,
    append_ledger,
    day_ago,
    fh_abort,
    fh_did_not_run,
    fh_row,
    receipt,
    seed_northstar,
    ts_ago,
    ts_offset,
    write_junit,
    write_ledger,
    write_receipt,
)

READERS = (read_northstar, read_memory, read_diagnostics, read_suite)

# A reason is operator-facing prose on a gated route; it still must not describe
# local layout or name an exception class.
_PATH_FRAGMENT = re.compile(r"(/|\\|var\b|\.sqlite3|\.jsonl|\.xml|\.json)")
_CLASS_NAME = re.compile(r"\b\w*(Error|Exception)\b")


def _assert_reason_is_clean(reason: str) -> None:
    assert reason and reason == reason.strip()
    assert not _PATH_FRAGMENT.search(reason), f"reason leaks layout: {reason!r}"
    assert not _CLASS_NAME.search(reason), f"reason leaks an exception class: {reason!r}"


def test_every_category_is_absent_with_a_clean_reason_on_an_empty_var(var_root: Path) -> None:
    for reader in READERS:
        result = reader(90)
        assert result["available"] is False
        _assert_reason_is_clean(result["reason"])
        assert result["overview"] == {"available": False, "reason": result["reason"]}
        assert result["series"] == [] and result["weakspots"] == []


def test_northstar_reads_series_latest_run_and_skips_corrupt_rows(var_root: Path) -> None:
    seed_northstar(var_root)
    result = read_northstar(90)

    assert result["available"] is True
    overview = result["overview"]
    assert overview["run_id"] == "nscert-t1-20260813T101005Z"
    assert overview["distance"] == 23.7
    assert overview["delta_distance"] == -1.4
    assert overview["gate_pass_rate"] == 88.2
    assert (overview["checks_pass"], overview["checks_fail"]) == (1, 1)
    assert overview["checks_not_evaluable"] == 1 and overview["checks_void"] == 0
    # The corrupt eval_results row is counted, not raised.
    assert result["skipped_rows"] == 1

    distance = next(s for s in result["series"] if s["metric"] == "nsc.distance")
    assert [p["date"] for p in distance["points"]] == [day_ago(2), day_ago(1)]
    assert distance["unit"] == "%"

    failing = next(w for w in result["weakspots"] if w["status"] == "FAIL")
    assert failing["id"] == "NSC-C11-01"
    # Decoded with the recorder's OWN alphabet: the reason round-trips exactly,
    # including the character that differs between the two base64 alphabets.
    assert failing["detail"] == FAIL_REASON
    assert failing["since"] == day_ago(1)
    assert {w["status"] for w in result["weakspots"]} == {"FAIL", "NOT_EVALUABLE"}


def test_northstar_window_excludes_older_points(var_root: Path) -> None:
    seed_northstar(var_root)
    points = next(
        s for s in read_northstar(1)["series"] if s["metric"] == "nsc.distance"
    )["points"]
    assert [p["date"] for p in points] == [day_ago(1)]


def test_northstar_run_that_scored_nothing_is_not_available(var_root: Path) -> None:
    """A present database whose latest run scored no check measured nothing."""
    seed_northstar(var_root, scored=False)
    result = read_northstar(90)
    assert result["available"] is False
    assert result["reason"] == "no scored checks in the latest northstar-cert run"
    assert result["skipped_rows"] == 3  # 2 unscored rows + the corrupt one


def test_memory_is_absent_until_memcert_persists_a_run(var_root: Path) -> None:
    result = read_memory(90)
    assert result["available"] is False
    assert result["reason"] == "no durable memcert runs recorded yet"
    assert "pass_rate" not in result["overview"]


def test_memory_reads_a_junit_run_without_counting_skips(var_root: Path) -> None:
    write_junit(
        var_root / "memcert" / "runs" / "mc1" / "junit.xml",
        tests=100, failures=2, errors=3, skipped=10,
    )
    result = read_memory(90)
    assert result["available"] is True
    assert result["overview"]["runs"] == 1
    # 90 ran, 5 bad -> 94.4%; the 10 skips are excluded from both sides.
    assert result["overview"]["pass_rate"] == 94.4
    assert result["series"][0]["metric"] == "memcert.pass_rate"


def test_memory_orders_same_day_runs_by_mtime_not_directory_name(var_root: Path) -> None:
    """``run-b`` sorts last by name but ran FIRST; the later run must win."""
    import os

    older = write_junit(
        var_root / "memcert" / "runs" / "run-b" / "junit.xml",
        tests=10, failures=5, errors=0, skipped=0,
    )
    newer = write_junit(
        var_root / "memcert" / "runs" / "run-a" / "junit.xml",
        tests=10, failures=1, errors=0, skipped=0,
    )
    os.utime(older, (1_760_000_000, 1_760_000_000))
    os.utime(newer, (1_760_000_600, 1_760_000_600))

    overview = read_memory(36500)["overview"]
    assert overview["failures"] == 1
    assert overview["pass_rate"] == 90.0


def test_memory_skips_an_inconsistent_junit_summary(var_root: Path) -> None:
    write_junit(
        var_root / "memcert" / "runs" / "mc1" / "junit.xml",
        tests=5, failures=9, errors=0, skipped=0,
    )
    result = read_memory(90)
    assert result["available"] is False
    assert result["skipped_rows"] == 1


def test_diagnostics_skips_malformed_lines_and_excludes_did_not_run(var_root: Path) -> None:
    write_ledger(
        var_root,
        [
            {"ts": ts_ago(1), "tier": "tier1", "feature": "api_ui", "passed": 9,
             "failed": 1, "errors": 0, "status": "ok",
             "failures": [{"nodeid": "tests/api/test_x.py::test_a"}]},
            {"ts": ts_ago(1), "tier": "tier1", "feature": "sessions", "passed": 10,
             "failed": 0, "errors": 0, "status": "ok"},
            # A negative count is corruption: the row is skipped whole.
            {"ts": ts_ago(1), "tier": "tier1", "feature": "sessions", "passed": -5,
             "failed": 0, "errors": 0, "status": "ok"},
            # did_not_run/aborted: excluded from the pass rate, counted as aborted.
            {"ts": ts_ago(1), "tier": "tier3", "feature": "__lane__", "passed": 0,
             "failed": 0, "errors": 0, "aborted": True, "did_not_run": True,
             "abort_reason": "load-guard", "status": "error"},
        ],
        extra_lines=["{not json at all", "   ", "[1, 2, 3]", '{"no": "timestamp"}'],
    )
    result = read_diagnostics(90)

    assert result["available"] is True
    # bad line + non-object + timestampless + negative-count row; blank ignored.
    assert result["skipped_rows"] == 4
    assert result["overview"]["aborted_recent"] == 1
    assert result["overview"]["features_total"] == 2
    assert result["overview"]["features_failing"] == 1

    tier1 = next(s for s in result["series"] if s["metric"] == "fh.tier1.pass_rate")
    assert tier1["points"][-1]["value"] == 95.0  # 19 passed / 20 non-aborted outcomes
    # The aborted tier3 row produced no outcomes, so it has no point at all.
    assert not [s for s in result["series"] if s["metric"] == "fh.tier3.pass_rate"]

    failing = [w for w in result["weakspots"] if w["status"] == "FAIL"]
    assert [w["id"] for w in failing] == ["api_ui/tier1"]
    assert "tests/api/test_x.py::test_a" in failing[0]["detail"]


def test_diagnostics_window_of_only_aborted_runs_is_not_available(var_root: Path) -> None:
    """Rows exist, but nothing completed: that is unknown, not a 0% pass rate."""
    write_ledger(
        var_root,
        [
            {"ts": ts_ago(1), "tier": "tier1", "feature": "__lane__", "passed": 0,
             "failed": 0, "errors": 0, "aborted": True, "did_not_run": True,
             "abort_reason": "load-guard", "status": "error"},
            {"ts": ts_ago(2), "tier": "tier3", "feature": "__lane__", "passed": 0,
             "failed": 0, "errors": 0, "aborted": True, "did_not_run": True,
             "abort_reason": "load-guard", "status": "error"},
        ],
    )
    result = read_diagnostics(90)
    assert result["available"] is False
    assert result["reason"] == "no completed feature-health runs in the requested window"
    assert result["overview"] == {"available": False, "reason": result["reason"]}


def test_diagnostics_staleness_is_per_cell_not_per_feature(var_root: Path) -> None:
    """One feature, two tiers: tier1 still runs, tier3 stopped nine days ago."""
    write_ledger(
        var_root,
        [
            {"ts": ts_ago(0), "tier": "tier1", "feature": "memory", "passed": 5,
             "failed": 0, "errors": 0, "status": "ok"},
            {"ts": ts_ago(9), "tier": "tier3", "feature": "memory", "passed": 5,
             "failed": 0, "errors": 0, "status": "ok"},
        ],
    )
    result = read_diagnostics(90)
    assert result["overview"]["cells_stale"] == 1
    assert result["overview"]["features_stale"] == 1
    assert [(w["id"], w["status"]) for w in result["weakspots"]] == [("memory/tier3", "STALE")]


def test_diagnostics_buckets_offset_timestamps_by_utc_date(var_root: Path) -> None:
    stamp, utc_date = ts_offset(1)
    assert stamp[:10] != utc_date, "fixture must straddle the date boundary"
    write_ledger(
        var_root,
        [{"ts": stamp, "tier": "tier1", "feature": "api_ui", "passed": 4, "failed": 0,
          "errors": 0, "status": "ok"}],
    )
    series = read_diagnostics(90)["series"][0]
    assert [p["date"] for p in series["points"]] == [utc_date]


def test_suite_skips_sidecars_run_envelopes_and_receipts_outside_the_window(
    var_root: Path,
) -> None:
    write_receipt(var_root, "aaa.json", receipt(9, 1))
    write_receipt(var_root, "bbb.json", receipt(10, 0, days_ago=1), age_days=1)
    # Sidecars: must never be parsed, never counted, never skipped-counted.
    write_receipt(var_root, "ccc.counterfeit-20260813T000000Z.json", receipt(1, 99))
    write_receipt(var_root, "ddd.ladder-20260813T000000Z.json", receipt(1, 99))
    write_receipt(var_root, "eee.junit-20260813T000000Z", "<xml/>")
    # A run-envelope receipt carries no check counts, and one file is corrupt.
    write_receipt(var_root, "fff.run-20260813T000000Z-1.json", {"exit_code": 0, "steps": []})
    write_receipt(var_root, "ggg.json", "{not json")
    # More outcomes than checks collected: an inconsistent receipt, skipped.
    write_receipt(var_root, "iii.json", {**receipt(9, 1), "checks_collected": 2})
    # Outside the window: filtered on st_mtime before it is ever opened.
    write_receipt(var_root, "hhh.json", receipt(0, 500, days_ago=200), age_days=200)

    result = read_suite(30)
    assert result["available"] is True
    assert result["overview"]["runs_7d"] == 2
    assert result["overview"]["pass_rate_7d"] == 95.0  # 19 of 20 checks
    # Only the corrupt file and the inconsistent receipt count as skipped: a run
    # envelope is a valid receipt of a different schema, not a malformed row.
    assert result["skipped_rows"] == 2
    rates = next(s for s in result["series"] if s["metric"] == "gate.pass_rate")
    assert [p["date"] for p in rates["points"]] == [day_ago(1), day_ago(0)]
    runs = next(s for s in result["series"] if s["metric"] == "gate.runs")
    assert [p["value"] for p in runs["points"]] == [1.0, 1.0]


def test_suite_pass_rate_is_none_when_nothing_was_checked(var_root: Path) -> None:
    """Receipts that collected zero checks are runs, not a 0% (or 100%) pass rate."""
    write_receipt(var_root, "aaa.json", receipt(0, 0))
    result = read_suite(30)
    assert result["available"] is True
    assert result["overview"]["runs_7d"] == 1
    assert result["overview"]["pass_rate_7d"] is None
    assert next(s for s in result["series"] if s["metric"] == "gate.pass_rate")["points"] == []


def test_suite_with_only_corrupt_receipts_is_not_available(var_root: Path) -> None:
    for name in ("aaa.json", "bbb.json"):
        write_receipt(var_root, name, "{not json")
    result = read_suite(30)
    assert result["available"] is False
    assert result["reason"] == "no usable merge-gate receipts in the requested window"
    assert result["skipped_rows"] == 2  # the true count survives the refusal


def test_suite_reports_the_newest_lane_junit(var_root: Path) -> None:
    write_receipt(var_root, "aaa.json", receipt(5, 0))
    write_junit(
        var_root / "test-reports" / "fast-lane-latest.xml",
        tests=11510, failures=3, errors=0, skipped=12,
    )
    lane = read_suite(30)["overview"]["latest_lane"]
    assert lane["name"] == "fast-lane"
    assert (lane["tests"], lane["failures"], lane["errors"], lane["skipped"]) == (11510, 3, 0, 12)
    assert "mtime" not in lane


def test_snapshot_caches_successes_only(var_root: Path) -> None:
    """A failure must not be pinned for a minute; a success may be."""
    first = snapshot("suite", 30)
    assert first["available"] is False
    assert snapshot("suite", 30) is not first  # not cached: the source is re-read

    write_receipt(var_root, "aaa.json", receipt(5, 0))
    good = snapshot("suite", 30)
    assert good["available"] is True
    assert snapshot("suite", 30) is good  # inside the 60s TTL
    assert snapshot("suite", 30, refresh=True) is not good


def test_snapshot_cache_is_lru_bounded(var_root: Path) -> None:
    from omniagentos.testobs import readers

    write_receipt(var_root, "aaa.json", receipt(5, 0))
    for days in range(1, 60):
        snapshot("suite", days)
    assert len(readers._cache) <= readers._CACHE_MAXSIZE


def test_weakspot_rank_puts_gate_failures_first(var_root: Path) -> None:
    items = [
        {"category": "northstar", "status": "NOT_EVALUABLE", "gate": False, "id": "c"},
        {"category": "diagnostics", "status": "STALE", "gate": None, "id": "e"},
        {"category": "diagnostics", "status": "FAIL", "gate": None, "id": "d"},
        {"category": "northstar", "status": "FAIL", "gate": False, "id": "b"},
        {"category": "northstar", "status": "FAIL", "gate": True, "id": "a"},
    ]
    assert [i["id"] for i in sorted(items, key=weakspot_rank)] == ["a", "b", "d", "c", "e"]


# --------------------------------------------------------------- round-2 regressions


def test_diagnostics_recovery_clears_a_failing_cell(var_root: Path) -> None:
    """fail then pass on the SAME stream: the cell is no longer failing."""
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2), fh_row(
        "memory", "tier1", passed=9, failed=1, report_path="20260814T050000Z-tier1.xml",
        failures=[{"nodeid": "tests/test_memory.py::test_fixed"}]))
    append_ledger(var_root, now - timedelta(hours=1), fh_row(
        "memory", "tier1", passed=10, report_path="20260814T060000Z-tier1.xml"))

    result = read_diagnostics(14)
    assert result["overview"]["features_failing"] == 0
    assert result["weakspots"] == []


def test_diagnostics_regression_after_a_pass_is_failing(var_root: Path) -> None:
    """pass then fail on the same stream: the newest verdict rules."""
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2), fh_row(
        "memory", "tier1", passed=10, report_path="20260814T050000Z-tier1.xml"))
    append_ledger(var_root, now - timedelta(hours=1), fh_row(
        "memory", "tier1", passed=9, failed=1, report_path="20260814T060000Z-tier1.xml",
        failures=[{"nodeid": "tests/test_memory.py::test_broke"}]))

    result = read_diagnostics(14)
    assert result["overview"]["features_failing"] == 1
    assert [(w["id"], w["status"]) for w in result["weakspots"]] == [("memory/tier1", "FAIL")]
    assert "tests/test_memory.py::test_broke" in result["weakspots"][0]["detail"]


def test_diagnostics_a_green_sibling_stream_cannot_erase_a_standing_failure(
    var_root: Path,
) -> None:
    """Worst-per-cell over latest-per-STREAM, per scripts/feature_health/fh.py.

    Collapsing a cell to "newest record" is the masking reduction fh.py records
    three previous readers growing: a live-probe stream stays red while a
    Playwright append lands green a minute later.
    """
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2), fh_row(
        "api_ui", "tier3", passed=1, failed=1, report_path="20260814T050000Z-liveprobe.xml",
        failures=[{"nodeid": "tests/live/test_probe.py::test_up"}]))
    append_ledger(var_root, now - timedelta(hours=1), fh_row(
        "api_ui", "tier3", passed=20, report_path="20260814T060000Z-playwright.xml"))

    result = read_diagnostics(14)
    assert result["overview"]["features_failing"] == 1
    assert [w["id"] for w in result["weakspots"]] == ["api_ui/tier3"]
    assert "tests/live/test_probe.py::test_up" in result["weakspots"][0]["detail"]


def test_diagnostics_failing_since_is_the_start_of_the_streak(var_root: Path) -> None:
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(days=5), fh_row(
        "memory", "tier1", passed=10, report_path="a-tier1.xml"))
    for day in (3, 2, 1):
        append_ledger(var_root, now - timedelta(days=day), fh_row(
            "memory", "tier1", passed=9, failed=1, report_path="a-tier1.xml"))

    weakspot = read_diagnostics(14)["weakspots"][0]
    assert weakspot["since"] == (now - timedelta(days=3)).date().isoformat()


def test_diagnostics_reads_every_shard_the_window_covers(var_root: Path) -> None:
    """A 365-day window must not be truncated to the last two month shards."""
    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    append_ledger(var_root, old, fh_row("historical", "tier1", passed=1))
    append_ledger(var_root, now, fh_row("fresh", "tier1", passed=1))

    dates = {p["date"] for p in read_diagnostics(365)["series"][0]["points"]}
    assert old.date().isoformat() in dates
    assert now.date().isoformat() in dates


def test_diagnostics_keeps_a_stopped_cell_stale_past_the_window(var_root: Path) -> None:
    """Last-seen is resolved over a fixed horizon, not over ``days``."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("fresh", "tier1", passed=1))
    append_ledger(var_root, now - timedelta(days=20), fh_row("stopped", "tier3", passed=1))

    result = read_diagnostics(14)
    assert result["available"] is True
    assert result["overview"]["features_stale"] == 1
    assert ("stopped/tier3", "STALE") in [(w["id"], w["status"]) for w in result["weakspots"]]


def test_diagnostics_skips_a_row_that_never_recorded_its_counts(var_root: Path) -> None:
    """A missing count is not a completed zero — the row is corruption."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("real", "tier1", passed=1))
    append_ledger(var_root, now, {"feature": "phantom", "tier": "tier1"})

    result = read_diagnostics(30)
    assert result["overview"]["features_total"] == 1
    assert result["skipped_rows"] == 1


def test_diagnostics_survives_a_non_finite_count(var_root: Path) -> None:
    """JSON admits NaN; int(NaN) raises. One bad row must not kill the category."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("real", "tier1", passed=1))
    append_ledger(var_root, now, fh_row("broken", "tier1", passed=float("nan")))

    result = read_diagnostics(30)
    assert result["available"] is True
    assert result["overview"]["features_total"] == 1
    assert result["skipped_rows"] == 1


def test_northstar_picks_the_latest_run_by_parsed_timestamp(var_root: Path) -> None:
    """SQL MAX is lexical: an offset stamp sorts before the UTC time it postdates."""
    seed_northstar(var_root, run_id="utc-latest", created_at="2026-08-13T20:00:00-05:00")
    seed_northstar(var_root, run_id="lexically-newer", created_at="2026-08-14T00:30:00+00:00")
    assert read_northstar(36500)["overview"]["run_id"] == "utc-latest"


def test_suite_excludes_a_new_file_recording_an_old_run(var_root: Path) -> None:
    """mtime is the cheap prefilter; the receipt's own timestamp decides membership."""
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    write_receipt(var_root, "copied-today.json", receipt(9, 1, started_at=old))
    write_receipt(var_root, "in-window.json", receipt(5, 0))

    result = read_suite(30)
    runs = next(s for s in result["series"] if s["metric"] == "gate.runs")
    assert [p["date"] for p in runs["points"]] == [day_ago(0)]
    assert result["overview"]["runs_7d"] == 1


def test_suite_without_any_receipts_reports_unmeasured_not_zero(var_root: Path) -> None:
    """Lane JUnit only: no gate run was measured, so runs_7d is null, not 0."""
    (var_root / "gate-evidence" / "records" / "merge-gate").mkdir(parents=True)
    write_junit(
        var_root / "test-reports" / "fast-lane-latest.xml",
        tests=10, failures=0, errors=0, skipped=0,
    )
    result = read_suite(30)
    assert result["available"] is True  # the lane report IS a real measurement
    assert result["overview"]["runs_7d"] is None
    assert result["overview"]["pass_rate_7d"] is None
    assert result["overview"]["latest_lane"]["tests"] == 10
    assert result["series"] == []


def test_suite_with_history_but_an_empty_window_keeps_an_honest_zero(var_root: Path) -> None:
    write_receipt(var_root, "old.json", receipt(5, 0, days_ago=200), age_days=200)
    write_junit(
        var_root / "test-reports" / "fast-lane-latest.xml",
        tests=10, failures=0, errors=0, skipped=0,
    )
    result = read_suite(30)
    assert result["overview"]["runs_7d"] == 0  # measured: the store has runs, none recent
    assert result["overview"]["pass_rate_7d"] is None


def test_snapshot_is_safe_under_concurrent_eviction(var_root: Path) -> None:
    """get + move_to_end unlocked was a KeyError (a 500) waiting for an evictor."""
    import threading

    from omniagentos.testobs import readers

    write_receipt(var_root, "aaa.json", receipt(5, 0))
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for i in range(200):
                snapshot("suite", (i % 40) + 1)
        except BaseException as exc:  # noqa: BLE001 — the point is that none escapes
            errors.append(exc)

    workers = [threading.Thread(target=hammer) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert not errors
    assert len(readers._cache) <= readers._CACHE_MAXSIZE


# --------------------------------------------------------------- round-3 regressions


def test_northstar_run_order_survives_mixed_offsets_inside_one_run(var_root: Path) -> None:
    """Per-run max must be parsed too: a lexical MAX inside the GROUP BY is
    exactly as wrong as one outside it."""
    seed_northstar(var_root, run_id="run-actual-latest", created_at="2026-08-13T20:00:00-05:00")
    # Same run, lexically larger stamp, 30 minutes OLDER in real time.
    add_eval_row(var_root, run_id="run-actual-latest", created_at="2026-08-14T00:30:00+00:00",
                 check_id="NSC-A2")
    # A different run, newer than the stamp a lexical per-run max would keep.
    seed_northstar(var_root, run_id="run-older", created_at="2026-08-14T00:45:00+00:00")

    assert read_northstar(36500)["overview"]["run_id"] == "run-actual-latest"


def test_diagnostics_ignores_a_future_dated_row(var_root: Path) -> None:
    """A record of a run that has not happened is not evidence of freshness."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("real", "tier1", passed=1))
    append_ledger(var_root, now + timedelta(days=2), fh_row("phantom", "tier1", passed=1))

    result = read_diagnostics(14)
    assert result["overview"]["features_total"] == 1
    assert datetime.fromisoformat(result["overview"]["last_run_ts"]) <= now + timedelta(minutes=1)
    assert result["skipped_rows"] == 1
    assert all(w["id"] != "phantom/tier1" for w in result["weakspots"])


def test_diagnostics_ignores_a_future_dated_shard(var_root: Path) -> None:
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("real", "tier1", passed=1))
    append_ledger(var_root, datetime(2099, 1, 15, tzinfo=UTC), fh_row("phantom", "tier1", passed=1))

    assert read_diagnostics(14)["overview"]["features_total"] == 1


def test_diagnostics_skips_a_calendar_invalid_shard_name(var_root: Path) -> None:
    """``ledger-202613.jsonl`` matches the pattern but is not a month."""
    append_ledger(var_root, datetime.now(UTC), fh_row("real", "tier1", passed=1))
    (var_root / "feature-health" / "ledger-202613.jsonl").write_text("", encoding="utf-8")

    result = read_diagnostics(14)
    assert result["available"] is True
    assert result["skipped_rows"] == 1


def test_diagnostics_shows_a_cell_whose_only_records_are_aborts(var_root: Path) -> None:
    """An abort-only cell used to vanish: no cell, no staleness, no weakspot."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("healthy", "tier1", passed=1,
                                        report_path="run-tier1.xml"))
    append_ledger(var_root, now, fh_abort("abort-only", "tier3", report_path="run-tier3.xml"))

    result = read_diagnostics(14)
    assert result["overview"]["features_total"] == 2
    assert result["overview"]["aborted_recent"] == 1
    assert result["overview"]["features_failing"] == 0  # not run is not failed
    weak = {w["id"]: w for w in result["weakspots"]}
    assert weak["abort-only/tier3"]["status"] == "ABORT"
    assert weak["abort-only/tier3"]["detail"] == "did not run (load-guard)"


def test_diagnostics_a_completed_run_closes_an_earlier_abort(var_root: Path) -> None:
    """An abort is a gap in coverage; a later completed run closes it."""
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2),
                  fh_abort("memory", "tier1", report_path="20260814T050000Z-tier1.xml"))
    append_ledger(var_root, now - timedelta(hours=1),
                  fh_row("memory", "tier1", passed=10, report_path="20260814T060000Z-tier1.xml"))

    result = read_diagnostics(14)
    assert result["weakspots"] == []
    assert result["overview"]["aborted_recent"] == 1  # still counted as an abort


def test_diagnostics_an_abort_after_a_pass_is_the_current_state(var_root: Path) -> None:
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2),
                  fh_row("memory", "tier1", passed=10, report_path="20260814T050000Z-tier1.xml"))
    append_ledger(var_root, now - timedelta(hours=1),
                  fh_abort("memory", "tier1", report_path="20260814T060000Z-tier1.xml"))

    result = read_diagnostics(14)
    assert [(w["id"], w["status"]) for w in result["weakspots"]] == [("memory/tier1", "ABORT")]


def test_diagnostics_surfaces_miss_and_empty_verdicts(var_root: Path) -> None:
    """MISS and EMPTY are canonical fh verdicts; hiding them is favourable absence."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_row("healthy", "tier1", passed=1,
                                        report_path="run-healthy.xml"))
    append_ledger(var_root, now, fh_row("incomplete", "tier1", passed=1,
                                        report_path="run-incomplete.xml",
                                        missing_paths=["tests/declared_but_absent.py"]))
    append_ledger(var_root, now, fh_row("only-skipped", "tier1", passed=0, skipped=5,
                                        report_path="run-skipped.xml"))

    weak = {w["id"]: w for w in read_diagnostics(14)["weakspots"]}
    assert weak["incomplete/tier1"]["status"] == "MISS"
    assert weak["only-skipped/tier1"]["status"] == "EMPTY"
    assert "healthy/tier1" not in weak
    # MISS reports how many declared paths were absent, never which.
    assert "declared_but_absent" not in weak["incomplete/tier1"]["detail"]


def test_suite_history_counts_a_receipt_its_own_timestamp_excluded(var_root: Path) -> None:
    """A receipt copied today but recording an old run still proves history."""
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    write_receipt(var_root, "copied-today.json", receipt(5, 0, started_at=old))
    write_junit(
        var_root / "test-reports" / "fast-lane-latest.xml",
        tests=10, failures=0, errors=0, skipped=0,
    )
    overview = read_suite(30)["overview"]
    assert overview["runs_7d"] == 0  # measured: the store has receipts, none recent
    assert overview["pass_rate_7d"] is None


def test_suite_history_probe_rejects_an_incomplete_old_receipt(var_root: Path) -> None:
    """The out-of-window probe validates counts exactly like the windowed scan."""
    write_receipt(var_root, "corrupt-old.json", {"checks_collected": 5}, age_days=60)
    write_junit(
        var_root / "test-reports" / "fast-lane-latest.xml",
        tests=10, failures=0, errors=0, skipped=0,
    )
    assert read_suite(30)["overview"]["runs_7d"] is None


def test_snapshot_keeps_the_newer_result_when_a_slow_scan_finishes_late(
    var_root: Path,
) -> None:
    """A scan that STARTED earlier must never overwrite a newer cached entry."""
    import threading

    from omniagentos.testobs import readers

    def result(marker: str) -> dict[str, object]:
        return {"available": True, "reason": None, "overview": {"marker": marker},
                "series": [], "weakspots": [], "skipped_rows": 0}

    inside = threading.Event()
    release = threading.Event()
    calls = []
    lock = threading.Lock()

    def controlled(_days: int) -> dict[str, object]:
        with lock:
            calls.append(1)
            first = len(calls) == 1
        if first:
            inside.set()
            assert release.wait(timeout=5)
            return result("older-scan")
        return result("newer-scan")

    original = readers.READERS["suite"]
    readers.READERS["suite"] = controlled
    try:
        slow = threading.Thread(target=lambda: snapshot("suite", 30))
        slow.start()
        assert inside.wait(timeout=5)
        assert snapshot("suite", 30)["overview"]["marker"] == "newer-scan"
        release.set()
        slow.join(timeout=5)
        assert not slow.is_alive()
        assert snapshot("suite", 30)["overview"]["marker"] == "newer-scan"
    finally:
        release.set()
        readers.READERS["suite"] = original


def test_weakspot_rank_orders_the_whole_status_vocabulary(var_root: Path) -> None:
    from omniagentos.testobs import WEAKSPOT_STATUSES

    items = [
        {"category": "diagnostics", "status": "STALE", "gate": None, "id": "i"},
        {"category": "diagnostics", "status": "EMPTY", "gate": None, "id": "h"},
        {"category": "diagnostics", "status": "MISS", "gate": None, "id": "g"},
        {"category": "diagnostics", "status": "ABORT", "gate": None, "id": "f"},
        {"category": "northstar", "status": "NOT_EVALUABLE", "gate": False, "id": "e"},
        {"category": "northstar", "status": "VOID", "gate": False, "id": "d"},
        {"category": "diagnostics", "status": "ERR", "gate": None, "id": "c"},
        {"category": "diagnostics", "status": "FAIL", "gate": None, "id": "b"},
        {"category": "northstar", "status": "FAIL", "gate": True, "id": "a"},
    ]
    ordered = [i["id"] for i in sorted(items, key=weakspot_rank)]
    assert ordered == ["a", "b", "c", "d", "e", "f", "g", "h", "i"]
    assert {i["status"] for i in items} <= set(WEAKSPOT_STATUSES)


# --------------------------------------------------------------- round-5 regression


def test_diagnostics_a_sibling_pass_cannot_clear_an_instrument_error(var_root: Path) -> None:
    """did_not_run WITHOUT aborted is ERR (fh.py:399/660), not a supersedable ABORT.

    Folding it into ABORT let a green Playwright append clear a live-probe stream
    that observed nothing — the sibling-masking bug, one class deeper.
    """
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2), fh_did_not_run(
        "api_ui", "tier3", report_path="20260814T050000Z-liveprobe.xml"))
    append_ledger(var_root, now - timedelta(hours=1), fh_row(
        "api_ui", "tier3", passed=20, report_path="20260814T060000Z-playwright.xml"))

    result = read_diagnostics(14)
    assert [(w["id"], w["status"]) for w in result["weakspots"]] == [("api_ui/tier3", "ERR")]
    assert result["weakspots"][0]["detail"] == "instrument error: the run observed no tests"


def test_diagnostics_a_later_run_of_the_same_stream_clears_an_instrument_error(
    var_root: Path,
) -> None:
    """Normal latest-per-stream rules: the SAME stream running clean supersedes."""
    now = datetime.now(UTC)
    append_ledger(var_root, now - timedelta(hours=2), fh_did_not_run(
        "api_ui", "tier3", report_path="20260814T050000Z-liveprobe.xml"))
    append_ledger(var_root, now - timedelta(hours=1), fh_row(
        "api_ui", "tier3", passed=20, report_path="20260814T060000Z-liveprobe.xml"))

    assert read_diagnostics(14)["weakspots"] == []


def test_diagnostics_a_window_of_only_instrument_errors_is_measured(var_root: Path) -> None:
    """F014: an ERR cell's visibility must not depend on some OTHER cell passing.

    A did_not_run-only record is a run that STARTED and errored, so the window is
    measured — degraded, but measured. Gating it behind an ordinary completion
    made the instrument error appear only when an unrelated feature happened to
    pass in the same window.
    """
    append_ledger(var_root, datetime.now(UTC), fh_did_not_run(
        "api_ui", "tier3", report_path="20260814T050000Z-liveprobe.xml"))

    result = read_diagnostics(14)
    assert result["available"] is True
    assert [(w["id"], w["status"]) for w in result["weakspots"]] == [("api_ui/tier3", "ERR")]
    assert result["overview"]["last_run_ts"] is not None


def test_diagnostics_an_unrelated_pass_does_not_change_an_error_cell(var_root: Path) -> None:
    """The same ERR, with and without an unrelated completion, reads the same."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_did_not_run(
        "api_ui", "tier3", report_path="20260814T050000Z-liveprobe.xml"))
    alone = read_diagnostics(14)

    append_ledger(var_root, now, fh_row("unrelated", "tier1", passed=1,
                                        report_path="20260814T060000Z-unrelated.xml"))
    clear_cache()
    together = read_diagnostics(14)

    assert [(w["id"], w["status"]) for w in alone["weakspots"]] == [("api_ui/tier3", "ERR")]
    assert [(w["id"], w["status"]) for w in together["weakspots"]] == [("api_ui/tier3", "ERR")]


def test_diagnostics_mixed_aborts_and_instrument_errors_stay_measured(var_root: Path) -> None:
    """A true abort proves nothing; the instrument error beside it still counts."""
    now = datetime.now(UTC)
    append_ledger(var_root, now, fh_abort("lane_thing", "tier1", report_path="run-tier1.xml"))
    append_ledger(var_root, now, fh_did_not_run(
        "api_ui", "tier3", report_path="20260814T050000Z-liveprobe.xml"))

    result = read_diagnostics(14)
    assert result["available"] is True
    assert result["overview"]["aborted_recent"] == 2  # both are runs that did not run
    weak = {w["id"]: w["status"] for w in result["weakspots"]}
    assert weak["api_ui/tier3"] == "ERR"
    assert weak["lane_thing/tier1"] == "ABORT"
