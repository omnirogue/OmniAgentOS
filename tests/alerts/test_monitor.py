from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import Events
from omniagentos.db.store import SqliteStore
from omniagentos.grants.store import GrantsStore
from omniagentos.steward.alerts import monitor
from omniagentos.steward.alerts.rules import AlertCandidate
from omniagentos.steward.config import AlertsConfig, BriefingConfig, StewardConfig
from omniagentos.steward.notify import NotifyResult
from omniagentos.steward.store import StewardStore


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _seed_snapshot(
    steward: StewardStore,
    source: str,
    metric: str,
    value: float,
    *,
    captured_at: datetime | None = None,
) -> None:
    steward.insert_metric_snapshot(
        {
            "source": source,
            "metric": metric,
            "value": value,
            "captured_at": (captured_at or _now()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )


def test_monitor_cycle_creates_once_notifies_emits_event_and_suppresses_cooldown(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_snapshot(steward, "meta", "roas", 0.5)
    cfg = steward_config.model_copy(
        update={"briefing": BriefingConfig(deliver_email="owner@example.com")}
    )
    slack: list[str] = []
    email: list[tuple[str, str, str]] = []

    def fake_send_slack(text: str, **_kwargs: object) -> NotifyResult:
        slack.append(text)
        return NotifyResult(True, "slack", "sent")

    def fake_send_piedpiper_email(to: str, subject: str, body: str) -> NotifyResult:
        email.append((to, subject, body))
        return NotifyResult(True, "piedpiper_email", "sent")

    monkeypatch.setattr(monitor, "send_slack", fake_send_slack)
    monkeypatch.setattr(monitor, "send_piedpiper_email", fake_send_piedpiper_email)

    first = monitor.monitor_once(
        database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
    )
    second = monitor.monitor_once(
        database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
    )

    assert first == {"evaluated": 1, "created": 1, "suppressed": 0, "triaged": 0}
    assert second == {"evaluated": 1, "created": 0, "suppressed": 1, "triaged": 0}
    assert len(steward.list_alerts()) == 1
    assert len(slack) == 1  # Slack is sent for every successfully created alert.
    assert len(email) == 1  # Critical alerts are emailed when delivery is configured.
    events = database.get_events_after(0, types=[Events.ALERT_CREATED])
    assert len(events) == 1
    assert events[0]["target_type"] == "alert"


@pytest.mark.parametrize(
    ("source", "metric", "value", "rule"),
    [("meta", "roas", 0.5, "roas_floor"), ("stripe", "payment_failures", 3, "payment_failures")],
)
def test_remediation_alerts_create_linked_read_only_suggestions(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    source: str,
    metric: str,
    value: float,
    rule: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_snapshot(steward, source, metric, value)
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))

    assert (
        monitor.monitor_once(
            database, cfg=steward_config, now=_now(), triaged_marker_path=triaged_marker_path
        )["created"]
        == 1
    )

    alert = steward.list_alerts()[0]
    suggestion = steward.list_suggestions()[0]
    assert alert["rule"] == rule
    assert suggestion["alert_id"] == alert["id"]
    assert suggestion["risk_class"] == "read_only"
    assert suggestion["source"] == "alerts"


def test_high_alerts_still_notify_slack_but_never_email(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_snapshot(steward, "stripe", "payment_failures", 3)
    cfg = steward_config.model_copy(
        update={"briefing": BriefingConfig(deliver_email="owner@example.com")}
    )
    slack: list[str] = []
    email: list[object] = []

    def fake_send_slack(text: str, **_kwargs: object) -> NotifyResult:
        slack.append(text)
        return NotifyResult(True, "slack", "sent")

    def fake_send_piedpiper_email(*_args: object) -> NotifyResult:
        email.append(object())
        return NotifyResult(True, "piedpiper_email", "sent")

    monkeypatch.setattr(monitor, "send_slack", fake_send_slack)
    monkeypatch.setattr(monitor, "send_piedpiper_email", fake_send_piedpiper_email)

    assert (
        monitor.monitor_once(
            database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
        )["created"]
        == 1
    )
    assert len(slack) == 1
    assert email == []


def test_one_broken_rule_does_not_abort_unrelated_payment_alerts(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_snapshot(steward, "stripe", "payment_failures", 3)
    monkeypatch.setattr(
        monitor,
        "reliability_deadman",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("dead-man defect")),
    )
    monkeypatch.setattr(
        monitor,
        "send_slack",
        lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"),
    )

    with caplog.at_level(logging.ERROR):
        summary = monitor.monitor_once(
            database,
            cfg=steward_config,
            now=_now(),
            triaged_marker_path=triaged_marker_path,
        )

    assert summary["created"] == 1
    assert steward.list_alerts()[0]["rule"] == "payment_failures"
    assert any(
        "reliability_deadman" in record.message and "failed" in record.message
        for record in caplog.records
    )


def test_audit_state_read_failure_preserves_watch_and_becomes_deadman_incident() -> None:
    now = _now()
    heartbeat = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    class _Result:
        def __init__(self, row: dict[str, Any]) -> None:
            self.row = row

        def fetchone(self) -> dict[str, Any]:
            return self.row

    class _Connection:
        def execute(self, sql: str) -> _Result:
            if "reliability_state" in sql:
                return _Result(
                    {
                        "key": "watch_cursor",
                        "value_json": json.dumps({"cursor": heartbeat, "first_seen": heartbeat}),
                        "updated_at": heartbeat,
                    }
                )
            raise RuntimeError("audit store unavailable")

    class _Database:
        _connection = _Connection()

    watch, audit = monitor._reliability_liveness(_Database())  # type: ignore[arg-type]
    candidates = monitor.reliability_deadman(watch, audit, now)

    assert watch is not None and watch["updated_at"] == heartbeat
    assert audit == {"_state": "store_error", "error_type": "RuntimeError"}
    assert [candidate.cooldown_key for candidate in candidates] == ["reliability_deadman_store"]
    assert candidates[0].evidence["components"] == ["audit"]


@pytest.mark.parametrize(("urgent", "created"), [(True, 1), (False, 0)])
def test_borderline_messages_use_triage_result(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    urgent: bool,
    created: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message, inserted = steward.insert_comms_message(
        {
            "source": "mail",
            "external_id": f"borderline-{urgent}",
            "sender": "customer@example.com",
            "sent_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "subject": "urgent question",
            "body_text": "Please help",
        }
    )
    assert inserted is True
    received: list[dict[str, Any]] = []
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))

    def fake_triage(row: dict[str, Any], _cfg: object) -> dict[str, bool | str]:
        received.append(row)
        return {"urgent": urgent, "reason": "fake adapter"}

    summary = monitor.monitor_once(
        database,
        cfg=steward_config,
        now=_now(),
        triage=fake_triage,
        triaged_marker_path=triaged_marker_path,
    )

    assert summary == {"evaluated": 1, "created": created, "suppressed": 0, "triaged": 1}
    assert received == [message]
    if urgent:
        assert steward.list_alerts()[0]["rule"] == "llm_triage"


# --- H6/PERF-001/SEC-O-005: triage-once + per-cycle cap regression --------


def test_triage_once_per_message_not_repeated_across_cycles(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A borderline message triaged in cycle 1 must NOT be re-triaged in cycle 2.

    Without the H6 fix, `monitor_once` triages every borderline message on
    every cycle it still appears in the trailing-24h window (no marker of
    "already triaged" was ever kept) -- this reproduces exactly that: the same
    still-recent message is fed through two consecutive cycles and the triage
    function's call count is asserted to stay at 1.
    """
    steward.insert_comms_message(
        {
            "source": "mail",
            "external_id": "borderline-repeat",
            "sender": "customer@example.com",
            "sent_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "subject": "urgent question",
            "body_text": "Please help",
        }
    )
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    calls: list[dict[str, Any]] = []

    def fake_triage(row: dict[str, Any], _cfg: object) -> dict[str, bool | str]:
        calls.append(row)
        return {"urgent": False, "reason": "not urgent"}

    first = monitor.monitor_once(
        database,
        cfg=steward_config,
        now=_now(),
        triage=fake_triage,
        triaged_marker_path=triaged_marker_path,
    )
    second = monitor.monitor_once(
        database,
        cfg=steward_config,
        now=_now(),
        triage=fake_triage,
        triaged_marker_path=triaged_marker_path,
    )

    assert first["triaged"] == 1
    # H6 fix under test: the second cycle must skip the already-triaged message.
    assert second["triaged"] == 0
    assert len(calls) == 1


def test_triage_per_cycle_cap_defers_excess_messages(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A first-seen flood of borderline messages is capped in a single cycle.

    Without the H6 fix there is no `triage_max_per_cycle` cap at all, so all N
    messages would be triaged (and counted) in one cycle; this seeds more
    messages than the configured cap and asserts only the cap's worth run.
    """
    cfg = StewardConfig(
        alerts=AlertsConfig(vip_senders=[], urgent_patterns=["urgent"], triage_max_per_cycle=2)
    )
    for index in range(5):
        steward.insert_comms_message(
            {
                "source": "mail",
                "external_id": f"flood-{index}",
                "sender": "customer@example.com",
                "sent_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "subject": "urgent question",
                "body_text": "Please help",
            }
        )
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    calls: list[dict[str, Any]] = []

    def fake_triage(row: dict[str, Any], _cfg: object) -> dict[str, bool | str]:
        calls.append(row)
        return {"urgent": False, "reason": "not urgent"}

    with caplog.at_level(logging.WARNING):
        summary = monitor.monitor_once(
            database,
            cfg=cfg,
            now=_now(),
            triage=fake_triage,
            triaged_marker_path=triaged_marker_path,
        )

    assert summary["triaged"] == 2
    assert len(calls) == 2
    assert any("cap" in record.message.lower() for record in caplog.records)


# --- H10/PROD-001: revenue-crash alert wired into monitor_once ------------


def test_revenue_drop_fires_critical_and_does_not_fire_on_healthy_revenue(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the H10 fix there is no revenue_drop rule at all -- monitor_once
    never creates an alert for a zero-revenue day no matter what net_revenue_usd
    snapshots exist. This proves it now does, and that healthy revenue doesn't
    spuriously fire.
    """
    cfg = StewardConfig(
        alerts=AlertsConfig(revenue_floor_usd=0.0, revenue_drop_pct=60.0, revenue_baseline_days=7)
    )
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))

    # Healthy revenue: no floor breach, no baseline yet -- must not fire.
    _seed_snapshot(steward, "stripe", "net_revenue_usd", 500.0)
    healthy = monitor.monitor_once(
        database, now=_now(), cfg=cfg, triaged_marker_path=triaged_marker_path
    )
    assert healthy["created"] == 0
    assert steward.list_alerts() == []

    # Zero-revenue day: floor rule must fire CRITICAL, even with only 1 day of history.
    _seed_snapshot(
        steward, "stripe", "net_revenue_usd", 0.0, captured_at=_now() + timedelta(days=1)
    )
    zero_day = monitor.monitor_once(
        database, now=_now() + timedelta(days=1), cfg=cfg, triaged_marker_path=triaged_marker_path
    )
    assert zero_day["created"] == 1
    alerts = steward.list_alerts()
    assert alerts[0]["rule"] == "revenue_drop"
    assert alerts[0]["severity"] == "critical"


def test_revenue_drop_fires_on_pct_crash_vs_trailing_baseline(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = StewardConfig(
        alerts=AlertsConfig(
            revenue_floor_usd=0.0,
            revenue_drop_pct=60.0,
            revenue_baseline_days=7,
            cooldown_minutes=1,
        )
    )
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    base_day = _now() - timedelta(days=7)
    for offset in range(7):
        _seed_snapshot(
            steward,
            "stripe",
            "net_revenue_usd",
            100.0,
            captured_at=base_day + timedelta(days=offset),
        )
    # Today crashes 70% below the $100/day baseline -- above the 60% threshold.
    _seed_snapshot(steward, "stripe", "net_revenue_usd", 30.0, captured_at=_now())

    summary = monitor.monitor_once(
        database, now=_now(), cfg=cfg, triaged_marker_path=triaged_marker_path
    )

    assert summary["created"] == 1
    alert = steward.list_alerts()[0]
    assert alert["rule"] == "revenue_drop"
    assert alert["severity"] == "critical"


# --- H12: escalation-through-cooldown for a worsening magnitude -----------


def test_worsening_roas_escalates_through_cooldown_via_magnitude(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same severity ("critical") both cycles, but a much worse ROAS the second
    time must still create a new alert (bypassing the 240-min cooldown) because
    its magnitude (the shortfall below the floor) jumped by >= 1.5x.

    Without H12's monitor-half fix, no magnitude is ever passed to
    create_alert, so store.py's escalation check never fires and the second,
    much-worse reading is silently suppressed by cooldown same as an unchanged
    repeat would be.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    cfg = StewardConfig(alerts=AlertsConfig(roas_floor=1.0, cooldown_minutes=240))

    _seed_snapshot(steward, "meta", "roas", 0.9)  # shortfall 0.1
    first = monitor.monitor_once(
        database, now=_now(), cfg=cfg, triaged_marker_path=triaged_marker_path
    )
    assert first["created"] == 1

    _seed_snapshot(
        steward, "meta", "roas", 0.2, captured_at=_now()
    )  # shortfall 0.8 -- >=1.5x worse
    second = monitor.monitor_once(
        database, now=_now(), cfg=cfg, triaged_marker_path=triaged_marker_path
    )

    assert second["created"] == 1  # escalates through cooldown instead of being suppressed
    # CASE IDENTITY: escalation updates the SAME open case in place instead of
    # appending a second row for a condition that never actually recovered.
    alerts = steward.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]["evidence"]["_case"]["occurrence_count"] == 2
    assert alerts[0]["evidence"]["magnitude"] == pytest.approx(0.8)


# --- M3/PROD-004: undelivered critical alert is recorded + warned ---------


def test_undelivered_critical_alert_is_recorded_and_warned(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CRITICAL alert with Slack unconfigured and no delivery email set must
    be recorded as undelivered (queryable via the alert-creation event) and a
    WARNING logged.

    Without the M3 fix, `_notify`'s NotifyResult is discarded entirely -- an
    alert whose only channel silently failed leaves zero trace anywhere.
    """
    _seed_snapshot(steward, "meta", "roas", 0.1)  # roas_floor -> critical
    monkeypatch.setattr(
        monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(False, "slack", "not configured")
    )

    with caplog.at_level(logging.WARNING):
        summary = monitor.monitor_once(
            database, cfg=steward_config, now=_now(), triaged_marker_path=triaged_marker_path
        )

    assert summary["created"] == 1
    events = database.get_events_after(0, types=[Events.ALERT_CREATED])
    payload = json.loads(events[0]["payload_json"])
    assert payload["evidence"]["undelivered"] is True
    assert payload["evidence"]["delivery"] == [
        {"channel": "slack", "ok": False, "detail": "not configured"}
    ]
    assert any("reached no delivery channel" in record.message for record in caplog.records)


def test_high_alert_with_no_channel_is_flagged_on_the_row(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RR-PROD-002: a critical alert created when NO channel is configured must be
    # visibly flagged on the alert ROW (evidence.delivery_warning), so the operator
    # who by definition wasn't pushed can still see it in the dashboard — not left
    # pixel-identical to a delivered alert.
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    _seed_snapshot(steward, "meta", "roas", 0.5)  # breaches roas_floor -> critical
    cfg = steward_config.model_copy(update={"briefing": BriefingConfig(deliver_email=None)})
    monkeypatch.setattr(
        monitor, "send_slack", lambda text, **_kwargs: NotifyResult(False, "slack", "not configured")
    )

    result = monitor.monitor_once(
        database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
    )
    assert result["created"] == 1
    alert = steward.list_alerts("open")[0]
    assert alert["evidence"].get("delivery_warning") == "no critical-capable channel configured"

    # Contrast: with a channel configured, the row carries NO delivery_warning.
    # payment_failures is a money rule (see monitor.MONEY_RULES), so its
    # "channel configured" check reads MONEY_ALERT_SLACK_WEBHOOK_URL, not the
    # shared SLACK_WEBHOOK_URL -- both must be set here for the same reason.
    steward.ack_alert(alert["id"], "operator")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("MONEY_ALERT_SLACK_WEBHOOK_URL", "https://hooks.example/money")
    monkeypatch.setattr(monitor, "send_slack", lambda text, **_kwargs: NotifyResult(True, "slack", "sent"))
    _seed_snapshot(steward, "stripe", "payment_failures", 5.0)  # another high/critical rule
    monitor.monitor_once(database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path)
    for row in steward.list_alerts("open"):
        assert "delivery_warning" not in row["evidence"]


# --- CASE IDENTITY + AUTO-RESOLVE: fire -> recover -> fire -----------------


def test_alert_fire_recover_fire_lifecycle_uses_case_identity(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without auto-resolve, the first firing's row would sit open forever
    even after the condition recovers (the reported "82 opens, 3 keys"
    backlog symptom). A subsequent re-breach must open a BRAND NEW case, not
    reopen the already-resolved row: open -> resolved -> new case.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    cfg = StewardConfig(alerts=AlertsConfig(roas_floor=1.0, vip_senders=[], urgent_patterns=[]))
    base = _now()

    _seed_snapshot(steward, "meta", "roas", 0.5, captured_at=base)
    fired = monitor.monitor_once(
        database, cfg=cfg, now=base, triaged_marker_path=triaged_marker_path
    )
    assert fired == {"evaluated": 1, "created": 1, "suppressed": 0, "triaged": 0}
    open_alerts = steward.list_alerts("open")
    assert len(open_alerts) == 1
    first_id = open_alerts[0]["id"]

    recovered_at = base + timedelta(days=1)
    _seed_snapshot(steward, "meta", "roas", 2.0, captured_at=recovered_at)
    recovered_summary = monitor.monitor_once(
        database, cfg=cfg, now=recovered_at, triaged_marker_path=triaged_marker_path
    )
    assert recovered_summary["created"] == 0
    assert steward.open_alert_count() == 0
    resolved = steward.get_alert(first_id)
    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["evidence"]["_case"]["resolved_reason"] == "recovered"

    refired_at = base + timedelta(days=2)
    _seed_snapshot(steward, "meta", "roas", 0.3, captured_at=refired_at)
    refired_summary = monitor.monitor_once(
        database, cfg=cfg, now=refired_at, triaged_marker_path=triaged_marker_path
    )
    assert refired_summary["created"] == 1
    new_open = steward.list_alerts("open")
    assert len(new_open) == 1
    assert new_open[0]["id"] != first_id
    assert new_open[0]["evidence"]["_case"]["occurrence_count"] == 1
    assert len(steward.list_alerts()) == 2  # one resolved case + one fresh open case


# --- DISABLED/REMOVED RULES: their open cases are not zombies ---------------


def test_a_rule_absent_from_the_config_has_its_open_cases_resolved(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule nobody evaluates any more can never report its key again.

    Auto-resolve only touches rules that RAN, so a disabled or deleted rule's
    open cases used to sit open until the 14-day stale sweep closed them as
    backlog they were never part of -- a fortnight of a dashboard counting an
    alert whose rule no longer exists.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    now = _now()
    zombie = steward.create_alert(
        {
            "rule": "retired_rule",
            "severity": "high",
            "title": "Raised by a rule that no longer exists",
            "cooldown_key": "retired:key",
            "cooldown_minutes": 240,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    assert zombie is not None

    cfg = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=[]))
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)

    resolved = steward.get_alert(zombie["id"])
    assert resolved is not None
    assert resolved["state"] == "resolved"
    # Its own reason: not "recovered" (nothing cleared -- nobody looked) and not
    # "stale_backlog" (it is one cycle old).
    assert resolved["evidence"]["_case"]["resolved_reason"] == "rule_disabled"


def test_a_rule_that_raised_this_cycle_keeps_its_open_cases(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Present-but-crashed is not absent-from-config, and must not be swept.

    The distinction is the entire safety property: a broken evaluator that
    closed its own cases would silently hide a live incident behind a defect.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    monkeypatch.setattr(
        monitor,
        "reliability_deadman",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("dead-man defect")),
    )
    now = _now()
    live = steward.create_alert(
        {
            "rule": "reliability_deadman",
            "severity": "high",
            "title": "Raised before the rule broke",
            "cooldown_key": "deadman:key",
            "cooldown_minutes": 240,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    assert live is not None

    cfg = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=[]))
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)

    still_open = steward.get_alert(live["id"])
    assert still_open is not None
    assert still_open["state"] == "open"


def test_a_rule_that_raised_this_cycle_still_never_auto_resolves(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-existing guarantee, restated against the new sweep.

    ``roas_floor`` is fully configured here; it raises, so it reports no keys.
    Neither closure path may claim its case.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    monkeypatch.setattr(
        monitor,
        "roas_floor",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("rule defect")),
    )
    now = _now()
    live = steward.create_alert(
        {
            "rule": "roas_floor",
            "severity": "critical",
            "title": "ROAS below floor",
            "cooldown_key": "roas_floor",
            "cooldown_minutes": 240,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    assert live is not None

    cfg = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=[]))
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)

    still_open = steward.get_alert(live["id"])
    assert still_open is not None
    assert still_open["state"] == "open"


def test_every_rule_label_is_the_rule_name_its_alerts_carry(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label that names the FUNCTION instead of the rule breaks both sweeps.

    ``payment_failure_burst`` (the function) raises candidates whose rule is
    ``payment_failures`` (the row). While the two disagreed, that rule's cases
    could never auto-resolve, and the disabled-rule sweep would read every live
    payment alert as belonging to a rule that no longer exists. The clean cycle
    below closes the case as RECOVERED, which is only possible when the label
    and the row agree.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    now = _now()
    cfg = StewardConfig(
        alerts=AlertsConfig(payment_failure_burst=3, vip_senders=[], urgent_patterns=[])
    )
    _seed_snapshot(steward, "stripe", "payment_failures", 5.0, captured_at=now)
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)
    fired = steward.list_alerts("open")
    assert [row["rule"] for row in fired] == ["payment_failures"]

    later = now + timedelta(days=1)
    _seed_snapshot(steward, "stripe", "payment_failures", 0.0, captured_at=later)
    monitor.monitor_once(database, cfg=cfg, now=later, triaged_marker_path=triaged_marker_path)

    resolved = steward.get_alert(fired[0]["id"])
    assert resolved is not None
    assert resolved["evidence"]["_case"]["resolved_reason"] == "recovered"


def test_a_rule_that_ran_clean_still_resolves_as_recovered_not_disabled(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new sweep must not steal the recovered path's cases or its reason."""
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    now = _now()
    cfg = StewardConfig(alerts=AlertsConfig(roas_floor=1.0, vip_senders=[], urgent_patterns=[]))
    _seed_snapshot(steward, "meta", "roas", 0.5, captured_at=now)
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)
    fired = steward.list_alerts("open")
    assert len(fired) == 1

    later = now + timedelta(days=1)
    _seed_snapshot(steward, "meta", "roas", 2.0, captured_at=later)
    monitor.monitor_once(database, cfg=cfg, now=later, triaged_marker_path=triaged_marker_path)

    resolved = steward.get_alert(fired[0]["id"])
    assert resolved is not None
    assert resolved["evidence"]["_case"]["resolved_reason"] == "recovered"


# --- BACKLOG POLICY SWEEP: 14-day aging for undecided cases -----------------


def test_backlog_policy_sweep_resolves_stale_alerts_and_expires_old_suggestions(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing decided in > 14 days ages out on the regular monitor cycle, into
    a terminal state DISTINCT from the other closure paths: 'stale_backlog' is
    not 'recovered', and 'expired' is not a human decision like 'rejected'.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    now = _now()
    old = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # These rows belong to a rule that is CONFIGURED but CRASHED this cycle, so
    # neither of the other two closure paths can claim them: auto-resolve needs
    # the rule to have completed, and the disabled-rule sweep needs it to be
    # absent from the rule set. That isolates the assertion to the backlog
    # sweep alone -- and pins that a broken evaluator never closes its cases.
    monkeypatch.setattr(
        monitor,
        "reliability_deadman",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("dead-man defect")),
    )
    stale_alert = steward.create_alert(
        {
            "rule": "reliability_deadman",
            "severity": "high",
            "title": "Old alert",
            "cooldown_key": "legacy:stale",
            "cooldown_minutes": 240,
            "created_at": old,
        }
    )
    fresh_alert = steward.create_alert(
        {
            "rule": "reliability_deadman",
            "severity": "high",
            "title": "Recent alert",
            "cooldown_key": "legacy:recent",
            "cooldown_minutes": 240,
            "created_at": recent,
        }
    )
    assert stale_alert is not None and fresh_alert is not None

    stale_suggestion = steward.create_suggestion(
        {
            "title": "Old suggestion",
            "risk_class": "read_only",
            "source": "goals",
            "created_at": old,
        }
    )
    fresh_suggestion = steward.create_suggestion(
        {
            "title": "Recent suggestion",
            "risk_class": "read_only",
            "source": "goals",
            "created_at": recent,
        }
    )

    cfg = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=[]))
    monitor.monitor_once(database, cfg=cfg, now=now, triaged_marker_path=triaged_marker_path)

    resolved = steward.get_alert(stale_alert["id"])
    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["evidence"]["_case"]["resolved_reason"] == "stale_backlog"
    still_open_alert = steward.get_alert(fresh_alert["id"])
    assert still_open_alert is not None and still_open_alert["state"] == "open"

    expired = steward.get_suggestion(stale_suggestion["id"])
    assert expired is not None
    assert expired["state"] == "expired"
    assert expired["decided_by"] == "system:policy-sweep"
    still_open_suggestion = steward.get_suggestion(fresh_suggestion["id"])
    assert still_open_suggestion is not None and still_open_suggestion["state"] == "open"


# --- PAGINATION: the three sweeps must see EVERY open case, not just page one -


def _seed_open_alert(
    steward: StewardStore, rule: str, cooldown_key: str, *, created_at: str
) -> dict[str, Any]:
    alert = steward.create_alert(
        {
            "rule": rule,
            "severity": "high",
            "title": f"{rule} fired",
            "cooldown_key": cooldown_key,
            "cooldown_minutes": 240,
            "created_at": created_at,
        }
    )
    assert alert is not None
    return alert


def test_auto_resolve_recovers_a_case_beyond_the_first_page(steward: StewardStore) -> None:
    """The other half of the suggestion-dedupe pagination fix (9e01d306):
    ``list_alerts("open")``'s default ``limit=100`` used to be the ONLY read
    auto-resolve had, so a recovered condition whose row had aged past page
    one would never close -- exactly when the backlog it exists to shrink was
    worst.
    """
    target = _seed_open_alert(
        steward, "roas_floor", "target-key", created_at="2026-01-01T00:00:00Z"
    )
    # 150 newer rows on an unrelated rule push the target off page one:
    # list_alerts("open") orders by created_at DESC, so newer rows crowd it out.
    for index in range(150):
        _seed_open_alert(
            steward,
            "filler_rule",
            f"filler-{index}",
            created_at=f"2026-02-01T00:{index // 60:02d}:{index % 60:02d}Z",
        )
    assert len(steward.list_alerts("open")) == 100  # the page the old code saw
    assert target["id"] not in {row["id"] for row in steward.list_alerts("open")}

    resolved = monitor._auto_resolve_recovered(steward, {"roas_floor": []}, now=_now())

    assert resolved == 1
    row = steward.get_alert(target["id"])
    assert row is not None
    assert row["state"] == "resolved"
    assert row["evidence"]["_case"]["resolved_reason"] == "recovered"
    # The 150 filler alerts belong to a rule that never ran this cycle, so none
    # of them were eligible -- only the boundary-crossing target was touched.
    assert steward.open_alert_count() == 150


def test_disabled_rule_sweep_resolves_a_case_beyond_the_first_page(
    steward: StewardStore,
) -> None:
    """Same pagination fix, the retired/disabled-rule closure path."""
    target = _seed_open_alert(
        steward, "retired_rule", "retired:key", created_at="2026-01-01T00:00:00Z"
    )
    for index in range(150):
        _seed_open_alert(
            steward,
            "live_rule",
            f"live-{index}",
            created_at=f"2026-02-01T00:{index // 60:02d}:{index % 60:02d}Z",
        )
    assert target["id"] not in {row["id"] for row in steward.list_alerts("open")}

    resolved = monitor._resolve_disabled_rule_cases(steward, {"live_rule"}, now=_now())

    assert resolved == 1
    row = steward.get_alert(target["id"])
    assert row is not None
    assert row["state"] == "resolved"
    assert row["evidence"]["_case"]["resolved_reason"] == "rule_disabled"
    # live_rule is configured, so none of the 150 recent rows were eligible.
    assert steward.open_alert_count() == 150


def test_backlog_sweep_ages_a_stale_case_beyond_the_first_page(steward: StewardStore) -> None:
    """Same pagination fix, the 14-day stale-backlog aging path."""
    now = _now()
    old = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = _seed_open_alert(steward, "some_rule", "stale:key", created_at=old)
    for index in range(150):
        _seed_open_alert(
            steward,
            "some_rule",
            f"recent-{index}",
            created_at=(now - timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    assert target["id"] not in {row["id"] for row in steward.list_alerts("open")}

    resolved, _expired = monitor._sweep_backlog(steward, now=now)

    assert resolved == 1
    row = steward.get_alert(target["id"])
    assert row is not None
    assert row["state"] == "resolved"
    assert row["evidence"]["_case"]["resolved_reason"] == "stale_backlog"
    # The 150 filler rows are minutes old, nowhere near the 14-day cutoff.
    assert steward.open_alert_count() == 150


# --- SUGGESTION DEDUPE: remediation suggestions collapse across occurrences -


def test_remediation_suggestion_dedupes_across_alert_occurrences(
    database: SqliteStore,
    steward: StewardStore,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated (escalating) roas_floor breach must not append a second
    "Review underperforming ad sets" suggestion (council: 71 suggestions
    collapsed to ~2 repeated titles) -- it bumps the SAME open suggestion.
    """
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))
    cfg = StewardConfig(alerts=AlertsConfig(roas_floor=1.0, vip_senders=[], urgent_patterns=[]))
    base = _now()

    _seed_snapshot(steward, "meta", "roas", 0.9, captured_at=base)
    monitor.monitor_once(database, cfg=cfg, now=base, triaged_marker_path=triaged_marker_path)
    _seed_snapshot(steward, "meta", "roas", 0.1, captured_at=base + timedelta(days=1))
    monitor.monitor_once(
        database, cfg=cfg, now=base + timedelta(days=1), triaged_marker_path=triaged_marker_path
    )

    suggestions = steward.list_suggestions()
    matching = [row for row in suggestions if row["title"] == "Review underperforming ad sets"]
    assert len(matching) == 1
    assert len(matching[0]["evidence"]) == 2
    alert = steward.list_alerts()[0]
    assert matching[0]["alert_id"] == alert["id"]


# --- MONEY CHANNEL SPLIT: money rules get their own Slack webhook ----------


def test_money_webhook_selection(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A money rule (payment_failures/roas_floor/revenue_drop) resolves
    MONEY_ALERT_SLACK_WEBHOOK_URL; every other rule (e.g. goal_limbo) still
    resolves the shared SLACK_WEBHOOK_URL unchanged.

    Fails against HEAD 87f3a6b622f00d797adaf95c6b297f1e1751bedc, where
    ``_notify`` calls ``send_slack(text)`` with no ``webhook_env`` at all, so
    every rule resolves the default env and this env-selection assertion
    cannot pass.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/shared")
    monkeypatch.setenv("MONEY_ALERT_SLACK_WEBHOOK_URL", "https://hooks.example/money")
    seen_envs: list[str] = []

    def fake_send_slack(text: str, *, webhook_env: str = "SLACK_WEBHOOK_URL") -> NotifyResult:
        seen_envs.append(webhook_env)
        return NotifyResult(True, "slack", "sent")

    monkeypatch.setattr(monitor, "send_slack", fake_send_slack)

    money_candidate = AlertCandidate(
        rule="payment_failures",
        severity="high",
        title="Payment failure burst",
        body="3 failures",
        evidence={"burst": 3},
        cooldown_key="payment_failures",
    )
    loop_candidate = AlertCandidate(
        rule="goal_limbo",
        severity="high",
        title="Board card stuck",
        body="stuck",
        evidence={"task_id": "t1"},
        cooldown_key="goal_limbo:owner:goal:t1",
    )
    assert monitor._persist_candidate(
        money_candidate, steward=steward, database=database, cfg=steward_config
    )
    assert monitor._persist_candidate(
        loop_candidate, steward=steward, database=database, cfg=steward_config
    )
    assert seen_envs == ["MONEY_ALERT_SLACK_WEBHOOK_URL", "SLACK_WEBHOOK_URL"]
    assert monitor.MONEY_RULES == {"payment_failures", "roas_floor", "revenue_drop"}


def test_goal_limbo_not_pushed(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """goal_limbo still persists a durable notification row linked to its
    alert, but is muted on the shared ntfy/OPS_ALERT_SLACK_WEBHOOK_URL push
    transport -- a money rule keeps push=True unchanged.

    Fails against HEAD, where goal_limbo takes the same push=True path as
    every other high-severity alert, so the assertion that its push transport
    was never invoked (while a money alert's push transport WAS) cannot pass.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/shared")
    monkeypatch.setenv("MONEY_ALERT_SLACK_WEBHOOK_URL", "https://hooks.example/money")
    monkeypatch.setattr(monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent"))

    pushed_kinds: list[str | None] = []

    def fake_push(title: str, body: str, **kwargs: Any) -> None:
        pushed_kinds.append(kwargs.get("group"))

    monkeypatch.setattr("omniagentos.notifications.service._push", fake_push)

    goal_limbo_candidate = AlertCandidate(
        rule="goal_limbo",
        severity="high",
        title="Board card stuck",
        body="stuck",
        evidence={"task_id": "t1"},
        cooldown_key="goal_limbo:owner:goal:t1",
    )
    money_candidate = AlertCandidate(
        rule="payment_failures",
        severity="high",
        title="Payment failure burst",
        body="3 failures",
        evidence={"burst": 3},
        cooldown_key="payment_failures",
    )
    assert monitor._persist_candidate(
        goal_limbo_candidate, steward=steward, database=database, cfg=steward_config
    )
    assert monitor._persist_candidate(
        money_candidate, steward=steward, database=database, cfg=steward_config
    )

    # goal_limbo's notification row is still persisted durably...
    goal_limbo_alert = next(a for a in steward.list_alerts() if a["rule"] == "goal_limbo")
    rows = database._connection.execute(
        "SELECT ref_id FROM notifications WHERE ref_type = 'alert'"
    ).fetchall()
    ref_ids = {str(row["ref_id"]) for row in rows}
    assert str(goal_limbo_alert["id"]) in ref_ids
    money_alert = next(a for a in steward.list_alerts() if a["rule"] == "payment_failures")
    assert str(money_alert["id"]) in ref_ids

    # ...but only the money rule's notification reached the push transport.
    assert not any(str(goal_limbo_alert["id"]) in (group or "") for group in pushed_kinds)
    assert any(str(money_alert["id"]) in (group or "") for group in pushed_kinds)


def test_money_webhook_unset_warns(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With MONEY_ALERT_SLACK_WEBHOOK_URL unset, a money alert still delivers
    via the shared SLACK_WEBHOOK_URL fallback AND the run stays loud about the
    split being unarmed: the alert row carries the unconfigured-channel
    warning even though delivery itself succeeded.

    Fails against HEAD, which has no concept of a money webhook and so never
    emits this warning when only the shared channel is configured.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/shared")
    monkeypatch.delenv("MONEY_ALERT_SLACK_WEBHOOK_URL", raising=False)
    sent_envs: list[str] = []

    def fake_send_slack(text: str, *, webhook_env: str = "SLACK_WEBHOOK_URL") -> NotifyResult:
        sent_envs.append(webhook_env)
        return NotifyResult(True, "slack", "sent")

    monkeypatch.setattr(monitor, "send_slack", fake_send_slack)

    money_candidate = AlertCandidate(
        rule="roas_floor",
        severity="critical",
        title="ROAS below floor",
        body="roas 0.5",
        evidence={"value": 0.5},
        cooldown_key="roas_floor",
    )
    assert monitor._persist_candidate(
        money_candidate, steward=steward, database=database, cfg=steward_config
    )
    # Delivery still succeeded via the shared-channel fallback...
    assert sent_envs == ["SLACK_WEBHOOK_URL"]
    alert = steward.list_alerts()[0]
    # ...but the unarmed money split is visibly flagged on the row.
    assert alert["evidence"].get("delivery_warning") == "no critical-capable channel configured"


def test_high_alert_with_only_email_is_flagged(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RR-PROD-002 residual: email only fires for CRITICAL, so a HIGH alert with
    # Slack unset + email configured reaches nobody and must still be flagged.
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    _seed_snapshot(steward, "stripe", "payment_failures", 5.0)  # payment_failure_burst -> HIGH
    cfg = steward_config.model_copy(
        update={"briefing": BriefingConfig(deliver_email="owner@example.com")}
    )
    monkeypatch.setattr(
        monitor, "send_slack", lambda text, **_kwargs: NotifyResult(False, "slack", "not configured")
    )

    monitor.monitor_once(database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path)
    highs = [a for a in steward.list_alerts("open") if a["severity"] == "high"]
    assert (
        highs
        and highs[0]["evidence"].get("delivery_warning") == "no critical-capable channel configured"
    )


def _mint_meta_spend_grant(database: SqliteStore) -> dict[str, Any]:
    """Insert a live meta capability grant the breaker should revoke.

    Inserts directly (bypassing create_grant's connector-registry pin) so this
    test does not depend on connectors.yaml being present under the per-test
    OMNIAGENTOS_VAR_DIR the breaker state file also uses.
    """
    from omniagentos.contracts import new_id, utc_now_iso

    grant_id = new_id("gnt")
    database._write(
        "INSERT INTO campaign_grants "
        "(id, created_at, label, capability, target_set_json, project_id, approval_id, "
        "plan_approval_state, max_actions, max_spend_usd, expires_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            utc_now_iso(),
            "spend-breaker-test",
            "meta_acmeuni.budget_change",
            "[]",
            None,
            "apr_spend_breaker_test",
            "approved",
            10,
            500.0,
            "2099-01-01T00:00:00+00:00",
            json.dumps({"generation": 0, "action_class": "consequential"}),
        ),
    )
    grant = GrantsStore(database).get_grant(grant_id)
    assert grant is not None
    return grant


def _mint_grant(database: SqliteStore, *, capability: str, label: str) -> dict[str, Any]:
    """Insert a live grant with an arbitrary capability (see _mint_meta_spend_grant)."""
    from omniagentos.contracts import new_id, utc_now_iso

    grant_id = new_id("gnt")
    database._write(
        "INSERT INTO campaign_grants "
        "(id, created_at, label, capability, target_set_json, project_id, approval_id, "
        "plan_approval_state, max_actions, max_spend_usd, expires_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            utc_now_iso(),
            label,
            capability,
            "[]",
            None,
            "apr_spend_breaker_test",
            "approved",
            10,
            500.0,
            "2099-01-01T00:00:00+00:00",
            json.dumps({"generation": 0, "action_class": "consequential"}),
        ),
    )
    grant = GrantsStore(database).get_grant(grant_id)
    assert grant is not None
    return grant


def test_revoke_spend_grants_matches_whole_tokens_not_bare_substrings(
    database: SqliteStore,
) -> None:
    """F3 regression: bare substring matching over-revoked unrelated capabilities.

    "metadata.read" contains "meta", "system.uploads" contains "ads", and
    "forum.threads.delete" contains "ads" ("thre-ads") -- none of them are
    spend/ads/meta capabilities, and a bare `token in capability` substring
    check wrongly matched all three. Whole-segment matching (split on "."/"_"/
    "-") must leave them alone while still catching real spend-path grants.
    """
    unrelated_read = _mint_grant(database, capability="metadata.read", label="unrelated-1")
    unrelated_uploads = _mint_grant(
        database, capability="system.uploads", label="unrelated-2"
    )
    unrelated_forum = _mint_grant(
        database, capability="forum.threads.delete", label="unrelated-3"
    )
    real_meta = _mint_grant(
        database, capability="meta_acmeuni.budget_change", label="real-meta"
    )
    real_ads = _mint_grant(database, capability="ads.pause_campaign", label="real-ads")

    revoked_ids = monitor._revoke_spend_grants(database, reason="spend circuit breaker: test")

    assert set(revoked_ids) == {real_meta["id"], real_ads["id"]}

    store = GrantsStore(database)
    assert not store.get_grant(unrelated_read["id"])["revoked_at"]
    assert not store.get_grant(unrelated_uploads["id"])["revoked_at"]
    assert not store.get_grant(unrelated_forum["id"])["revoked_at"]
    assert store.get_grant(real_meta["id"])["revoked_at"]
    assert store.get_grant(real_ads["id"])["revoked_at"]


def test_spend_breaker_trips_writes_state_and_revokes_grant_even_when_notify_fails(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmed spend spike trips the breaker independent of alert delivery."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    grant = _mint_meta_spend_grant(database)
    assert grant["revoked_at"] is None or grant.get("revoked_at") in (None, "")

    notify_calls: list[str] = []

    def failing_slack(text: str, **_kwargs: object) -> NotifyResult:
        notify_calls.append(text)
        return NotifyResult(False, "slack", "webhook down")

    monkeypatch.setattr(monitor, "send_slack", failing_slack)

    candidate = AlertCandidate(
        rule="spend_spike_intraday",
        severity="critical",
        title="Intraday advertising spend cap exceeded",
        body="Today's spend blew the absolute cap.",
        evidence={"metric": "spend_usd", "today": 900.0, "absolute_cap_usd": 100.0},
        cooldown_key="spend_spike_intraday",
        magnitude=800.0,
    )

    created = monitor._persist_candidate(
        candidate, steward=steward, database=database, cfg=steward_config
    )
    assert created is True
    assert notify_calls, "notify must still be attempted after the trip"

    state_path = tmp_path / "spend-breaker-state.json"
    assert state_path.exists(), "breaker state file must be written before/without notify success"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tripped"] is True
    assert state["rule"] == "spend_spike_intraday"
    assert "tripped_at" in state
    assert state["evidence"]["today"] == 900.0

    revoked = GrantsStore(database).get_grant(grant["id"])
    assert revoked is not None
    assert revoked["revoked_at"], "meta spend grant must be revoked by the breaker"
    assert "spend circuit breaker" in (revoked.get("revoke_reason") or "")

    typed = [
        row for row in database.get_events_after(0) if row.get("type") == "spend_breaker_tripped"
    ]
    assert typed, "trip must be durably recorded as an event"


def test_spend_breaker_trip_is_idempotent(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    grant = _mint_meta_spend_grant(database)
    monkeypatch.setattr(
        monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(False, "slack", "down")
    )
    candidate = AlertCandidate(
        rule="spend_spike",
        severity="high",
        title="Advertising spend spike detected",
        body="spike",
        evidence={"metric": "spend_usd", "today": 500.0},
        cooldown_key="spend_spike",
        magnitude=100.0,
    )
    assert monitor._persist_candidate(
        candidate, steward=steward, database=database, cfg=steward_config
    )
    # Second direct trip (simulating a re-fire) must not raise and must keep state tripped.
    monitor._trip_spend_breaker(candidate, database=database)
    state = json.loads((tmp_path / "spend-breaker-state.json").read_text(encoding="utf-8"))
    assert state["tripped"] is True
    assert GrantsStore(database).get_grant(grant["id"])["revoked_at"]


def test_write_breaker_state_is_atomic_no_partial_file_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 regression: the state file must never be observable half-written.

    ``_write_breaker_state`` writes to a temp file in the same directory and
    ``os.replace()``s it onto the final path. This asserts the on-disk
    behavior a concurrent chokepoint reader depends on: no leftover temp file
    after a successful write, and the final file is always fully valid JSON
    (never a partial write a reader could catch mid-flight).
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    candidate = AlertCandidate(
        rule="spend_spike_intraday",
        severity="critical",
        title="Intraday advertising spend cap exceeded",
        body="cap blown",
        evidence={"metric": "spend_usd", "today": 900.0},
        cooldown_key="spend_spike_intraday",
        magnitude=800.0,
    )
    result_path = monitor._write_breaker_state(candidate)
    assert result_path is not None
    assert result_path.exists()

    # No temp artifacts left in the directory -- os.replace() consumed it.
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".spend-breaker-state.")
    ]
    assert leftovers == [], f"leftover temp file(s) after atomic write: {leftovers}"

    # Final file is fully valid JSON, never a half-write.
    state = json.loads(result_path.read_text(encoding="utf-8"))
    assert state["tripped"] is True
    assert state["rule"] == "spend_spike_intraday"

    # Re-write (idempotent) must also leave no temp artifacts.
    monitor._write_breaker_state(candidate)
    leftovers_again = [
        p for p in tmp_path.iterdir() if p.name.startswith(".spend-breaker-state.")
    ]
    assert leftovers_again == []


def test_persist_candidate_does_not_retrip_when_breaker_already_tripped(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 (idempotency half): a candidate for an already-tripped breaker must
    not re-write state / re-attempt grant revoke every cycle -- only trip
    when transitioning from not-tripped to tripped (or re-arming after a
    clear; see test_persist_candidate_re_trips_after_reset_while_spike_persists).
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(
        monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent")
    )

    trip_calls: list[str] = []
    real_trip = monitor._trip_spend_breaker

    def counting_trip(candidate: AlertCandidate, *, database: SqliteStore) -> None:
        trip_calls.append(candidate.cooldown_key)
        real_trip(candidate, database=database)

    monkeypatch.setattr(monitor, "_trip_spend_breaker", counting_trip)

    candidate = AlertCandidate(
        rule="spend_spike_intraday",
        severity="critical",
        title="Intraday advertising spend cap exceeded",
        body="cap blown",
        evidence={"metric": "spend_usd", "today": 900.0},
        cooldown_key="spend_spike_intraday",
        magnitude=800.0,
    )

    # First candidate: not tripped yet -> trips.
    assert monitor._persist_candidate(
        candidate, steward=steward, database=database, cfg=steward_config
    )
    assert trip_calls == ["spend_spike_intraday"]

    # A second candidate for the SAME still-tripped breaker (simulating the
    # next monitor cycle while the spike persists) must not trip again.
    second_candidate = AlertCandidate(
        rule="spend_spike_intraday",
        severity="critical",
        title="Intraday advertising spend cap exceeded",
        body="cap still blown",
        evidence={"metric": "spend_usd", "today": 950.0},
        cooldown_key="spend_spike_intraday",
        magnitude=850.0,
    )
    monitor._persist_candidate(
        second_candidate, steward=steward, database=database, cfg=steward_config
    )
    assert trip_calls == ["spend_spike_intraday"], "must not re-trip while already tripped"


def test_persist_candidate_re_trips_after_reset_while_spike_persists(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 (re-arm half): the exact regression the reviewer flagged.

    create_alert cooldown-suppresses a same-cooldown_key candidate fired
    again inside cfg.alerts.cooldown_minutes (see steward_config fixture:
    cooldown_minutes=60) -- that suppression must NOT prevent the breaker
    from re-tripping if it was cleared (e.g. via the authenticated reset
    route) while the spike is still live.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(
        monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent")
    )
    grant = _mint_meta_spend_grant(database)

    candidate = AlertCandidate(
        rule="spend_spike",
        severity="high",
        title="Advertising spend spike detected",
        body="spike",
        evidence={"metric": "spend_usd", "today": 500.0},
        cooldown_key="spend_spike",
        magnitude=100.0,
    )

    # First firing: trips + creates the alert row.
    created_first = monitor._persist_candidate(
        candidate, steward=steward, database=database, cfg=steward_config
    )
    assert created_first is True
    state_path = tmp_path / "spend-breaker-state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["tripped"] is True
    assert GrantsStore(database).get_grant(grant["id"])["revoked_at"]

    # Operator (or an attacker who somehow got a token) clears the breaker
    # via the same mechanism api/routes/system.py's route uses.
    from omniagentos.api.middleware.chokepoint import clear_breaker_state

    assert clear_breaker_state() is True
    assert json.loads(state_path.read_text(encoding="utf-8"))["tripped"] is False

    # A NEW re-grant (as if the operator also un-revoked it, or a fresh grant
    # was minted) so we can observe the re-trip actually revokes it again.
    regrant = _mint_meta_spend_grant(database)

    # Second candidate, SAME cooldown_key, well within cooldown_minutes=60 ->
    # create_alert will suppress the alert row (this is the exact condition
    # that hid the re-arm gap: cooldown-suppressed but the spike is real).
    created_second = monitor._persist_candidate(
        candidate, steward=steward, database=database, cfg=steward_config
    )
    assert created_second is False, "sanity: cooldown really did suppress the alert row"

    # But the breaker must have RE-TRIPPED despite the suppressed alert.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tripped"] is True, "breaker must re-arm even when the alert itself is cooldown-suppressed"
    assert GrantsStore(database).get_grant(regrant["id"])["revoked_at"], (
        "re-trip must actually re-run enforcement (grant revoke), not just flip a flag"
    )


def test_monitor_once_re_trips_breaker_across_cycles_after_reset(
    database: SqliteStore,
    steward: StewardStore,
    steward_config: StewardConfig,
    triaged_marker_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end version via monitor_once (as the reviewer's VERIFY asked):
    a persistent spend spike survives a reset across two full monitor cycles.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(
        monitor, "send_slack", lambda _text, **_kwargs: NotifyResult(True, "slack", "sent")
    )
    cfg = steward_config.model_copy(
        update={
            "alerts": steward_config.alerts.model_copy(
                update={"spend_spike_absolute_cap_usd": 100.0}
            )
        }
    )
    _seed_snapshot(steward, "meta", "spend_usd", 900.0)

    monitor.monitor_once(
        database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
    )
    state_path = tmp_path / "spend-breaker-state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["tripped"] is True

    from omniagentos.api.middleware.chokepoint import clear_breaker_state

    assert clear_breaker_state() is True

    # Second cycle, spike still live (same snapshot data, well within
    # cooldown_minutes) -> must re-trip even though the alert row itself is
    # cooldown-suppressed.
    monitor.monitor_once(
        database, cfg=cfg, now=_now(), triaged_marker_path=triaged_marker_path
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["tripped"] is True


# --- FF-2: the breaker's revoke must not depend on an invariant -------------
#
# ``_revoke_spend_grants`` only reaches ``campaign_grants``. That was sufficient
# ONLY while "every ad-spend capability is consequential, so it can only be
# exercised through a revocable campaign grant" held. These tests construct the
# state where that invariant is broken -- a standing write-mode
# ``agent_capabilities`` grant on a spend capability, with NO campaign grant
# behind it -- and pin that a tripped breaker refuses it anyway.

_INVARIANT_BREAK_REGISTRY = """
version: 1
groups:
  ads:       { label: "Advertising", danger: true }
  analytics: { label: "Analytics",   danger: false }
connectors:
  meta_future:
    label: "Ad surface declared OUTSIDE the danger group"
    group: analytics
    env: []
    capabilities:
      meta_future.spend_write:
        label: "Change ad spend"
        action_class: internal_reversible
        http:
          base_url: "https://ads.invalid"
          methods: [POST]
          path_prefixes: [/spend]
  meta_reports:
    label: "Ad reporting"
    group: ads
    env: []
    capabilities:
      meta_reports.read:
        label: "Read ad performance"
        action_class: read_only
        http:
          base_url: "https://ads.invalid"
          methods: [GET]
          path_prefixes: [/insights]
"""

_SPEND_WRITE_CAP = "meta_future.spend_write"
_ADS_READ_CAP = "meta_reports.read"


@pytest.fixture
def invariant_break_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A registry where an ad-spend WRITE is declared with a soft action class.

    This is the invariant break itself, expressed as configuration: the
    capability spends ad budget but is neither ``consequential`` (so nothing
    forces it through a campaign grant) nor in a ``danger`` group (so the
    broker's separate danger-write hard-stop does not catch it either). It is
    exactly the future the breaker must survive.

    ``load_registry`` is ``lru_cache``d on its (defaulted) path argument, so the
    cache is cleared on BOTH sides -- entering with the session registry cached
    would ignore this file, and leaving it cached would hand this fixture's
    registry to unrelated tests.
    """
    from omniagentos.connectors import load_registry

    # Both names, never one: conftest's isolation fixture documents why a
    # partial override silently reads the registry from the other root.
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    (tmp_path / "connectors.yaml").write_text(_INVARIANT_BREAK_REGISTRY, encoding="utf-8")
    load_registry.cache_clear()
    try:
        yield tmp_path
    finally:
        load_registry.cache_clear()


def _grant_standing_capability(
    database: SqliteStore,
    *,
    holder: str,
    capability: str,
    mode: str,
) -> None:
    """Issue a STANDING ``agent_capabilities`` grant (no campaign grant behind it).

    Inserted directly rather than through ``CapabilityStore.issue_scoped_grant``
    so the row can carry a mode the issuing API would refuse to write, which is
    the point: the breaker must cope with rows it did not issue.
    """
    from omniagentos.contracts import utc_now_iso

    now = utc_now_iso()
    database._write(
        "INSERT OR IGNORE INTO agents "
        "(id, name, lineage, model, expertise_json, trust_level, status, created_at, updated_at) "
        "VALUES (?, ?, 'test', NULL, '[]', 'T3', 'idle', ?, ?)",
        (holder, holder, now, now),
    )
    database._write(
        "INSERT INTO agent_capabilities "
        "(agent_id, capability_id, granted_at, granted_by, note, mode, expires_at, "
        "issued_by, request_id, project_id) "
        "VALUES (?, ?, ?, 'operator', 'standing grant', ?, NULL, 'operator', NULL, NULL)",
        (holder, capability, now, mode),
    )


def _spend_spike_candidate() -> AlertCandidate:
    return AlertCandidate(
        rule="spend_spike_intraday",
        severity="critical",
        title="Intraday advertising spend cap exceeded",
        body="Today's spend blew the absolute cap.",
        evidence={"metric": "spend_usd", "today": 900.0, "absolute_cap_usd": 100.0},
        cooldown_key="spend_spike_intraday",
        magnitude=800.0,
    )


def test_tripped_breaker_refuses_ad_spend_write_held_only_by_a_standing_grant(
    database: SqliteStore,
    invariant_break_registry: Path,
) -> None:
    """FF-2 acceptance: the breaker's revoke must be invariant-INDEPENDENT.

    The holder's ONLY authorization is a standing ``agent_capabilities`` row --
    there is no campaign grant anywhere in this test, so the campaign-grants-only
    revoke has nothing to bite on. Before the fix the tripped breaker left this
    grant fully live and the ad-spend write was admitted.
    """
    from omniagentos.connectors import broker
    from omniagentos.connectors.store import CapabilityStore

    holder = "agt_ff2_spender"
    _grant_standing_capability(
        database, holder=holder, capability=_SPEND_WRITE_CAP, mode="write"
    )
    capabilities = CapabilityStore(database)

    # Precondition: this standing grant really does authorize the ad-spend write.
    admitted = broker.authorize_with_grant(
        _SPEND_WRITE_CAP,
        None,
        capabilities,
        grant_holder=holder,
        method="POST",
        path="/spend",
    )
    assert admitted.id == _SPEND_WRITE_CAP

    monitor._trip_spend_breaker(_spend_spike_candidate(), database=database)

    with pytest.raises(broker.BrokerDenied) as denial:
        broker.authorize_with_grant(
            _SPEND_WRITE_CAP,
            None,
            capabilities,
            grant_holder=holder,
            method="POST",
            path="/spend",
        )
    assert denial.value.reason == "not_granted"
    assert capabilities.get_grant(holder) == []


def test_breaker_deletes_the_standing_grant_rather_than_expiring_it(
    database: SqliteStore,
    invariant_break_registry: Path,
) -> None:
    """Expiry is not enforced on the standing path, so only deletion suffices.

    ``broker.authorize`` checks ``expires_at``/``mode`` only when a caller names
    an ``agent_id``; ``CapabilityStore.get_grant`` (the holder path's source of
    truth) filters on neither. A breaker that merely stamped an expiry would
    still leave the capability authorizing calls.
    """
    from omniagentos.connectors.store import CapabilityStore

    holder = "agt_ff2_spender"
    _grant_standing_capability(
        database, holder=holder, capability=_SPEND_WRITE_CAP, mode="write"
    )

    monitor._trip_spend_breaker(_spend_spike_candidate(), database=database)

    assert CapabilityStore(database).get_grant_row(holder, _SPEND_WRITE_CAP) is None
    rows = database._connection.execute(
        "SELECT COUNT(*) AS n FROM agent_capabilities WHERE agent_id = ?", (holder,)
    ).fetchone()
    assert dict(rows)["n"] == 0


def test_standing_revoke_is_recorded_in_the_append_only_grant_log(
    database: SqliteStore,
    invariant_break_registry: Path,
) -> None:
    """The row is destroyed, so the audit log is the only trace it existed."""
    from omniagentos.connectors.store import CapabilityStore

    holder = "agt_ff2_spender"
    _grant_standing_capability(
        database, holder=holder, capability=_SPEND_WRITE_CAP, mode="write"
    )

    monitor._trip_spend_breaker(_spend_spike_candidate(), database=database)

    logged = [
        entry
        for entry in CapabilityStore(database).grant_log(holder)
        if entry["action"] == "revoke"
    ]
    assert len(logged) == 1
    assert logged[0]["capability_id"] == _SPEND_WRITE_CAP
    assert logged[0]["actor"] == "spend-breaker"
    assert "spend circuit breaker" in str(logged[0]["note"])
    # The lifecycle snapshot is what makes a deliberate reissue possible later.
    assert logged[0]["mode"] == "write"

    typed = [
        row for row in database.get_events_after(0) if row.get("type") == "spend_breaker_tripped"
    ]
    assert typed, "trip must still be durably recorded as an event"
    payload = json.loads(typed[-1]["payload_json"])
    assert f"{holder}::{_SPEND_WRITE_CAP}" in payload["revoked_standing_grants"]


def test_read_mode_is_only_trusted_when_the_capability_cannot_write(
    database: SqliteStore,
    invariant_break_registry: Path,
) -> None:
    """Fail-CLOSED sparing rule, and its over-revocation guard.

    ``mode='read'`` is a statement of intent that the standing broker path never
    enforces, so it spares a grant only when the capability's own method
    allowlist proves the same thing. An ads READ capability survives the trip
    (revoking it would blind the operator mid-incident for no safety gain); a
    write-capable one does not, whatever its mode column says.
    """
    from omniagentos.connectors.store import CapabilityStore

    _grant_standing_capability(
        database, holder="agt_ff2_reader", capability=_ADS_READ_CAP, mode="read"
    )
    _grant_standing_capability(
        database, holder="agt_ff2_mislabelled", capability=_SPEND_WRITE_CAP, mode="read"
    )
    _grant_standing_capability(
        database, holder="agt_ff2_blank_mode", capability=_SPEND_WRITE_CAP, mode=""
    )
    _grant_standing_capability(
        database, holder="agt_ff2_unrelated", capability="forum.threads.delete", mode="write"
    )

    monitor._trip_spend_breaker(_spend_spike_candidate(), database=database)

    capabilities = CapabilityStore(database)
    assert capabilities.get_grant("agt_ff2_reader") == [_ADS_READ_CAP]
    assert capabilities.get_grant("agt_ff2_mislabelled") == []
    assert capabilities.get_grant("agt_ff2_blank_mode") == []
    assert capabilities.get_grant("agt_ff2_unrelated") == ["forum.threads.delete"]


def test_standing_revoke_is_idempotent_and_survives_an_unreadable_registry(
    database: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registry at all must not spare a spend grant, and re-tripping is safe.

    ``OMNIAGENTOS_VAR_DIR`` here has no ``connectors.yaml``, so every registry
    lookup raises. The read-only proof therefore cannot be obtained -- and the
    grant must be revoked rather than admitted on a failed check.
    """
    from omniagentos.connectors import load_registry
    from omniagentos.connectors.store import CapabilityStore

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    load_registry.cache_clear()
    try:
        _grant_standing_capability(
            database, holder="agt_ff2_reader", capability=_ADS_READ_CAP, mode="read"
        )
        candidate = _spend_spike_candidate()
        monitor._trip_spend_breaker(candidate, database=database)
        assert CapabilityStore(database).get_grant("agt_ff2_reader") == []
        # Second trip for the same incident: nothing left to take, no raise.
        monitor._trip_spend_breaker(candidate, database=database)
        assert json.loads(
            (tmp_path / "spend-breaker-state.json").read_text(encoding="utf-8")
        )["tripped"] is True
    finally:
        load_registry.cache_clear()
