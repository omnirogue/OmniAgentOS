"""Carrier tests for the lane-1 census capability: backlog-drain-census.

This build is scoped to the CAPABILITY only (`pipeline/bridge/
backlog_drain_census.py` + this test file) — the NSC-C32-04 manifest rebind
and the `writers.yaml` witness registration are a separate, later lane that
sequences after the writers-registry lane and must not collide with a sibling
lane building those two shared config files concurrently. Every test here is
therefore SYNTHETIC and HERMETIC — a scratch `ledger.jsonl` built line-by-line
in `tmp_path` and a scratch sqlite database created with a bare `CREATE TABLE
board_tasks`, never the live loopqueue ledger or the live runtime database. A
carrier that read the real ledger for its exact-value assertions would
certify whatever happens to be in it on the day it runs — favourable
absence, the exact defect class this repo's reachability gate exists to
refuse. (An estate-scope carrier that inspects the LIVE ledger/board belongs
to the deferred manifest-wiring lane, not here — this build's mechanical
pass-list requires every test in this file to be green by construction, not
contingent on today's production backlog state.)

`test_terminal_set_released_counts_as_closed` and
`test_terminal_set_claim_expired_does_not_close` are the two pins the
2026-08-11 cross-lineage review (GPT-5.6-Sol, MAJOR, candidate
sha256:461364b6) named explicitly: `parked`/`claim_expired` must NOT close an
id (they are suspensions, not terminals — see `backlog_drain_census.py`'s
module docstring for the full correction), while `released` still must.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import backlog_drain_census as BDC  # noqa: E402

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _ts(hours_before_now: float) -> str:
    return (NOW - timedelta(hours=hours_before_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_ledger(path: Path, events: list[dict]) -> Path:
    ledger = path / "ledger.jsonl"
    with ledger.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return ledger


def _make_board_db(path: Path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    """rows: (id, status, priority, archived_at) with created_at derived below."""
    db_path = path / "board.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE board_tasks ("
            " id TEXT PRIMARY KEY, status TEXT, priority TEXT,"
            " archived_at TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO board_tasks (id, status, priority, archived_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# compute_loopqueue_drain — hermetic unit coverage
# ---------------------------------------------------------------------------


def test_three_admitted_ids_merged_rejected_and_open(tmp_path: Path) -> None:
    """id A: admitted 200h ago, merged 4h later  -> CLOSED, lag 4h
    id B: admitted 100h ago, rejected 1h later -> CLOSED, lag 1h
    id C: admitted 48h ago, no terminal event   -> OPEN, age 48h

    Exact assertions on open_count, open_age p95/max, and closed_lag p95/max
    — not just "the numbers exist" — because a census that only proves it
    imports is a vacuous carrier."""
    events = [
        {"ts": _ts(200), "event": "admitted", "id": "sha256:aaaa"},
        {"ts": _ts(196), "event": "merged", "id": "sha256:aaaa"},
        {"ts": _ts(100), "event": "admitted", "id": "sha256:bbbb"},
        {"ts": _ts(99), "event": "rejected", "id": "sha256:bbbb"},
        {"ts": _ts(48), "event": "admitted", "id": "sha256:cccc"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 3
    assert report["open_without_terminal"] == ["sha256:cccc"]
    assert report["open_age_hours"]["n"] == 1
    assert report["open_age_hours"]["max"] == pytest.approx(48.0)
    assert report["open_age_hours"]["p50"] == pytest.approx(48.0)
    assert report["closed_lag_hours"]["n"] == 2
    assert report["closed_lag_hours"]["max"] == pytest.approx(4.0)
    assert sorted(report["closed_lag_hours"].keys()) == ["max", "n", "p50", "p95"]
    assert report["terminal_set"] == sorted(BDC.BROAD_TERMINAL_EVENTS)
    assert report["malformed_ids"] == []
    assert report["corrupt_line_count"] == 0


def test_first_admitted_and_first_terminal_win_over_duplicates(tmp_path: Path) -> None:
    """A re-admission after unpark, or a duplicate terminal, must not move
    the id's original clock — first parseable occurrence of each kind wins."""
    events = [
        {"ts": _ts(50), "event": "admitted", "id": "sha256:dddd"},
        {"ts": _ts(30), "event": "admitted", "id": "sha256:dddd"},  # duplicate admit, ignored
        {"ts": _ts(10), "event": "merged", "id": "sha256:dddd"},
        {"ts": _ts(5), "event": "rejected", "id": "sha256:dddd"},  # duplicate terminal, ignored
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 1
    assert report["open_without_terminal"] == []
    # lag measured from the FIRST admitted (50h ago) to the FIRST terminal (10h ago) = 40h
    assert report["closed_lag_hours"]["max"] == pytest.approx(40.0)


def test_terminal_set_released_counts_as_closed(tmp_path: Path) -> None:
    """THE pin: an admitted id whose only terminal event is `released` must
    count as CLOSED, not open.

    `LedgerView`'s narrow terminal set (merged/completed/rejected/closed at
    pipeline/bridge/integration.py:510-516) does NOT include `released`, so a
    census built on that set would read this id as still open. This test
    fails against that narrow set and only that set: it constructs an id
    with no merged, no completed, no rejected event at all, so a
    narrow-set implementation has nothing else to close it on."""
    events = [
        {"ts": _ts(72), "event": "admitted", "id": "sha256:eeee"},
        {"ts": _ts(60), "event": "released", "id": "sha256:eeee"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["open_without_terminal"] == [], (
        "an id whose only terminal is 'released' must not appear as open — "
        "the broad terminal set must include it, unlike LedgerView's narrow one"
    )
    assert report["closed_lag_hours"]["n"] == 1
    assert report["closed_lag_hours"]["max"] == pytest.approx(12.0)


def test_terminal_set_claim_expired_does_not_close(tmp_path: Path) -> None:
    """THE other pin (2026-08-11 review, MAJOR): `parked` and `claim_expired`
    are SUSPENSIONS, not terminals, and must not close an id.

    `admitted -> parked -> unparked` must read as OPEN (the id never
    actually left "approved and pending"). `claim_expired -> admitted` for
    the SAME immutable id must not retroactively close the LATER admission —
    a claim expiry returns work to the open pool, it does not retire the id,
    and it is chronologically a claim-lifecycle event for whatever came
    before it, never an authority over an admission that happens after it.
    Removing `claim_expired` from `BROAD_TERMINAL_EVENTS` must fail this
    test — it is not enough for `released` alone to be pinned."""
    events = [
        {"ts": _ts(100), "event": "admitted", "id": "sha256:unparked"},
        {"ts": _ts(90), "event": "parked", "id": "sha256:unparked"},
        {"ts": _ts(80), "event": "unparked", "id": "sha256:unparked"},
        {"ts": _ts(70), "event": "claim_expired", "id": "sha256:later-admit"},
        {"ts": _ts(50), "event": "admitted", "id": "sha256:later-admit"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["open_without_terminal"] == [
        "sha256:later-admit",
        "sha256:unparked",
    ], (
        "parked is a suspension and claim_expired returns a claim to open; "
        f"the census incorrectly reported {report['open_without_terminal']!r}"
    )
    assert report["open_age_hours"]["n"] == 2
    assert report["malformed_ids"] == []


def test_terminal_before_admission_is_malformed_not_a_zero_lag_close(
    tmp_path: Path,
) -> None:
    """A terminal event recorded chronologically BEFORE its own admission is
    corrupt or reordered evidence — it must be surfaced in `malformed_ids`,
    never silently clamped into a fabricated zero-lag "close"."""
    events = [
        {"ts": _ts(20), "event": "merged", "id": "sha256:reordered"},
        {"ts": _ts(10), "event": "admitted", "id": "sha256:reordered"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["malformed_ids"] == ["sha256:reordered"]
    assert report["open_without_terminal"] == []
    assert report["closed_lag_hours"]["n"] == 0


def test_malformed_first_admission_clock_is_repaired_by_a_later_valid_one(
    tmp_path: Path,
) -> None:
    """The FIRST admitted occurrence for an id may fail to parse; a LATER
    admitted occurrence for the SAME id must still be able to resolve the
    clock — the id's identity is not locked away from ever being dated just
    because its first record was unparseable."""
    events = [
        {"ts": "not-a-timestamp", "event": "admitted", "id": "sha256:malformed-clock"},
        {"ts": _ts(100), "event": "admitted", "id": "sha256:malformed-clock"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["open_without_terminal"] == ["sha256:malformed-clock"]
    assert report["open_age_hours"]["n"] == 1
    assert report["open_age_hours"]["max"] == pytest.approx(100.0)
    assert report["malformed_ids"] == []


def test_admission_with_no_parseable_clock_is_malformed(tmp_path: Path) -> None:
    """An id whose ONLY admitted record(s) never parse must be named in
    `malformed_ids`, not silently excluded from every stat with no trace."""
    events = [
        {"ts": "not-a-timestamp", "event": "admitted", "id": "sha256:unclocked"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 1
    assert report["open_without_terminal"] == []
    assert report["malformed_ids"] == ["sha256:unclocked"]


def test_corrupt_ledger_line_is_counted_not_silently_dropped(tmp_path: Path) -> None:
    """A torn/undecodable line must increment `corrupt_line_count` — the
    module's parse_events-based decode has a status channel and this census
    must use it, unlike a plain iter_events-only read."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"ts":"2026-08-01T00:00:00Z","event":"admitted","id":"sha256:lost"\n',
        encoding="utf-8",
    )

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["corrupt_line_count"] == 1
    assert report["admitted_count"] == 0


def test_terminal_without_prior_admission_excluded_from_both_stats(tmp_path: Path) -> None:
    """A terminal event for an id with no admitted record in this ledger
    window contributes to neither open_without_terminal nor closed_lag_hours
    — there is no admission clock to measure FROM, and inventing one would
    manufacture an unmeasured observation."""
    events = [
        {"ts": _ts(10), "event": "merged", "id": "sha256:ffff"},
        {"ts": _ts(5), "event": "admitted", "id": "sha256:gggg"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 1
    assert report["open_without_terminal"] == ["sha256:gggg"]
    assert report["closed_lag_hours"]["n"] == 0
    assert report["malformed_ids"] == []


def test_non_terminal_and_non_admitted_events_are_ignored(tmp_path: Path) -> None:
    """`observed`, `claimed`, `gated` etc. must not perturb the census —
    only `admitted` and the BROAD_TERMINAL_EVENTS set participate."""
    events = [
        {"ts": _ts(20), "event": "observed", "id": "sha256:hhhh"},
        {"ts": _ts(15), "event": "admitted", "id": "sha256:hhhh"},
        {"ts": _ts(10), "event": "gated", "id": "sha256:hhhh"},
        {"ts": _ts(5), "event": "completed", "id": "sha256:hhhh"},
    ]
    ledger = _write_ledger(tmp_path, events)

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 1
    assert report["open_without_terminal"] == []
    assert report["closed_lag_hours"]["max"] == pytest.approx(10.0)


def test_empty_ledger_reports_zero_observations(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, [])

    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    assert report["admitted_count"] == 0
    assert report["open_without_terminal"] == []
    assert report["open_age_hours"] == {"p50": None, "p95": None, "max": None, "n": 0}
    assert report["closed_lag_hours"] == {"p50": None, "p95": None, "max": None, "n": 0}
    assert report["malformed_ids"] == []
    assert report["corrupt_line_count"] == 0


# ---------------------------------------------------------------------------
# compute_board_urgent_open
# ---------------------------------------------------------------------------


def test_board_urgent_open_counts_and_ages_only_matching_rows(tmp_path: Path) -> None:
    """2 urgent+open aged rows, 1 done, 1 archived — only the 2 count."""
    rows = [
        ("t-1", "open", "urgent", None, _ts(128)),
        ("t-2", "open", "urgent", None, _ts(4)),
        ("t-3", "done", "urgent", None, _ts(200)),  # wrong status
        ("t-4", "open", "urgent", _ts(1), _ts(300)),  # archived
        ("t-5", "open", "normal", None, _ts(50)),  # wrong priority
    ]
    db_path = _make_board_db(tmp_path, rows)

    report = BDC.compute_board_urgent_open(db_path, now=NOW)

    assert report["count"] == 2
    assert report["age_hours"]["n"] == 2
    assert report["age_hours"]["max"] == pytest.approx(128.0)
    # nearest-rank p50 of [4.0, 128.0] (n=2, index ceil(0.5*2)-1=0) is the
    # smaller observed value, not an interpolated midpoint.
    assert report["age_hours"]["p50"] == pytest.approx(4.0)


def test_board_urgent_open_zero_rows(tmp_path: Path) -> None:
    db_path = _make_board_db(tmp_path, [])

    report = BDC.compute_board_urgent_open(db_path, now=NOW)

    assert report == {"count": 0, "age_hours": {"p50": None, "p95": None, "max": None, "n": 0}}


# ---------------------------------------------------------------------------
# evaluate_drain_bounds — the S3-O03 PASS clause, plus evidence-integrity
# ---------------------------------------------------------------------------


def _report_with_open_ages(tmp_path: Path, *hours: float) -> dict:
    """Build a real `compute_loopqueue_drain` report with N ids admitted
    `hours[i]` ago and never terminated — through the public API, not by
    hand-assembling the internal stats shape, so these bound tests exercise
    the same code path the primary carrier does."""
    events = [
        {"ts": _ts(h), "event": "admitted", "id": f"sha256:{index:04d}"}
        for index, h in enumerate(hours)
    ]
    ledger = _write_ledger(tmp_path, events)
    return BDC.compute_loopqueue_drain(ledger, now=NOW)


def test_bounds_pass_when_p95_within_bound_and_count_flat(tmp_path: Path) -> None:
    report = _report_with_open_ages(tmp_path, 10.0, 12.0)
    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=2
    )
    assert verdict == "PASS"
    assert reasons == []


def test_bounds_fail_when_p95_exceeds_bound(tmp_path: Path) -> None:
    report = _report_with_open_ages(tmp_path, 10.0, 200.0)
    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=2
    )
    assert verdict == "FAIL"
    assert any("open_age_p95" in reason for reason in reasons)


def test_bounds_fail_when_open_count_rises_even_if_p95_is_fine(tmp_path: Path) -> None:
    """The flat-or-down clause is load-bearing on its own: a synthetic report
    with a rising open count must FAIL with a NAMED reason even though p95 is
    comfortably within bound — dropping this clause (or defaulting to
    unconditional PASS) is exactly the regression this test exists to catch."""
    report = _report_with_open_ages(tmp_path, 1.0, 1.0, 1.0)  # p95 trivially fine
    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=1
    )
    assert verdict == "FAIL"
    assert any("open_count" in reason for reason in reasons)


def test_bounds_zero_open_ids_trivially_passes_p95_clause(tmp_path: Path) -> None:
    report = _report_with_open_ages(tmp_path)
    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=0.001, prior_open_count=0
    )
    assert verdict == "PASS"
    assert reasons == []


def test_bounds_no_prior_skips_the_flat_or_down_clause(tmp_path: Path) -> None:
    """`prior_open_count=None` evaluates the p95/tail bounds only — a first
    run with no history to compare against must not FAIL on a clause it
    cannot measure."""
    report = _report_with_open_ages(tmp_path, *([1.0] * 10))
    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=None
    )
    assert verdict == "PASS"
    assert reasons == []


def test_bounds_fail_on_single_stuck_tail_item_even_with_flat_p95_and_count(
    tmp_path: Path,
) -> None:
    """THE tail-bound pin (2026-08-11 review, MAJOR): one item stuck for
    1000h among 20 fresh 1h items gives p95=1h and a flat open count — the
    bound must still FAIL on the max/tail clause, or an indefinitely-rotting
    single item is structurally invisible."""
    report = _report_with_open_ages(tmp_path, *([1.0] * 20 + [1_000.0]))
    assert report["open_age_hours"]["p95"] == pytest.approx(1.0)
    assert report["open_age_hours"]["max"] == pytest.approx(1_000.0)

    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=21
    )
    assert verdict == "FAIL", (
        "one item rotted for 1000h while p95=1h and count is flat, but "
        f"evaluate_drain_bounds returned {verdict} with {reasons!r}"
    )
    assert any("open_age_max" in reason for reason in reasons)


def test_bounds_explicit_max_open_hours_max_overrides_the_p95_default(
    tmp_path: Path,
) -> None:
    """A caller may name a looser (or tighter) tail bound explicitly rather
    than inheriting the p95 bound as the tail ceiling."""
    report = _report_with_open_ages(tmp_path, *([10.0] * 19 + [500.0]))
    verdict, reasons = BDC.evaluate_drain_bounds(
        report,
        p95_open_hours_max=24.0,
        max_open_hours_max=1_000.0,
        prior_open_count=20,
    )
    assert verdict == "PASS"
    assert reasons == []


def test_bounds_missing_report_fields_fail_loudly_not_pass(tmp_path: Path) -> None:
    """THE favourable-absence pin (2026-08-11 review, MAJOR): an entirely
    malformed/missing report (`{}`) must FAIL with a named reason, never
    default to a healthy empty-observation PASS."""
    verdict, reasons = BDC.evaluate_drain_bounds(
        {}, p95_open_hours_max=24.0, prior_open_count=None
    )
    assert verdict == "FAIL"
    assert any("malformed report" in reason for reason in reasons)


def test_bounds_corrupt_line_count_fails_even_with_zero_open_ids(
    tmp_path: Path,
) -> None:
    """A corrupt ledger line must not be byte-equivalent to an empty, healthy
    ledger — `corrupt_line_count > 0` fails on its own, independent of
    whatever (possibly zero) open ids were recovered around the corruption."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"ts":"2026-08-01T00:00:00Z","event":"admitted","id":"sha256:lost"\n',
        encoding="utf-8",
    )
    report = BDC.compute_loopqueue_drain(ledger, now=NOW)

    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=None
    )
    assert verdict == "FAIL"
    assert any("ledger corruption" in reason for reason in reasons)


def test_bounds_malformed_ids_fail_even_when_bound_numbers_look_healthy(
    tmp_path: Path,
) -> None:
    """A report with unresolved (`malformed_ids`) evidence must FAIL even
    when the numeric bounds it DID manage to compute look fine."""
    events = [
        {"ts": _ts(20), "event": "merged", "id": "sha256:reordered"},
        {"ts": _ts(10), "event": "admitted", "id": "sha256:reordered"},
    ]
    ledger = _write_ledger(tmp_path, events)
    report = BDC.compute_loopqueue_drain(ledger, now=NOW)
    assert report["malformed_ids"] == ["sha256:reordered"]

    verdict, reasons = BDC.evaluate_drain_bounds(
        report, p95_open_hours_max=24.0, prior_open_count=None
    )
    assert verdict == "FAIL"
    assert any("malformed_ids" in reason or "unresolved evidence" in reason for reason in reasons)
