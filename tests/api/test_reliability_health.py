"""Behavioral regressions for the reliability-summary health contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.api.routes.reliability import get_summary
from omniagentos.reliability.store import SqliteReliabilityStore


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def store(tmp_path: Path) -> SqliteReliabilityStore:
    result = SqliteReliabilityStore(str(tmp_path / "health.db"))
    try:
        yield result
    finally:
        result.close()


def _put_watch_state(
    store: SqliteReliabilityStore,
    *,
    heartbeat_at: str,
    cursor_at: str | None = None,
    value_json: str | None = None,
) -> None:
    payload = value_json
    if payload is None:
        payload = json.dumps(
            {
                "cursor": cursor_at or heartbeat_at,
                "first_seen": heartbeat_at,
            }
        )
    store._connection.execute(
        """
        INSERT INTO reliability_state (key, value_json, updated_at)
        VALUES ('watch_cursor', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                       updated_at = excluded.updated_at
        """,
        (payload, heartbeat_at),
    )


def test_summary_distinguishes_never_run_from_a_current_heartbeat(
    store: SqliteReliabilityStore,
) -> None:
    never = get_summary(store)

    assert never["contract_version"] == "reliability-summary.v1"
    assert never["health"] == "degraded"
    assert never["watch"] == {
        "state": "never_run",
        "heartbeat_at": None,
        "cursor_at": None,
        "age_seconds": None,
        "stale_after_seconds": 2700,
        "error": None,
    }
    assert never["watch_heartbeat"] is None

    now = datetime.now(UTC)
    _put_watch_state(store, heartbeat_at=_iso(now))
    current = get_summary(store)

    assert current["watch"]["state"] == "current"
    assert current["watch"]["heartbeat_at"] == _iso(now)
    assert current["watch_heartbeat"] == _iso(now)
    assert current["watch"]["age_seconds"] is not None
    assert current["watch"]["age_seconds"] < 5


def test_summary_marks_a_stale_durable_heartbeat_degraded(
    store: SqliteReliabilityStore,
) -> None:
    stale_at = _iso(datetime.now(UTC) - timedelta(hours=2))
    _put_watch_state(store, heartbeat_at=stale_at)

    summary = get_summary(store)

    assert summary["health"] == "degraded"
    assert summary["watch"]["state"] == "stale"
    assert summary["watch"]["heartbeat_at"] == stale_at
    assert summary["watch"]["age_seconds"] >= 7200
    assert "watch_stale" in summary["degraded_reasons"]


def test_summary_preserves_last_known_good_heartbeat_when_its_read_fails(
    store: SqliteReliabilityStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_at = _iso(datetime.now(UTC))
    _put_watch_state(store, heartbeat_at=heartbeat_at)
    first = get_summary(store)
    assert first["watch"]["state"] == "current"

    def unavailable() -> dict[str, object]:
        raise OSError("simulated store outage")

    monkeypatch.setattr(store, "get_watch_state", unavailable)
    degraded = get_summary(store)

    assert degraded["health"] == "degraded"
    assert degraded["watch"]["state"] == "last_known_good"
    assert degraded["watch"]["heartbeat_at"] == heartbeat_at
    assert degraded["watch"]["error"] == "watch_state_unavailable"
    assert "watch_state_unavailable" in degraded["degraded_reasons"]


def test_summary_preserves_row_heartbeat_when_watch_payload_is_corrupt(
    store: SqliteReliabilityStore,
) -> None:
    heartbeat_at = _iso(datetime.now(UTC) - timedelta(minutes=2))
    _put_watch_state(store, heartbeat_at=heartbeat_at, value_json="{not-json")

    summary = get_summary(store)

    assert summary["health"] == "degraded"
    assert summary["watch"]["state"] == "last_known_good"
    assert summary["watch"]["heartbeat_at"] == heartbeat_at
    assert summary["watch"]["cursor_at"] is None
    assert summary["watch"]["error"] == "corrupt_watch_state"
    assert "corrupt_watch_state" in summary["degraded_reasons"]


def test_summary_store_down_is_explicit_and_never_returns_healthy_zeros(
    tmp_path: Path,
) -> None:
    store = SqliteReliabilityStore(str(tmp_path / "closed.db"))
    store.close()

    summary = get_summary(store)

    assert summary["health"] == "degraded"
    assert summary["open_events"] == {
        "info": None,
        "warning": None,
        "critical": None,
    }
    assert summary["open_events_state"] == "unavailable"
    assert summary["last_audit_state"] == "unavailable"
    assert summary["watch"]["state"] == "unavailable"
    assert summary["incidents"]
    assert all(item["severity"] == "critical" for item in summary["incidents"])


def test_summary_persists_a_visible_incident_for_a_component_read_failure(
    store: SqliteReliabilityStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> dict[str, int]:
        raise OSError("simulated aggregate failure")

    monkeypatch.setattr(store, "count_open_events_by_severity", unavailable)

    summary = get_summary(store)

    assert summary["open_events_state"] == "unavailable"
    incidents = store.list_events(
        status="open",
        severity="critical",
        limit=20,
    )
    assert any(
        event.source == "api:reliability_summary"
        and event.evidence_json.get("component") == "open_events"
        for event in incidents
    )


def test_summary_counts_all_open_events_above_the_old_thousand_row_cap(
    store: SqliteReliabilityStore,
) -> None:
    now = _iso(datetime.now(UTC))
    rows = [
        (
            f"evt_bulk_{index}",
            "other",
            "warning",
            f"bulk-{index}",
            f"bulk-occurrence-{index}",
            "test",
            "{}",
            "open",
            now,
            now,
        )
        for index in range(1005)
    ]
    store._connection.executemany(
        """
        INSERT INTO reliability_events
          (id, failure_class, severity, signature, occurrence_key, source,
           evidence_json, status, detected_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    summary = get_summary(store)

    assert summary["open_events_state"] == "current"
    assert summary["open_events"]["warning"] == 1005
    assert summary["open_warning"] == 1005


def test_published_summary_fixture_matches_the_versioned_contract(
    store: SqliteReliabilityStore,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "fixtures"
        / "reliability-summary.v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["contract_version"] == "reliability-summary.v1"
    assert fixture["health"] == "degraded"
    assert fixture["watch"]["state"] == "last_known_good"
    assert fixture["watch"]["heartbeat_at"]
    assert fixture["watch"]["error"] == "watch_state_unavailable"
    assert fixture["open_events_state"] == "unavailable"
    live_keys = set(get_summary(store))
    assert set(fixture) == live_keys
    assert set(fixture["watch"]) == set(get_summary(store)["watch"])
