from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from omniagentos.connectors.store import CapabilityStore
from omniagentos.db.store import SqliteStore
from omniagentos.piedpiper import pipeline_report
from omniagentos.piedpiper.pipeline_report import CAPABILITY_ID, GOAL_ID, HOLDER, collect_once
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "piedpiper.db")


def _count(store: SqliteStore, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = store._connection.execute(sql, params).fetchone()
    return int(row[0])


def _grant(store: SqliteStore) -> None:
    # agent_capabilities.agent_id has an FK to agents(id): the operator-side
    # grant-issuance flow this exercises requires a registered holder row.
    store._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (HOLDER, HOLDER, "collector.piedpiper", None, "[]", "T1", "idle", "2026-08-13T00:00:00+00:00",
         "2026-08-13T00:00:00+00:00"),
    )
    store._connection.commit()
    CapabilityStore(store).set_grant(HOLDER, [CAPABILITY_ID])


def _ok(total: int) -> dict[str, Any]:
    return {"ok": True, "status": 200, "body": {"meta": {"total": total}}}


def test_four_reads_are_grant_backed_gets(store: SqliteStore, monkeypatch: Any) -> None:
    """Every call is a GET against piedpiper_acmeuni.read, holder-backed, granted UNSET."""
    _grant(store)
    calls: list[dict[str, Any]] = []

    def fake_call(cap_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"cap_id": cap_id, "args": args, **kwargs})
        return _ok(7)

    monkeypatch.setattr(pipeline_report.broker, "call", fake_call)
    collect_once(store, target_day=date(2026, 7, 10))

    assert len(calls) == 4
    for call in calls:
        assert call["cap_id"] == CAPABILITY_ID
        assert call["method"] == "GET"
        assert call["args"] == ()  # `granted` is never positionally supplied
        assert "granted" not in call  # nor keyword-supplied — the broker loads it
        assert call["grant_holder"] == HOLDER
        assert isinstance(call["grant_store"], CapabilityStore)


def test_no_grant_means_no_call_and_no_row(store: SqliteStore, monkeypatch: Any) -> None:
    """DARK-SAFE: with no grant, zero broker calls and zero persisted rows."""

    def unreachable(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("broker.call must not be invoked without a grant")

    monkeypatch.setattr(pipeline_report.broker, "call", unreachable)
    messages = collect_once(store, target_day=date(2026, 7, 10))

    assert messages[0]["collector"] == "piedpiper"
    assert "skipped" in messages[0]
    assert _count(store, "SELECT COUNT(*) FROM broker_calls") == 0
    assert _count(store, "SELECT COUNT(*) FROM metric_snapshots WHERE source = 'piedpiper'") == 0
    assert StewardStore(store).get_goal(GOAL_ID) is None


def test_same_day_rerun_is_idempotent(store: SqliteStore, monkeypatch: Any) -> None:
    """A naive INSERT would leave 8 rows after two same-day passes; UPSERT leaves 4."""
    _grant(store)
    monkeypatch.setattr(pipeline_report.broker, "call", lambda *_a, **_k: _ok(3))

    collect_once(store, target_day=date(2026, 7, 10))
    collect_once(store, target_day=date(2026, 7, 10))

    assert _count(store, "SELECT COUNT(*) FROM metric_snapshots WHERE source = 'piedpiper'") == 4


def test_denied_response_persists_nothing(store: SqliteStore, monkeypatch: Any) -> None:
    """A denied/malformed response on any one of the four reads writes zero rows."""
    _grant(store)
    calls = {"n": 0}

    def flaky_call(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 4:
            return {"ok": False, "status": 403, "body": {"error": "denied"}}
        return _ok(5)

    monkeypatch.setattr(pipeline_report.broker, "call", flaky_call)
    messages = collect_once(store, target_day=date(2026, 7, 10))

    assert calls["n"] == 4  # the denial was reached, not short-circuited early
    assert messages[0]["collector"] == "piedpiper"
    assert "skipped" in messages[0]
    assert _count(store, "SELECT COUNT(*) FROM metric_snapshots WHERE source = 'piedpiper'") == 0
    assert StewardStore(store).get_goal(GOAL_ID) is None
