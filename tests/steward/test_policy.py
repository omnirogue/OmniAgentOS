from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omniagentos.steward.policy import expired_suggestion_ids, stale_alert_ids


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def test_stale_alert_ids_flags_open_alerts_idle_past_the_threshold() -> None:
    alerts = [
        {
            "id": 1,
            "state": "open",
            "created_at": _iso(NOW - timedelta(days=30)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=20))}},
        },
        {
            "id": 2,
            "state": "open",
            "created_at": _iso(NOW - timedelta(days=5)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=5))}},
        },
    ]
    assert stale_alert_ids(alerts, now=NOW) == [1]


def test_stale_alert_ids_ignores_non_open_states() -> None:
    alerts = [
        {
            "id": 1,
            "state": "resolved",
            "created_at": _iso(NOW - timedelta(days=30)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=30))}},
        },
        {
            "id": 2,
            "state": "acked",
            "created_at": _iso(NOW - timedelta(days=30)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=30))}},
        },
    ]
    assert stale_alert_ids(alerts, now=NOW) == []


def test_stale_alert_ids_falls_back_to_created_at_without_case_metadata() -> None:
    # A row that predates case-identity metadata (no evidence["_case"]) still
    # ages out, using created_at instead of guessing.
    alerts = [
        {"id": 7, "state": "open", "created_at": _iso(NOW - timedelta(days=15)), "evidence": {}}
    ]
    assert stale_alert_ids(alerts, now=NOW) == [7]


def test_stale_alert_ids_boundary_is_strictly_greater_than_threshold() -> None:
    exactly_14 = [
        {
            "id": 1,
            "state": "open",
            "created_at": _iso(NOW - timedelta(days=14)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=14))}},
        }
    ]
    assert stale_alert_ids(exactly_14, now=NOW) == []
    just_over_14 = [
        {
            "id": 2,
            "state": "open",
            "created_at": _iso(NOW - timedelta(days=14, seconds=1)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=14, seconds=1))}},
        }
    ]
    assert stale_alert_ids(just_over_14, now=NOW) == [2]


def test_stale_alert_ids_respects_custom_threshold() -> None:
    alerts = [
        {
            "id": 3,
            "state": "open",
            "created_at": _iso(NOW - timedelta(days=8)),
            "evidence": {"_case": {"last_seen": _iso(NOW - timedelta(days=8))}},
        }
    ]
    assert stale_alert_ids(alerts, now=NOW, stale_days=7) == [3]
    assert stale_alert_ids(alerts, now=NOW, stale_days=30) == []


def test_stale_alert_ids_ignores_unparsable_timestamps() -> None:
    alerts = [
        {"id": 4, "state": "open", "created_at": "not-a-date", "evidence": {}},
        {"id": 5, "state": "open", "created_at": None, "evidence": {}},
    ]
    assert stale_alert_ids(alerts, now=NOW) == []


def test_expired_suggestion_ids_flags_open_suggestions_past_the_threshold() -> None:
    suggestions = [
        {"id": "a", "state": "open", "created_at": _iso(NOW - timedelta(days=15))},
        {"id": "b", "state": "open", "created_at": _iso(NOW - timedelta(days=2))},
    ]
    assert expired_suggestion_ids(suggestions, now=NOW) == ["a"]


def test_expired_suggestion_ids_ignores_claimed_and_decided_states() -> None:
    suggestions = [
        {"id": "a", "state": "approving", "created_at": _iso(NOW - timedelta(days=30))},
        {"id": "b", "state": "approved", "created_at": _iso(NOW - timedelta(days=30))},
        {"id": "c", "state": "rejected", "created_at": _iso(NOW - timedelta(days=30))},
        {"id": "d", "state": "dismissed", "created_at": _iso(NOW - timedelta(days=30))},
    ]
    assert expired_suggestion_ids(suggestions, now=NOW) == []


def test_expired_suggestion_ids_respects_custom_threshold() -> None:
    suggestions = [{"id": "e", "state": "open", "created_at": _iso(NOW - timedelta(days=8))}]
    assert expired_suggestion_ids(suggestions, now=NOW, expire_days=7) == ["e"]
    assert expired_suggestion_ids(suggestions, now=NOW, expire_days=30) == []
