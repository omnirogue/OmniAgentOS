"""Aggregator: compute_metrics / snapshot against seeded SQLite fixtures.

Each ``seed_*`` helper in conftest.py sets up ONE aspect of the schema so the
aggregator can be tested end-to-end without a live system running.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.pulse.aggregator import METRICS, compute_metrics, snapshot
from omniagentos.pulse.store import PulseStore
from tests.pulse.conftest import (
    seed_improvements,
    seed_reliability,
    seed_routine_runs,
    seed_skills,
)

# Rate metrics whose empty-denominator form must be None, never a flattering 0.0.
_RATE_METRICS = ("loops.acceptance", "reliability.score")


def test_compute_metrics_returns_canonical_keys(database: SqliteStore) -> None:
    values, errors = compute_metrics(database)
    assert set(values.keys()) == set(METRICS)
    # Real counts are zero on an empty store; rates with no denominator are
    # unknown (None), not a vacuous 0.0 / 1.0 claim.
    for metric in METRICS:
        if metric in _RATE_METRICS:
            assert values[metric] is None
        else:
            assert values[metric] == 0.0


def test_empty_reliability_does_not_persist_vacuous_score(
    database: SqliteStore,
) -> None:
    """Unknown health must not be rewritten as 0.0 on the way to storage.

    ``pulse_series.value`` is non-null, so the honest representation is
    *absence* of today's point — recoverable as unknown — not a coerced
    degraded score that claims total failure.
    """
    pulse = PulseStore(database)
    pulse.upsert("reliability.score", "2026-01-14", 1.0)
    pulse.upsert("reliability.score", "2026-01-15", 0.9)

    values = snapshot(database, date="2026-01-15")

    assert values["reliability.score"] is None
    # Same-day numeric claim cleared; prior day remains historical evidence.
    assert pulse.latest("reliability.score") == {
        "date": "2026-01-14",
        "value": 1.0,
    }
    assert json.loads(json.dumps(values))["reliability.score"] is None


def test_skills_total_counts_active_only(database: SqliteStore) -> None:
    seed_skills(database, n=3)
    # Insert one archived skill; it must NOT count.
    now = utc_now_iso()
    database._connection.execute(
        "INSERT INTO skills (id, slug, category, subcategory, title, summary, "
        "status, current_version, created_at, updated_at) "
        "VALUES ('sk_arch', 'skill-arch', 'cat', 'sub', 'Archived', '', "
        "'archived', 1, ?, ?)",
        (now, now),
    )
    database._connection.commit()

    values, errors = compute_metrics(database)
    assert values["skills.total"] == 3.0


def test_improvements_applied_counts_terminal_good_only(database: SqliteStore) -> None:
    seed_improvements(
        database,
        statuses=["applied", "monitoring", "confirmed", "rejected", "proposed"],
    )
    values, errors = compute_metrics(database)
    assert values["improvements.applied"] == 3.0


def test_loops_fires_counts_todays_runs(database: SqliteStore) -> None:
    seed_routine_runs(database, today_count=4, accepted=2)
    values, errors = compute_metrics(database)
    assert values["loops.fires"] == 4.0


def test_loops_acceptance_rate(database: SqliteStore) -> None:
    """Positive denominator still yields the correct computed rate.

    Guards a hardcoded-None implementation that would pass an empty-only test.
    """
    seed_routine_runs(database, today_count=4, accepted=3)
    values, errors = compute_metrics(database)
    # 3 accepted out of 4 settled = 0.75
    assert values["loops.acceptance"] == 0.75
    assert values["loops.acceptance"] is not None


def test_loops_acceptance_unknown_when_no_settled(database: SqliteStore) -> None:
    """Empty denominator is unknown — not 0.0 ('everything rejected')."""
    values, errors = compute_metrics(database)
    assert values["loops.acceptance"] is None


def test_loops_acceptance_unknown_when_outcome_is_invalid(
    database: SqliteStore,
) -> None:
    """A non-boolean outcome cannot become a confident numeric rate.

    Fail closed (raise) rather than soft-None: the HTTP seed path discards
    snapshot's return map and serves empty series for omitted metrics, which
    mixed tiles render as zero.
    """
    seed_routine_runs(database, today_count=1, accepted=1)
    database._connection.execute(
        "INSERT INTO routine_runs "
        "(routine_id, iteration, accepted, cost_usd, created_at) "
        "VALUES ('rtn_seed', 2, 2, 0.0, ?)",
        (utc_now_iso(),),
    )
    database._connection.commit()

    # Counterfeit guard: per-metric failure (not global).
    # One metric's error doesn't blank others; loops.acceptance should fail.
    values, errors = compute_metrics(database)
    assert errors["loops.acceptance"] is not None
    # Other metrics should still have values
    assert values["skills.total"] == 0.0
    assert values["loops.fires"] == 2.0  # Two runs inserted (one seeded, one manually with bad outcome)

    # Snapshot must handle per-metric failures gracefully.
    snapshot(database, date="2026-01-15")
    # Metrics with errors are not persisted; others are.
    assert PulseStore(database).has_any() is True


def test_loops_acceptance_none_survives_snapshot_round_trip(
    database: SqliteStore,
) -> None:
    """None must not be coerced back to 0.0 through snapshot/serialization.

    Counterfeit guard: assert IS None (not merely ``!= 0.0``), and separately
    that a positive denominator still produces the real rate.
    """
    # Empty → None through snapshot return + JSON.
    empty = snapshot(database, date="2026-01-15")
    assert empty["loops.acceptance"] is None
    assert json.loads(json.dumps(empty))["loops.acceptance"] is None
    pulse = PulseStore(database)
    # Non-null column: unknown is omitted, not rewritten as 0.0.
    assert pulse.latest("loops.acceptance") is None
    assert pulse.series("loops.acceptance", days=30) == []

    # Positive denominator → computed rate (blocks hardcoded None).
    seed_routine_runs(database, today_count=4, accepted=3)
    known = snapshot(database, date="2026-01-16")
    assert known["loops.acceptance"] == 0.75
    assert json.loads(json.dumps(known))["loops.acceptance"] == 0.75
    assert pulse.latest("loops.acceptance") == {
        "date": "2026-01-16",
        "value": 0.75,
    }


def test_reliability_score_degrades_with_critical(database: SqliteStore) -> None:
    # 1 critical + 3 others open → 1 - (1/4) = 0.75
    seed_reliability(database, open_critical=1, open_other=3)
    values, errors = compute_metrics(database)
    assert values["reliability.score"] == 0.75


def test_reliability_score_can_be_perfect_with_nonempty_evidence(
    database: SqliteStore,
) -> None:
    """A real healthy population prevents a hardcoded-unknown implementation."""
    seed_reliability(database, open_critical=0, open_other=4)

    values, errors = compute_metrics(database)

    assert values["reliability.score"] == 1.0


@pytest.mark.parametrize(
    "bad_ts",
    (
        "9999-12-31T00:00:00Z",
        "not-a-date",
        pytest.param(None, id="invalid-current-month-day"),
    ),
)
def test_recent_activity_rejects_future_and_unparseable_timestamps(
    database: SqliteStore,
    bad_ts: str | None,
) -> None:
    """Unreadable source timestamps are measurement failures, not soft zeros.

    Soft-None was rejected: the production seed path discards snapshot's map
    and serves empty series, which mixed Skills/Loops tiles render as 0.
    Fail closed so no companion metric is written either.
    """
    if bad_ts is None:
        # Keep this malformed date inside the SQL prefilter's current-month
        # range while still requiring Python to reject the impossible day.
        bad_ts = datetime.now(UTC).strftime("%Y-%m-32T00:00:00Z")

    seed_skills(database, n=1)
    now = utc_now_iso()
    for version, created_at in enumerate((now, bad_ts), start=1):
        database._connection.execute(
            "INSERT INTO skill_versions "
            "(id, skill_id, version, content_snapshot, change_reason, author, "
            "status, created_at) "
            "VALUES (?, 'sk_0', ?, '', '', 'test', 'active', ?)",
            (f"skv_{version}", version, created_at),
        )
    database._connection.commit()

    # Counterfeit guard: silently dropping the bad row would return 1.0.
    # Per-metric failure: bad timestamps in skill_versions fail that metric
    values, errors = compute_metrics(database)
    assert errors['skills.versions'] is not None  # Failed metric has error
    assert values['skills.versions'] is None  # No value for failed metric
    assert values['skills.total'] == 1.0  # Other metrics still work

    # snapshot handles per-metric failures gracefully
    snapshot(database, date="2026-01-15")
    # Failed metrics not persisted; succeeded ones are
    stored = PulseStore(database).latest("skills.total")
    assert stored is not None  # skills.total was persisted
    stored_versions = PulseStore(database).latest("skills.versions")
    assert stored_versions is None  # skills.versions not persisted due to error


    database._connection.execute(
        "DELETE FROM skill_versions WHERE created_at != ?",
        (now,),
    )
    database._connection.commit()

    # Hardcoded failure cannot fake the fix: once every skill_versions
    # timestamp is measurable again, the metric must produce a REAL number.
    values, errors = compute_metrics(database)
    assert errors["skills.versions"] is None
    assert values["skills.versions"] == 1.0

    seed_routine_runs(database, today_count=1, accepted=0)
    database._connection.execute(
        "INSERT INTO routine_runs "
        "(routine_id, iteration, accepted, cost_usd, created_at) "
        "VALUES ('rtn_seed', 2, 1, 0.0, ?)",
        (bad_ts,),
    )
    database._connection.commit()

    # Per-metric isolation in the other direction: a bad routine_runs row
    # fails loops.fires explicitly while skills.versions stays measurable.
    values, errors = compute_metrics(database)
    assert errors["loops.fires"] is not None
    assert values["loops.fires"] is None
    assert values["skills.versions"] == 1.0



def test_reliability_query_failure_is_logged_and_raised(
    database: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    database._connection.execute("DROP TABLE reliability_events")
    database._connection.commit()

    with caplog.at_level(logging.ERROR, logger="omniagentos.pulse.aggregator"):
        values, errors = compute_metrics(database)
    # Query failure for reliability.score marks it unavailable per-metric
    assert errors['reliability.score'] is not None
    assert values['reliability.score'] is None
    # Other metrics still compute
    assert values['skills.total'] == 0.0
    assert "Pulse metric query failed" in caplog.text
    assert "reliability_events" in caplog.text

    assert "Pulse metric query failed" in caplog.text
    assert "reliability_events" in caplog.text


def test_unavailable_database_cannot_report_health(
    database: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    database._connection.close()

    with caplog.at_level(logging.ERROR, logger="omniagentos.pulse.aggregator"):
        values, errors = compute_metrics(database)

    # When database is unavailable, all metrics fail per-metric
    assert all(err is not None for err in errors.values()), "All metrics should error when DB is unavailable"
    assert all(val is None for val in values.values()), "All metrics should have None values when DB is unavailable"
    assert "Pulse metric query failed" in caplog.text


def test_snapshot_writes_and_is_idempotent(database: SqliteStore) -> None:
    seed_skills(database, n=2)
    today_iso = datetime.now(UTC).date().isoformat()
    first = snapshot(database, date=today_iso)
    assert first["skills.total"] == 2.0

    pulse = PulseStore(database)
    points = pulse.series("skills.total", days=1)
    assert len(points) == 1
    assert points[0]["date"] == today_iso
    assert points[0]["value"] == 2.0

    # Re-seed with more skills (offset avoids PK collision); snapshot the
    # same date → value updates.
    seed_skills(database, n=2, offset=100)
    second = snapshot(database, date=today_iso)
    assert second["skills.total"] == 4.0
    points2 = pulse.series("skills.total", days=1)
    assert len(points2) == 1
    assert points2[0]["value"] == 4.0


def test_snapshot_missing_table_is_observable(database: SqliteStore, caplog: pytest.LogCaptureFixture) -> None:
    database._connection.execute("DROP TABLE metacog_memory_records")
    database._connection.commit()

    with caplog.at_level(logging.ERROR, logger="omniagentos.pulse.aggregator"):
        snapshot(database)

    # snapshot handles per-metric failures; doesn't raise
    # Failed metrics not persisted, succeeded ones are
    pulse = PulseStore(database)
    stored_improvements = pulse.latest("improvements.applied")
    assert stored_improvements is not None  # other metrics still work
    stored_memory = pulse.latest("memory.facts")
    assert stored_memory is None  # memory.facts table missing, metric failed
    assert "Pulse metric query failed" in caplog.text

def test_parse_utc_timestamp_accepts_sqlite_format() -> None:
    """Verify parser accepts SQLite CURRENT_TIMESTAMP format (space-separated, tz-naive).

    Migration 032:138 seeds this format; fresh installs must not have permanently
    broken skills.versions metrics due to timestamp parsing failures.
    """
    from omniagentos.pulse.aggregator import _parse_utc_timestamp

    # SQLite CURRENT_TIMESTAMP format: space-separated, tz-naive, treated as UTC
    sqlite_now = "2026-07-31 15:08:19"
    parsed = _parse_utc_timestamp(sqlite_now)
    assert parsed is not None, f"Parser must accept SQLite CURRENT_TIMESTAMP format: {sqlite_now}"
    assert parsed.tzinfo is not None, "Parser must add UTC tzinfo to tz-naive timestamps"
    assert parsed.year == 2026
    assert parsed.month == 7
    assert parsed.day == 31
    assert parsed.hour == 15

    # Canonical ISO format still works
    iso_now = "2026-07-31T15:08:19Z"
    parsed_iso = _parse_utc_timestamp(iso_now)
    assert parsed_iso is not None
    # Both should parse to approximately the same instant (allow for format differences)
    assert abs((parsed_iso - parsed).total_seconds()) < 1


def test_same_day_mixed_timestamp_forms_are_all_counted(database: SqliteStore) -> None:
    """Date-only SQL floor: T-form and space-form same-day rows BOTH count.

    A full-isoformat floor bound ("...T00:00:00+00:00") string-compares above
    SQLite's space-separated CURRENT_TIMESTAMP form (" " < "T"), silently
    dropping same-day space-form rows — an undercount published with
    error=None. The floor must bind a date-only prefix; Python filters exact
    instants afterward.
    """
    seed_routine_runs(database, today_count=1, accepted=0)
    now = datetime.now(UTC)
    t_form = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    space_form = now.strftime("%Y-%m-%d %H:%M:%S")
    for iteration, created_at in enumerate((t_form, space_form), start=2):
        database._connection.execute(
            "INSERT INTO routine_runs "
            "(routine_id, iteration, accepted, cost_usd, created_at) "
            "VALUES ('rtn_seed', ?, 1, 0.0, ?)",
            (iteration, created_at),
        )
    database._connection.commit()

    values, errors = compute_metrics(database)
    assert errors["loops.fires"] is None
    assert values["loops.fires"] == 3.0
