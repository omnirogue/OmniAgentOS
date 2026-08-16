"""PulseStore: upsert/series/latest/metrics round-trips and seed-on-empty behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omniagentos.pulse.store import PulseStore


def test_upsert_then_series_returns_ordered_points(pulse: PulseStore) -> None:
    today = datetime.now(UTC).date()
    dates = [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    pulse.upsert("skills.total", dates[2], 5.0)
    pulse.upsert("skills.total", dates[0], 3.0)
    pulse.upsert("skills.total", dates[1], 4.0)

    points = pulse.series("skills.total", days=3)
    assert [p["date"] for p in points] == dates
    assert [p["value"] for p in points] == [3.0, 4.0, 5.0]


def test_upsert_replaces_same_date_value(pulse: PulseStore) -> None:
    today_iso = datetime.now(UTC).date().isoformat()
    pulse.upsert("skills.total", today_iso, 1.0)
    pulse.upsert("skills.total", today_iso, 42.0)

    points = pulse.series("skills.total", days=1)
    assert len(points) == 1
    assert points[0]["value"] == 42.0


def test_upsert_many_inserts_in_one_txn(pulse: PulseStore) -> None:
    pulse.upsert_many([
        ("skills.total", "2026-01-01", 1.0),
        ("skills.versions", "2026-01-01", 2.0),
        ("loops.fires", "2026-01-01", 3.0),
    ])
    assert pulse.metrics() == ["loops.fires", "skills.total", "skills.versions"]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_upsert_rejects_nonfinite_measurements(
    pulse: PulseStore,
    value: float,
) -> None:
    """A non-finite value cannot be persisted as a successful measurement."""
    today_iso = datetime.now(UTC).date().isoformat()

    with pytest.raises(ValueError, match="finite"):
        pulse.upsert("reliability.score", today_iso, value)

    assert pulse.latest("reliability.score") is None


def test_upsert_many_rejects_nonfinite_batch_atomically(pulse: PulseStore) -> None:
    """Rejecting a bad value cannot partially persist its valid neighbour."""
    today_iso = datetime.now(UTC).date().isoformat()

    with pytest.raises(ValueError, match="finite"):
        pulse.upsert_many(
            [
                ("reliability.score", today_iso, 0.5),
                ("loops.acceptance", today_iso, float("inf")),
            ]
        )

    assert pulse.latest("reliability.score") is None
    assert pulse.latest("loops.acceptance") is None

    # Counterfeit guard: finite measurements remain writable.
    pulse.upsert("reliability.score", today_iso, 0.5)
    assert pulse.latest("reliability.score") == {
        "date": today_iso,
        "value": 0.5,
    }


def test_series_respects_days_window(pulse: PulseStore) -> None:
    today = datetime.now(UTC).date()
    thirty_days_ago = (today - timedelta(days=30)).isoformat()
    today_iso = today.isoformat()
    pulse.upsert("skills.total", thirty_days_ago, 1.0)
    pulse.upsert("skills.total", today_iso, 31.0)

    assert len(pulse.series("skills.total", days=31)) == 2
    assert pulse.series("skills.total", days=1) == [{"date": today_iso, "value": 31.0}]


def test_series_days_is_calendar_window_not_stale_point_limit(pulse: PulseStore) -> None:
    """A stale favourable point is not evidence for the requested window.

    Counterfeit guard: the current point must still be returned, so an
    implementation that always returns an empty series cannot pass.
    """
    today = datetime.now(UTC).date()
    stale_date = (today - timedelta(days=365)).isoformat()
    today_iso = today.isoformat()
    pulse.upsert("reliability.score", stale_date, 1.0)

    assert pulse.series("reliability.score", days=30) == []

    pulse.upsert("reliability.score", today_iso, 0.5)
    assert pulse.series("reliability.score", days=30) == [{"date": today_iso, "value": 0.5}]


@pytest.mark.parametrize("bad_date", ["9999-12-31", "not-a-date", "2026-13-40"])
def test_upsert_rejects_invalid_or_future_dates(
    pulse: PulseStore,
    bad_date: str,
) -> None:
    """A successful write must be a real, non-future calendar observation."""
    with pytest.raises(ValueError, match="Pulse date"):
        pulse.upsert("reliability.score", bad_date, 1.0)

    assert pulse.has_any() is False
    assert pulse.latest("reliability.score") is None
    assert pulse.series("reliability.score", days=30) == []
    assert pulse.metrics() == []


def test_upsert_many_rejects_invalid_date_atomically(pulse: PulseStore) -> None:
    """A bad date in a batch cannot commit its valid neighbour."""
    today_iso = datetime.now(UTC).date().isoformat()

    with pytest.raises(ValueError, match="Pulse date"):
        pulse.upsert_many(
            [
                ("reliability.score", today_iso, 0.5),
                ("loops.acceptance", "not-a-date", 0.9),
            ]
        )

    assert pulse.latest("reliability.score") is None
    assert pulse.latest("loops.acceptance") is None

    # Counterfeit guard: valid dates remain writable after a rejected batch.
    pulse.upsert("reliability.score", today_iso, 0.5)
    assert pulse.latest("reliability.score") == {
        "date": today_iso,
        "value": 0.5,
    }


def test_legacy_corrupt_dates_are_hidden_on_read(pulse: PulseStore) -> None:
    """Rows that bypassed the write gate still cannot counterfeit availability.

    Counterfeit guard: a real in-window point remains visible, so an
    implementation that always returns empty cannot pass.
    """
    today = datetime.now(UTC).date()
    today_iso = today.isoformat()
    valid_date = (today - timedelta(days=1)).isoformat()
    conn = pulse._conn()
    conn.executemany(
        "INSERT INTO pulse_series (metric, date, value) VALUES (?, ?, ?)",
        [
            ("reliability.score", "9999-12-31", 1.0),
            ("reliability.score", "not-a-date", 1.0),
        ],
    )
    conn.commit()

    assert pulse.has_any() is False
    assert pulse.has_any("reliability.score") is False
    assert pulse.latest("reliability.score") is None
    assert pulse.series("reliability.score", days=30) == []
    assert pulse.metrics() == []

    pulse.upsert("reliability.score", valid_date, 0.5)
    pulse.upsert("reliability.score", today_iso, 0.25)
    assert pulse.has_any() is True
    assert pulse.has_any("reliability.score") is True
    assert pulse.latest("reliability.score") == {
        "date": today_iso,
        "value": 0.25,
    }
    assert pulse.series("reliability.score", days=30) == [
        {"date": valid_date, "value": 0.5},
        {"date": today_iso, "value": 0.25},
    ]
    assert pulse.metrics() == ["reliability.score"]


def test_series_empty_metric_returns_empty_list(pulse: PulseStore) -> None:
    assert pulse.series("skills.total", days=30) == []


def test_has_any_distinguishes_empty_from_seeded(pulse: PulseStore) -> None:
    assert pulse.has_any() is False
    pulse.upsert("skills.total", "2026-01-01", 1.0)
    assert pulse.has_any() is True
    assert pulse.has_any("skills.total") is True
    assert pulse.has_any("loops.fires") is False


def test_latest_returns_most_recent_or_none(pulse: PulseStore) -> None:
    assert pulse.latest("skills.total") is None
    pulse.upsert("skills.total", "2026-01-01", 1.0)
    pulse.upsert("skills.total", "2026-01-02", 5.0)
    latest = pulse.latest("skills.total")
    assert latest is not None
    assert latest["value"] == 5.0
    assert latest["date"] == "2026-01-02"


def test_metrics_lists_distinct_names(pulse: PulseStore) -> None:
    pulse.upsert("skills.total", "2026-01-01", 1.0)
    pulse.upsert("loops.fires", "2026-01-01", 0.0)
    pulse.upsert("skills.total", "2026-01-02", 2.0)
    assert pulse.metrics() == ["loops.fires", "skills.total"]
