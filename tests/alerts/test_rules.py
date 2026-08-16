from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omniagentos.steward.alerts.rules import (
    borderline_urgent,
    payment_failure_burst,
    reliability_deadman,
    revenue_drop,
    roas_floor,
    spend_spike,
    spend_spike_intraday,
    vip_urgent,
)
from omniagentos.steward.config import AlertsConfig


def _snapshot(
    metric: str, value: float, captured_at: datetime, *, row_id: int = 1
) -> dict[str, object]:
    return {
        "id": row_id,
        "metric": metric,
        "value": value,
        "captured_at": captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _watch_row(
    *,
    updated_at: datetime,
    first_seen: datetime | None = None,
    value_json: str | None = None,
) -> dict[str, object]:
    if value_json is None:
        first = first_seen or updated_at
        value_json = (
            '{"cursor":"'
            + updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            + '","first_seen":"'
            + first.strftime("%Y-%m-%dT%H:%M:%SZ")
            + '"}'
        )
    return {
        "key": "watch_cursor",
        "updated_at": updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value_json": value_json,
    }


def test_reliability_deadman_handles_never_run_audit_without_crashing() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    watch = _watch_row(
        updated_at=now - timedelta(minutes=5),
        first_seen=now - timedelta(hours=15),
    )

    candidates = reliability_deadman(watch, None, now)

    audit = next(c for c in candidates if c.cooldown_key == "reliability_deadman_audit")
    assert audit.severity == "critical"
    assert audit.evidence["last_audit_id"] is None
    assert audit.evidence["last_audit_started_at"] is None
    assert audit.evidence["state"] == "never_run"


def test_reliability_deadman_distinguishes_current_and_stale_watch() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    current = _watch_row(updated_at=now - timedelta(minutes=5))
    audit = {
        "id": "aud_current",
        "started_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert reliability_deadman(current, audit, now) == []

    stale = _watch_row(updated_at=now - timedelta(hours=2))
    candidates = reliability_deadman(stale, audit, now)
    watch = next(c for c in candidates if c.cooldown_key == "reliability_deadman_watch")
    assert watch.evidence["state"] == "stale"
    assert watch.evidence["stale_minutes"] >= 120


def test_reliability_deadman_reports_corrupt_watch_state() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    corrupt = _watch_row(
        updated_at=now - timedelta(minutes=5),
        value_json="{not-json",
    )

    candidates = reliability_deadman(corrupt, None, now)

    state = next(c for c in candidates if c.cooldown_key == "reliability_deadman_watch_state")
    assert state.severity == "critical"
    assert state.evidence["state"] == "corrupt"


def test_reliability_deadman_reports_corrupt_audit_state() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    current = _watch_row(updated_at=now - timedelta(minutes=5))

    candidates = reliability_deadman(
        current,
        {"id": "aud_bad", "started_at": "not-a-timestamp"},
        now,
    )

    state = next(c for c in candidates if c.cooldown_key == "reliability_deadman_audit_state")
    assert state.severity == "critical"
    assert state.evidence == {
        "state": "corrupt",
        "last_audit_id": "aud_bad",
        "last_audit_started_at": "not-a-timestamp",
    }


def test_reliability_deadman_reports_store_down_state() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)

    candidates = reliability_deadman(
        {"_state": "store_error", "error_type": "OperationalError"},
        {"_state": "store_error", "error_type": "OperationalError"},
        now,
    )

    assert len(candidates) == 1
    assert candidates[0].cooldown_key == "reliability_deadman_store"
    assert candidates[0].evidence == {
        "state": "unavailable",
        "components": ["audit", "watch"],
        "error_types": ["OperationalError"],
    }


def test_reliability_deadman_does_not_mislabel_failed_audit_read_as_never_run() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    current = _watch_row(
        updated_at=now - timedelta(minutes=5),
        first_seen=now - timedelta(days=2),
    )

    candidates = reliability_deadman(
        current,
        {"_state": "store_error", "error_type": "OperationalError"},
        now,
    )

    assert [candidate.cooldown_key for candidate in candidates] == ["reliability_deadman_store"]
    assert candidates[0].evidence["components"] == ["audit"]


def test_roas_floor_triggers_and_clears_with_critical_cooldown(alerts_config: AlertsConfig) -> None:
    now = datetime.now(UTC)
    triggered = roas_floor([_snapshot("roas", 0.99, now)], alerts_config)

    assert len(triggered) == 1
    assert triggered[0].severity == "critical"
    assert triggered[0].cooldown_key == "roas_floor"
    assert roas_floor([_snapshot("roas", 1.0, now)], alerts_config) == []


def test_roas_floor_magnitude_is_the_shortfall_below_floor(alerts_config: AlertsConfig) -> None:
    now = datetime.now(UTC)
    mild = roas_floor([_snapshot("roas", 0.9, now)], alerts_config)[0]
    severe = roas_floor([_snapshot("roas", 0.1, now)], alerts_config)[0]

    assert mild.magnitude == alerts_config.roas_floor - 0.9
    assert severe.magnitude == alerts_config.roas_floor - 0.1
    assert severe.magnitude > mild.magnitude  # a worse ROAS crash is a bigger magnitude


def test_spend_spike_needs_three_prior_days_and_respects_threshold(
    alerts_config: AlertsConfig,
) -> None:
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    sparse = [
        _snapshot("spend_usd", 200, today),
        _snapshot("spend_usd", 100, today - timedelta(days=1)),
        _snapshot("spend_usd", 100, today - timedelta(days=2)),
    ]

    assert spend_spike(sparse, alerts_config) == []

    triggered = spend_spike(
        [*sparse, _snapshot("spend_usd", 100, today - timedelta(days=3))], alerts_config
    )
    assert len(triggered) == 1
    assert triggered[0].severity == "high"
    assert triggered[0].cooldown_key == "spend_spike"

    at_threshold = [
        _snapshot("spend_usd", 150, today),
        *[_snapshot("spend_usd", 100, today - timedelta(days=day)) for day in (1, 2, 3)],
    ]
    assert spend_spike(at_threshold, alerts_config) == []

    assert triggered[0].magnitude == 200 - 100  # excess spend over the prior-day average


def test_spend_spike_intraday_field_is_declared_on_the_real_config_schema() -> None:
    """F1 regression: the cap field must survive Pydantic validation.

    getattr(cfg, "spend_spike_absolute_cap_usd", None) reads whatever attribute
    is ACTUALLY on the validated model -- if the field is undeclared, Pydantic
    silently drops it from any YAML-loaded config and this always reads None in
    production, even though a SimpleNamespace-based test would look green. This
    asserts against the real AlertsConfig model (not a stand-in) so a future
    Pydantic-schema regression fails loudly here instead of shipping dead.
    """
    assert "spend_spike_absolute_cap_usd" in AlertsConfig.model_fields
    assert AlertsConfig().spend_spike_absolute_cap_usd is None
    configured = AlertsConfig(spend_spike_absolute_cap_usd=500.0)
    assert configured.spend_spike_absolute_cap_usd == 500.0
    # Round-trips through the same validation a real YAML load goes through.
    round_tripped = AlertsConfig.model_validate(configured.model_dump())
    assert round_tripped.spend_spike_absolute_cap_usd == 500.0


def test_spend_spike_intraday_fires_on_single_day_when_absolute_cap_exceeded() -> None:
    """Absolute-cap rule needs no prior-day baseline — day-1 runaway spend trips it.

    Uses REAL, Pydantic-validated ``AlertsConfig`` instances throughout (no
    SimpleNamespace) — see test_spend_spike_intraday_field_is_declared_on_the_
    real_config_schema for why a stand-in object would hide a dead-by-default
    schema bug.
    """
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    single_day = [_snapshot("spend_usd", 750.0, today)]

    # Cap unset on a real, validated config → never fires (safe default; no
    # surprise trips just because the field exists on the model).
    assert spend_spike_intraday(single_day, AlertsConfig()) == []

    # Under cap → no candidate.
    under = AlertsConfig(spend_spike_absolute_cap_usd=1000.0)
    assert spend_spike_intraday(single_day, under) == []

    # Cap exceeded with only one day of data → critical, own cooldown key.
    over = AlertsConfig(spend_spike_absolute_cap_usd=500.0)
    triggered = spend_spike_intraday(single_day, over)
    assert len(triggered) == 1
    assert triggered[0].rule == "spend_spike_intraday"
    assert triggered[0].severity == "critical"
    assert triggered[0].cooldown_key == "spend_spike_intraday"
    assert triggered[0].evidence["today"] == 750.0
    assert triggered[0].evidence["absolute_cap_usd"] == 500.0
    assert triggered[0].magnitude == 250.0

    # Independent of baseline spend_spike: single day still cannot trip spend_spike.
    assert spend_spike(single_day, AlertsConfig(spend_spike_pct=50.0)) == []


def test_payment_failure_burst_triggers_and_clears_with_high_cooldown(
    alerts_config: AlertsConfig,
) -> None:
    now = datetime.now(UTC)
    triggered = payment_failure_burst([_snapshot("payment_failures", 3, now)], alerts_config)

    assert len(triggered) == 1
    assert triggered[0].severity == "high"
    assert triggered[0].cooldown_key == "payment_failures"
    assert triggered[0].magnitude == 3  # magnitude is the failure count itself
    assert payment_failure_burst([_snapshot("payment_failures", 2, now)], alerts_config) == []


def test_vip_urgent_triggers_only_for_vips_and_uses_critical_cooldown(
    alerts_config: AlertsConfig,
) -> None:
    vip = {
        "id": 11,
        "source": "mail",
        "sender": "VIP@example.com",
        "subject": "URGENT",
        "body_text": "Act",
    }
    non_vip = {
        "id": 12,
        "source": "mail",
        "sender": "other@example.com",
        "subject": "urgent",
        "body_text": "Act",
    }

    triggered = vip_urgent([vip], alerts_config)

    assert len(triggered) == 1
    assert triggered[0].severity == "critical"
    assert triggered[0].cooldown_key == "vip:vip@example.com"
    assert vip_urgent([non_vip], alerts_config) == []


def test_borderline_urgent_triggers_only_for_non_vips_and_uses_triage_cooldown(
    alerts_config: AlertsConfig,
) -> None:
    non_vip = {
        "id": 21,
        "source": "mail",
        "sender": "other@example.com",
        "subject": "urgent",
        "body_text": "Act",
    }
    vip = {
        "id": 22,
        "source": "mail",
        "sender": "vip@example.com",
        "subject": "urgent",
        "body_text": "Act",
    }
    no_pattern = {
        "id": 23,
        "source": "mail",
        "sender": "other@example.com",
        "subject": "hello",
        "body_text": "Act",
    }

    triggered = borderline_urgent([non_vip], alerts_config)

    assert len(triggered) == 1
    assert triggered[0].severity == "triage"
    assert triggered[0].cooldown_key == "triage:21"
    assert triggered[0].message == non_vip
    assert borderline_urgent([vip, no_pattern], alerts_config) == []


def test_revenue_drop_fires_on_floor_day_one_no_history_required(
    alerts_config: AlertsConfig,
) -> None:
    now = datetime.now(UTC)

    triggered = revenue_drop([_snapshot("net_revenue_usd", 0.0, now)], alerts_config)

    assert len(triggered) == 1
    assert triggered[0].rule == "revenue_drop"
    assert triggered[0].severity == "critical"
    assert triggered[0].cooldown_key == "revenue_drop"


def test_revenue_drop_does_not_fire_on_healthy_revenue_with_no_baseline(
    alerts_config: AlertsConfig,
) -> None:
    now = datetime.now(UTC)

    assert revenue_drop([_snapshot("net_revenue_usd", 500.0, now)], alerts_config) == []


def test_revenue_drop_needs_baseline_before_pct_rule_can_fire(alerts_config: AlertsConfig) -> None:
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    # Only 1 prior day: below _MIN_REVENUE_BASELINE_DAYS(2) -- pct rule must not fire
    # even though the drop (70%) would otherwise clear the threshold, and the
    # floor (default 0.0) isn't breached by a positive $30 reading.
    sparse = [
        _snapshot("net_revenue_usd", 30.0, today),
        _snapshot("net_revenue_usd", 100.0, today - timedelta(days=1)),
    ]
    assert revenue_drop(sparse, alerts_config) == []

    triggered = revenue_drop(
        [*sparse, _snapshot("net_revenue_usd", 100.0, today - timedelta(days=2))], alerts_config
    )
    assert len(triggered) == 1
    assert triggered[0].severity == "critical"
    assert triggered[0].evidence["pct_triggered"] is True


def test_revenue_drop_magnitude_is_the_drop_amount(alerts_config: AlertsConfig) -> None:
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [
        _snapshot("net_revenue_usd", 30.0, today),
        *[_snapshot("net_revenue_usd", 100.0, today - timedelta(days=day)) for day in (1, 2, 3)],
    ]

    triggered = revenue_drop(rows, alerts_config)

    assert len(triggered) == 1
    assert triggered[0].magnitude == 100.0 - 30.0
