"""Fail-loud contract: the revenue collector must never exit 0 with dead sources.

Before this lane, every source could fail every day (Stripe unconfigured, NMI
bad shape, fanbasis 301, Meta empty) and ``collect_day``/``main`` still
reported success — 8+ consecutive days of that went unnoticed because nothing
converted persistent data-quality failure into an alert or a failing
health-sentinel check. These tests are the regression: a source that stops
producing real facts must turn the run RED, fire exactly one alert per bad
day, and trip ``health_sentinel.check_revenue`` independent of the
collector's own exit code.

Fully offline — the broker and Slack notifier are both mocked. No test may
touch the live network.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from omniagentos.revenue import collect
from omniagentos.revenue.collect import collect_day
from omniagentos.revenue.store import RevenueStore

_HEALTH_SENTINEL_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "health_sentinel.py"
)


def _load_health_sentinel() -> Any:
    """Standalone script (stdlib + sqlite3, no package import) -- load by path,
    exactly the way launchd runs it and the way every other health-sentinel
    test in this repo loads it (see tests/scripts/test_health_sentinel.py).
    """
    name = "health_sentinel_failloud_under_test"
    spec = importlib.util.spec_from_file_location(name, _HEALTH_SENTINEL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sentinel = _load_health_sentinel()

_ALL_CONFIG_ENVS = (
    "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
    "INITECH_STRIPE_PRIMARY_SECRET_KEY",
    "ACMEUNI_META_ACCESS_TOKEN",
    "ACMEUNI_META_AD_ACCOUNT_IDS",
    "INITECH_META_ACCESS_TOKEN",
    "INITECH_META_AD_ACCOUNT_IDS",
    "STRIPE_PRIMARY_SECRET_KEY",
    "STRIPE_SECONDARY_SECRET_KEY",
    "GLACIER_NMI_SECURITY_KEY",
    "FORTPOINT_NMI_SECURITY_KEY",
    "FANBASIS_API_KEY",
    "SLACK_WEBHOOK_URL",
)


def _clear_all_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee a fully unconfigured environment regardless of ambient env --
    every credential env name every source in collect.py reads (see
    _GLOBAL_STRIPE_ACCOUNTS, the NMI loop, fanbasis, and each Brand)."""
    for env_name in _ALL_CONFIG_ENVS:
        monkeypatch.delenv(env_name, raising=False)


def _configure_all_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brand Stripe + Meta configured; global Stripe/NMI/fanbasis stay
    unconfigured on purpose -- these two streak tests are about ONE brand
    source failing while others stay healthy, not about every source."""
    _clear_all_config(monkeypatch)
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_test_acmeuni")
    monkeypatch.setenv("INITECH_STRIPE_PRIMARY_SECRET_KEY", "sk_test_omni")
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "tok_acmeuni")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "111")
    monkeypatch.setenv("INITECH_META_ACCESS_TOKEN", "tok_omni")
    monkeypatch.setenv("INITECH_META_AD_ACCOUNT_IDS", "333")


def _stripe_body() -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [{"id": "ch1", "status": "succeeded", "amount": 10000, "amount_refunded": 0}],
            "has_more": False,
        },
    }


def _meta_body(account_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [
                {
                    "campaign_id": f"cmp_{account_id}",
                    "campaign_name": "Campaign A",
                    "spend": "10.00",
                    "purchase_roas": [{"value": "2.0"}],
                }
            ]
        },
    }


class _CapturedSlack:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str, **_kwargs: Any) -> Any:
        self.calls.append(text)
        from omniagentos.steward.notify import NotifyResult

        return NotifyResult(True, "slack", "sent")


# --- Immediate floor breach: zero effective facts on the FIRST attempted day ---


def test_all_sources_unconfigured_flips_red_on_first_run_not_just_the_streak(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every source unconfigured (the actual 8-day incident) must go RED
    immediately -- a zero-fact run must never wait for a multi-day streak to
    become visible, or the collector stays silently green for days again."""
    _clear_all_config(monkeypatch)
    slack = _CapturedSlack()
    monkeypatch.setattr(collect, "send_slack", slack)

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert result.facts == []
    assert result.outcome == "red"
    assert result.alert is not None
    assert result.alert["sent"] is True
    assert len(slack.calls) == 1


def test_red_outcome_makes_main_exit_nonzero(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original defect: every source failing still exited 0. Pin main()'s
    exit code directly, not just collect_day's outcome field."""
    _clear_all_config(monkeypatch)
    monkeypatch.setattr(collect, "send_slack", _CapturedSlack())
    monkeypatch.setattr(collect, "default_db_path", lambda: store._db_path)

    rc = collect.main(["--once", "--date", "2026-07-10"])

    assert rc == 1


def test_dry_run_never_flips_red_or_alerts(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview run must not mutate the persisted streak or page anyone."""
    _clear_all_config(monkeypatch)
    slack = _CapturedSlack()
    monkeypatch.setattr(collect, "send_slack", slack)

    result = collect_day(store, target_day=date(2026, 7, 10), dry_run=True)

    assert result.outcome == "ok"
    assert result.alert is None
    assert slack.calls == []
    assert RevenueStore(store).source_status_snapshot() == []


# --- Consecutive-day streak (a source failing without an overall floor breach) ---


def test_one_failing_source_among_healthy_ones_flips_red_only_after_the_streak(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AcmeUni Stripe fails two days running while every other source stays
    healthy (so the floor never breaches -- facts are written every day).
    Day 1 must stay 'ok' (streak=1 < threshold=2); day 2 must go RED from the
    streak alone, with exactly one alert fired."""
    _configure_all_sources(monkeypatch)
    slack = _CapturedSlack()
    monkeypatch.setattr(collect, "send_slack", slack)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/v1/charges" and cap == "stripe_acmeuni.read":
            raise RuntimeError("simulated Stripe outage")
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        account_id = kwargs["path"].split("/")[1].removeprefix("act_")
        return _meta_body(account_id)

    monkeypatch.setattr(collect.broker, "call", fake_call)

    day1 = collect_day(store, target_day=date(2026, 7, 10))
    assert day1.outcome == "ok", "single-day failure must not exceed the threshold"
    assert day1.alert is None
    assert day1.facts, "healthy sources must still have written real facts"

    day2 = collect_day(store, target_day=date(2026, 7, 11))
    assert day2.outcome == "red"
    assert day2.alert is not None
    assert day2.alert["sent"] is True
    assert "AcmeUni:stripe" in day2.alert["red_sources"]

    assert len(slack.calls) == 1

    snapshot = {row["status_key"]: row for row in RevenueStore(store).source_status_snapshot()}
    assert snapshot["AcmeUni:stripe"]["consecutive_failures"] == 2
    assert snapshot["AcmeUni:stripe"]["last_status"] == "failed"
    # A healthy source's streak must stay at zero, not get swept up by the
    # other source's failure.
    assert snapshot["Initech:stripe"]["consecutive_failures"] == 0


def test_same_day_rerun_does_not_double_alert(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hourly re-collect that stays RED all day must fire the alert once,
    not once per pass -- backs claim_daily_alert's once-per-day contract."""
    _clear_all_config(monkeypatch)
    slack = _CapturedSlack()
    monkeypatch.setattr(collect, "send_slack", slack)

    collect_day(store, target_day=date(2026, 7, 10))
    collect_day(store, target_day=date(2026, 7, 10))
    collect_day(store, target_day=date(2026, 7, 10))

    assert len(slack.calls) == 1


def test_recovery_resets_the_streak_to_zero(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that comes back must not keep counting toward a stale streak.

    Every other source in this fixture (global Stripe/NMI/fanbasis) stays
    permanently unconfigured for both days, so their own streaks only ever
    grow -- this test isolates the claim to AcmeUni:stripe specifically, which
    is the one source that recovers on day 2.
    """
    _configure_all_sources(monkeypatch)

    def failing(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/v1/charges" and cap == "stripe_acmeuni.read":
            raise RuntimeError("simulated Stripe outage")
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        account_id = kwargs["path"].split("/")[1].removeprefix("act_")
        return _meta_body(account_id)

    def healthy(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        account_id = kwargs["path"].split("/")[1].removeprefix("act_")
        return _meta_body(account_id)

    monkeypatch.setattr(collect.broker, "call", failing)
    collect_day(store, target_day=date(2026, 7, 10))
    day1_snapshot = {row["status_key"]: row for row in RevenueStore(store).source_status_snapshot()}
    assert day1_snapshot["AcmeUni:stripe"]["consecutive_failures"] == 1

    monkeypatch.setattr(collect.broker, "call", healthy)
    collect_day(store, target_day=date(2026, 7, 11))

    snapshot = {row["status_key"]: row for row in RevenueStore(store).source_status_snapshot()}
    assert snapshot["AcmeUni:stripe"]["consecutive_failures"] == 0
    assert snapshot["AcmeUni:stripe"]["last_status"] == "ok"


# --- health-sentinel: the second, exit-code-independent half of the contract ---


def test_health_sentinel_check_revenue_fails_on_a_persisted_streak(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_revenue reads revenue_source_status directly by raw SQL (never
    importing the omniagentos package) and must FAIL once a streak reaches
    the threshold, independent of whatever the collector's own exit code
    was on a given run."""
    # record_source_outcome computes the streak from whatever the previous row
    # said, so seed it via two real calls in chronological order.
    revenue_store = RevenueStore(store)
    revenue_store.record_source_outcome(
        day="2026-07-10", vertical="AcmeUni", source="stripe", status="failed", message="boom"
    )
    revenue_store.record_source_outcome(
        day="2026-07-11", vertical="AcmeUni", source="stripe", status="failed", message="boom"
    )
    revenue_store.record_source_outcome(
        day="2026-07-11", vertical="Initech", source="stripe", status="ok", message="collected"
    )

    monkeypatch.setenv("OMNIAGENTOS_DB", str(store._db_path))

    result = sentinel.check_revenue()

    assert result.status == sentinel.FAIL
    assert "AcmeUni:stripe" in result.evidence
    assert any(row["status_key"] == "AcmeUni:stripe" for row in result.detail["sources"])


def test_health_sentinel_check_revenue_ok_when_all_sources_healthy(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    RevenueStore(store).record_source_outcome(
        day="2026-07-10", vertical="AcmeUni", source="stripe", status="ok", message="collected"
    )
    monkeypatch.setenv("OMNIAGENTOS_DB", str(store._db_path))

    result = sentinel.check_revenue()

    assert result.status == sentinel.OK


def test_health_sentinel_check_revenue_warns_when_table_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No revenue collection has ever run against this DB yet -- must WARN,
    never FAIL (a table that legitimately doesn't exist yet is not a data
    quality incident) and never crash the whole sentinel run."""
    import sqlite3

    empty_db = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty_db).close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(empty_db))

    result = sentinel.check_revenue()

    assert result.status == sentinel.WARN


def test_health_sentinel_registers_the_revenue_check() -> None:
    names = [name for name, _fn in sentinel.CHECKS]
    assert "revenue" in names
    assert dict(sentinel.CHECKS)["revenue"] is sentinel.check_revenue
