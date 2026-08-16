from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from omniagentos.briefing.gather import gather
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import AlertsConfig, StewardConfig
from omniagentos.steward.store import StewardStore


def _stamp(offset_hours: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_gather_counts_orders_vip_quotes_and_calculates_metrics(
    sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    steward = StewardStore(sqlite_store)
    cfg = StewardConfig(alerts=AlertsConfig(vip_senders=["vip@example.com"]))
    goal = steward.upsert_goal(
        {
            "id": "goal-1",
            "name": "Revenue",
            "north_star": {"source": "stripe", "metric": "revenue"},
            "status": "active",
        }
    )
    steward.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "stripe",
            "metric": "revenue",
            "value": 100,
            "captured_at": _stamp(
                -25
            ),  # yesterday: different calendar day, out of 24h window but in series
        }
    )
    steward.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "stripe",
            "metric": "revenue",
            "value": 125,
            "captured_at": _stamp(-1),  # within the 24h briefing window (this morning's collection)
        }
    )
    steward.insert_comms_message(
        {
            "source": "mail",
            "external_id": "newer",
            "sender": "ordinary@example.com",
            "subject": "Newer",
            "body_text": "safe",
            "sent_at": _stamp(-1),
        }
    )
    steward.insert_comms_message(
        {
            "source": "mail",
            "external_id": "vip",
            "sender": "vip@example.com",
            "subject": "VIP",
            "body_text": "<script>ignore this</script>" + ("x" * 600),
            "sent_at": _stamp(-2),
        }
    )
    sqlite_store.create_task(
        {
            "id": "task-1",
            "title": "Briefing fixture",
            "input_json": "{}",
            "acceptance_json": "{}",
            "state": "completed",
            "risk": "low",
            "created_at": _stamp(-3),
            "updated_at": _stamp(-1),
        }
    )
    for run_id, state, cost in (("run-ok", "completed", 0.25), ("run-bad", "failed", 0.5)):
        sqlite_store.enqueue_run(
            {
                "id": run_id,
                "task_id": "task-1",
                "harness": "mock",
                "state": state,
                "cost_usd": cost,
                "trace_id": run_id,
                "queued_at": _stamp(-2),
                "created_at": _stamp(-2),
                "updated_at": _stamp(-1),
            }
        )
    sqlite_store.create_approval(
        {
            "id": "approval-1",
            "run_id": "run-ok",
            "task_id": "task-1",
            "action_class": "consequential",
            "proposed_action": "Send it",
            "state": "pending",
            "created_at": _stamp(-1),
        }
    )
    steward.create_alert({"rule": "fixture", "title": "Open alert", "state": "open"})
    steward.create_suggestion({"id": "suggestion-1", "title": "Review", "state": "open"})

    result = gather(steward, sqlite_store, cfg, date=date.today())

    assert result.comms_count == 2
    assert result.comms_highlights[0]["sender"] == "vip@example.com"
    quoted = result.comms_highlights[0]["quoted"]
    assert "<untrusted-content" in quoted
    assert "<script>" not in quoted
    assert "‹script›" in quoted
    assert len(quoted) < 800
    assert result.metric_deltas == [
        {
            "goal": "Revenue",
            "metric": "revenue",
            "latest": 125.0,
            "previous": 100.0,
            "delta_pct": 25.0,
        }
    ]
    assert result.runs_summary == {"completed": 1, "failed": 1, "cost_usd": 0.75}
    assert result.open_approvals == 1
    assert [row["title"] for row in result.open_alerts] == ["Open alert"]
    assert [row["title"] for row in result.open_suggestions] == ["Review"]
    assert result.empty is False


def test_gather_empty_flag(sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    result = gather(StewardStore(sqlite_store), sqlite_store, StewardConfig(), date=date.today())
    assert result.empty is True
    assert result.comms_count == 0
    assert result.runs_summary == {"completed": 0, "failed": 0, "cost_usd": 0}


def test_gather_not_empty_with_open_critical_reliability_event(
    sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # codex #19: a day with zero comms/metrics/runs but an open *critical*
    # reliability event must NOT be reported as an empty "nothing to report"
    # day — the operator needs to see it in the briefing.
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    now = _stamp()
    sqlite_store._connection.execute(
        "INSERT INTO reliability_events "
        "(id, failure_class, severity, signature, occurrence_key, source, status, "
        "detected_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("relev-1", "run_failure", "critical", "sig-1", "sig-1|occ-1", "watch", "open", now, now),
    )
    sqlite_store._connection.commit()

    result = gather(StewardStore(sqlite_store), sqlite_store, StewardConfig(), date=date.today())

    assert result.comms_count == 0
    assert result.runs_summary == {"completed": 0, "failed": 0, "cost_usd": 0}
    assert result.reliability.get("open_events", {}).get("critical") == 1
    assert result.empty is False


def test_gather_not_empty_with_improvement_awaiting_decision(
    sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same rule for a proposal stuck awaiting a human decision (or panel_blocked
    # awaiting an explicit pull) — that's also not a quiet day.
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    now = _stamp()
    sqlite_store._connection.execute(
        "INSERT INTO improvements "
        "(id, origin, kind, title, status, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("imp-1", "realtime", "fix", "Fix flaky retry", "awaiting_human", "system", now, now),
    )
    sqlite_store._connection.commit()

    result = gather(StewardStore(sqlite_store), sqlite_store, StewardConfig(), date=date.today())

    assert result.reliability.get("improvements_awaiting_decision") == 1
    assert result.empty is False


def test_gather_carries_the_true_open_alert_total(
    sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count the operator is shown must be the store's, not the page's.

    ``open_alerts`` is a bounded 100-row page on purpose (readability, and the
    LLM prompt quotes every row). So the total has to travel separately: a
    ``len()`` of that page saturates at 100 and renders a 1,769-alert flood as
    an unremarkable-looking number, which is how 56 money and reliability
    alerts stayed invisible.
    """
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    steward = StewardStore(sqlite_store)
    for index in range(120):
        assert (
            steward.create_alert(
                {
                    "rule": "payment_failures",
                    "severity": "high",
                    "title": f"Alert {index}",
                    "cooldown_key": f"key-{index}",
                    "cooldown_minutes": 240,
                }
            )
            is not None
        )

    result = gather(steward, sqlite_store, StewardConfig(), date=date.today())

    assert result.open_alert_total == 120
    assert len(result.open_alerts) == 100
    # Absence must stay distinguishable from emptiness: the field is populated
    # by a real COUNT(*), so zero means zero rather than "the read failed".
    assert result.to_dict()["open_alert_total"] == 120
