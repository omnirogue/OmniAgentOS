from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omniagentos.adapters.common import normalize_provider_cost
from omniagentos.adapters.kimi_k3_api import (
    FireworksKimiK3Adapter,
    MoonshotKimiK3Adapter,
    ProviderAuthRefusal,
)
from omniagentos.adapters.spend_db import (
    PRODUCTION_SPEND_DB,
    SpendDbResolutionError,
    resolve_spend_db_path,
)
from omniagentos.adapters.spend_guard import SpendGuard, SpendGuardRefusal
from omniagentos.contracts import AgentInput, BudgetSpec
from omniagentos.db.busy import BusyRetryExhausted
from omniagentos.db.store import SqliteStore

DAY = "2026-08-05"
NOW = "2026-08-05T12:00:00Z"


def test_spend_db_resolver_has_one_env_override_and_fixed_default(tmp_path: Path) -> None:
    assert resolve_spend_db_path(environ={}) == PRODUCTION_SPEND_DB
    override = tmp_path / "spend.sqlite3"
    assert resolve_spend_db_path(environ={"OMNIAGENTOS_SPEND_DB": str(override)}) == override


def test_simulation_context_refuses_paid_spend_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim_root = tmp_path / "sims"
    campaign_root = sim_root / "campaign-a"
    var_root = campaign_root / "var"
    var_root.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign-a")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(var_root / "state.sqlite3"))

    with pytest.raises(SpendDbResolutionError, match="simulation campaign"):
        resolve_spend_db_path()
    with pytest.raises(SpendGuardRefusal) as caught:
        SpendGuard(config_path=tmp_path / "spend-caps.yaml")
    assert caught.value.reason_class == "simulation_spend_db"


def _write_config(path: Path, *, cap_usd: str = "0.01") -> None:
    path.write_text(
        "version: 1\n"
        "safety_factor: '1.25'\n"
        "soft_threshold: '0.95'\n"
        "providers:\n"
        "  moonshot:\n"
        "    enabled: true\n"
        f"    daily_cap_usd: '{cap_usd}'\n"
        "    models:\n"
        "      kimi-k3:\n"
        "        input_usd_per_million_tokens: '1.00'\n"
        "        output_usd_per_million_tokens: '1.00'\n"
        "        max_output_tokens: 1000\n"
        "        default_output_tokens: 128\n"
        "  fireworks:\n"
        "    enabled: true\n"
        f"    daily_cap_usd: '{cap_usd}'\n"
        "    models:\n"
        "      kimi-k3:\n"
        "        input_usd_per_million_tokens: '1.00'\n"
        "        output_usd_per_million_tokens: '1.00'\n"
        "        max_output_tokens: 1000\n"
        "        default_output_tokens: 128\n",
        encoding="utf-8",
    )


def _input(*, call_id: str, tokens_max: int = 1000) -> AgentInput:
    return AgentInput(
        run_id="run-spend-test",
        task_id="task-spend-test",
        prompt="x" * 4,
        model="kimi-k3",
        budget=BudgetSpec(tokens_max=tokens_max),
        metadata={"call_id": call_id, "request_id": f"req-{call_id}"},
    )


def _seed_exact(
    store: SqliteStore,
    *,
    call_id: str,
    provider: str,
    nanos: int,
    upper_bound_nanos: int | None = None,
) -> None:
    whole, fractional = divmod(nanos, 1_000_000_000)
    decimal = str(whole) if not fractional else f"{whole}.{fractional:09d}".rstrip("0")
    store.record_provider_call(
        {
            "call_id": call_id,
            "request_id": f"req-{call_id}",
            "execution_id": f"exe-{call_id}",
            "stage": "worker",
            "provider": provider,
            "transport": "test",
            "requested_model": "kimi-k3",
            "effective_model": "kimi-k3",
            "model_lineage": "kimi",
            "billing_provider": provider,
            "adapter_key": "mock-paid",
            "request_state": "sent",
            "provider_outcome": "completed",
            "cost_usd_decimal": decimal,
            "cost_usd_nanos": nanos,
            "cost_upper_bound_usd_nanos": upper_bound_nanos,
            "cost_quality": "exact",
            "cost_source": "provider-report",
            "created_at": NOW,
            "settled_at": NOW,
        }
    )


def test_spend_day_prefers_exact_cost_and_uses_estimate_when_exact_is_absent(
    tmp_path: Path,
) -> None:
    store = SqliteStore(str(tmp_path / "spend-day.sqlite3"))
    try:
        _seed_exact(
            store,
            call_id="exact-with-stale-ceiling",
            provider="fireworks",
            nanos=2_000_000,
            upper_bound_nanos=9_000_000,
        )
        store.record_provider_call(
            {
                "call_id": "estimated-only",
                "request_id": "req-estimated-only",
                "execution_id": "exe-estimated-only",
                "stage": "worker",
                "provider": "fireworks",
                "transport": "test",
                "requested_model": "kimi-k3",
                "effective_model": "kimi-k3",
                "model_lineage": "kimi",
                "billing_provider": "fireworks",
                "adapter_key": "test",
                "request_state": "not_sent",
                "provider_outcome": "reserved",
                "cost_upper_bound_usd_nanos": 3_000_000,
                "cost_quality": "estimated",
                "cost_source": "test-upper-bound",
                "created_at": NOW,
            }
        )

        rows = store.provider_spend_day(provider="fireworks", utc_day=DAY)
        assert len(rows) == 1
        assert rows[0]["spend_usd_nanos"] == 5_000_000
        assert rows[0]["ledger_row_count"] == 2
    finally:
        store.close()


@pytest.fixture
def guard_parts(tmp_path: Path) -> Iterator[tuple[SpendGuard, SqliteStore, list[str]]]:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "state.sqlite3"
    alerts: list[str] = []

    def alert_sender(text: str, *, webhook_env: str) -> Any:
        assert webhook_env == "OPS_ALERT_SLACK_WEBHOOK_URL"
        alerts.append(text)
        return type("AlertResult", (), {"ok": True, "detail": "sent"})()

    guard = SpendGuard(
        config_path=config,
        db_path=str(db_path),
        alert_sender=alert_sender,
        now=lambda: NOW,
    )
    store = SqliteStore(str(db_path))
    try:
        yield guard, store, alerts
    finally:
        store.close()


def test_temp_one_cent_cap_refuses_and_records_attempt(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]],
) -> None:
    guard, store, _alerts = guard_parts
    _seed_exact(store, call_id="prior", provider="fireworks", nanos=9_000_000)

    with pytest.raises(SpendGuardRefusal, match="terminally parked") as caught:
        guard.preflight(
            _input(call_id="refused", tokens_max=1000),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )

    assert caught.value.reason_class == "daily_cap_exceeded"
    row = store.get_provider_call("refused")
    assert row is not None
    assert row["request_state"] == "not_sent"
    assert row["provider_outcome"] == "terminally_parked"
    assert row["cost_source"] == "spend-guard-refusal:daily_cap_exceeded"


def test_soft_threshold_alerts_once_and_only_marks_success(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "state.sqlite3"
    store = SqliteStore(str(db_path))
    _seed_exact(store, call_id="prior", provider="fireworks", nanos=8_500_000)
    outcomes = iter([False, True])
    alerts: list[str] = []

    def alert_sender(text: str, *, webhook_env: str) -> Any:
        alerts.append(text)
        return type("AlertResult", (), {"ok": next(outcomes), "detail": "test"})()

    guard = SpendGuard(
        config_path=config,
        db_path=str(db_path),
        alert_sender=alert_sender,
        now=lambda: NOW,
    )
    try:
        first = guard.preflight(
            _input(call_id="soft-1", tokens_max=799),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
        guard.settle_exact(first, normalize_provider_cost("0"), provider_outcome="test-no-charge")
        assert not any(
            row["cost_source"] == "spend-cap-alert:delivered"
            for row in store.list_provider_calls(execution_id=f"spend-alert:fireworks:{DAY}")
        )

        second = guard.preflight(
            _input(call_id="soft-2", tokens_max=799),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
        guard.settle_exact(second, normalize_provider_cost("0"), provider_outcome="test-no-charge")
        assert sum(
            row["cost_source"] == "spend-cap-alert:delivered"
            for row in store.list_provider_calls(execution_id=f"spend-alert:fireworks:{DAY}")
        ) == 1

        third = guard.preflight(
            _input(call_id="soft-3", tokens_max=799),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
        guard.settle_exact(third, normalize_provider_cost("0"), provider_outcome="test-no-charge")
        assert len(alerts) == 2
    finally:
        store.close()


def test_soft_alert_network_runs_outside_reservation_write_lock(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "state.sqlite3"
    store = SqliteStore(str(db_path))
    alert_writer = SqliteStore(str(db_path))
    _seed_exact(store, call_id="prior-alert-lock", provider="fireworks", nanos=8_500_000)

    def alert_sender(_text: str, *, webhook_env: str) -> Any:
        assert webhook_env == "OPS_ALERT_SLACK_WEBHOOK_URL"
        # A separate SQLite connection can take the write lock while delivery
        # runs; this would block/fail if BEGIN IMMEDIATE were still open.
        _seed_exact(alert_writer, call_id="alert-lock-probe", provider="probe", nanos=0)
        return type("AlertResult", (), {"ok": True})()

    guard = SpendGuard(
        config_path=config,
        db_path=str(db_path),
        alert_sender=alert_sender,
        now=lambda: NOW,
    )
    try:
        guard.preflight(
            _input(call_id="alert-lock", tokens_max=799),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
        assert store.get_provider_call("alert-lock-probe") is not None
        delivered = [
            row
            for row in store.list_provider_calls(execution_id=f"spend-alert:fireworks:{DAY}")
            if row["cost_source"] == "spend-cap-alert:delivered"
        ]
        assert len(delivered) == 1
    finally:
        guard.close()
        alert_writer.close()
        store.close()


def test_soft_alert_claim_expires_after_crash(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "state.sqlite3"
    store = SqliteStore(str(db_path))
    guard = SpendGuard(config_path=config, db_path=str(db_path), now=lambda: NOW)
    execution_id = f"spend-alert:fireworks:{DAY}"

    def marker(created_at: str) -> Any:
        return guard._marker(
            provider="fireworks",
            model="kimi-k3",
            created_at=created_at,
            call_id=execution_id,
            execution_id=execution_id,
            cost_source="spend-cap-alert:delivered",
            outcome="soft_threshold_alert_delivered",
        )

    sent: list[str] = []

    def record_send(label: str) -> bool:
        sent.append(label)
        return True

    try:
        with pytest.raises(SystemExit):
            store.deliver_provider_alert_once(
                provider="fireworks",
                utc_day=DAY,
                marker=marker(NOW),
                deliver=lambda: (_ for _ in ()).throw(SystemExit("simulated kill")),
            )
        assert (
            store.deliver_provider_alert_once(
                provider="fireworks",
                utc_day=DAY,
                marker=marker("2026-08-05T12:00:30Z"),
                deliver=lambda: record_send("too-early"),
            )
            is False
        )
        assert sent == []
        assert store.deliver_provider_alert_once(
            provider="fireworks",
            utc_day=DAY,
            marker=marker("2026-08-05T12:01:01Z"),
            deliver=lambda: record_send("after-expiry"),
        )
        assert sent == ["after-expiry"]
    finally:
        guard.close()
        store.close()


def test_matching_emergency_override_audits_and_alerts_every_use(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, store, alerts = guard_parts
    _seed_exact(store, call_id="prior", provider="fireworks", nanos=9_000_000)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_CAP_OVERRIDE", f"fireworks:{DAY}")

    for index in range(2):
        ticket = guard.preflight(
            _input(call_id=f"override-{index}", tokens_max=1000),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
        assert ticket.override_used is True
        guard.settle_unknown(ticket, provider_outcome="test-no-charge")

    override_rows = [
        row
        for row in store.list_provider_calls(limit=100)
        if row["cost_source"] == "spend-cap-override:audit"
    ]
    assert len(override_rows) == 2
    assert len(alerts) == 2


@pytest.mark.parametrize(
    "override",
    ["fireworks:2026-08-04", "moonshot:2026-08-05", "fireworks:*", "fireworks"],
)
def test_emergency_override_requires_exact_provider_and_utc_day(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    guard, store, alerts = guard_parts
    _seed_exact(store, call_id="prior-override-mismatch", provider="fireworks", nanos=9_000_000)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_CAP_OVERRIDE", override)

    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id=f"override-mismatch-{override}", tokens_max=1000),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )

    assert caught.value.reason_class == "daily_cap_exceeded"
    assert alerts  # The ordinary provider/day threshold alert still applies.
    assert not any(
        row["cost_source"] == "spend-cap-override:audit"
        for row in store.list_provider_calls(limit=100)
    )


def test_config_edit_takes_effect_on_the_next_call(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config, cap_usd="1.00")
    guard = SpendGuard(
        config_path=config,
        db_path=str(tmp_path / "state.sqlite3"),
        alert_sender=lambda *_args, **_kwargs: type("Alert", (), {"ok": True})(),
        now=lambda: NOW,
    )
    first = guard.preflight(
        _input(call_id="before-config-edit", tokens_max=1),
        provider="fireworks",
        model="kimi-k3",
        transport="http",
        adapter_key="api-kimi-k3",
    )
    guard.settle_exact(first, normalize_provider_cost("0"), provider_outcome="test")

    _write_config(config, cap_usd="0.000001")
    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id="after-config-edit", tokens_max=1),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="api-kimi-k3",
        )
    assert caught.value.reason_class == "daily_cap_exceeded"


@pytest.mark.parametrize(
    ("provider", "model", "reason_class"),
    [
        ("unknown-paid-provider", "kimi-k3", "unknown_provider"),
        ("fireworks", "unknown-model", "unknown_model"),
    ],
)
def test_unknown_provider_or_model_fails_closed_with_distinct_reason(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]],
    provider: str,
    model: str,
    reason_class: str,
) -> None:
    guard, _store, _alerts = guard_parts
    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id=f"bad-{reason_class}"),
            provider=provider,
            model=model,
            transport="http",
            adapter_key="mock-paid",
        )
    assert caught.value.reason_class == reason_class


def test_missing_config_fails_closed_with_named_class(tmp_path: Path) -> None:
    guard = SpendGuard(
        config_path=tmp_path / "missing.yaml",
        db_path=str(tmp_path / "state.sqlite3"),
        alert_sender=lambda *_args, **_kwargs: None,
        now=lambda: NOW,
    )
    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id="missing-config"),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
    assert caught.value.reason_class == "config_missing"


@pytest.mark.parametrize(
    "contents",
    [
        "not: [valid",
        (
            "version: 1\n"
            "safety_factor: '1.25'\n"
            "soft_threshold: '0.95'\n"
            "providers:\n"
            "  moonshot:\n"
            "    enabled: true\n"
            "    daily_cap_usd: '1'\n"
            "    models: {}\n"
        ),
    ],
)
def test_unparseable_or_missing_default_route_pricing_fails_all_paid_calls(
    tmp_path: Path,
    contents: str,
) -> None:
    config = tmp_path / "bad-spend-caps.yaml"
    config.write_text(contents, encoding="utf-8")
    guard = SpendGuard(
        config_path=config,
        db_path=str(tmp_path / "state.sqlite3"),
        alert_sender=lambda *_args, **_kwargs: None,
        now=lambda: NOW,
    )
    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id="bad-config"),
            provider="kimi",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
    assert caught.value.reason_class == "config_unparseable"


def test_unreadable_ledger_fails_closed_with_named_class(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    with pytest.raises(SpendGuardRefusal) as caught:
        SpendGuard(
            config_path=config,
            db_path=str(tmp_path),  # A directory cannot be opened as a SQLite database.
            alert_sender=lambda *_args, **_kwargs: None,
            now=lambda: NOW,
        )
    assert caught.value.reason_class == "ledger_unreadable"


def test_conflicting_call_id_fails_closed_as_ledger_conflict(tmp_path: Path) -> None:
    guard, store = _adapter_guard(tmp_path)
    try:
        guard.preflight(
            _input(call_id="duplicate", tokens_max=1),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
        with pytest.raises(SpendGuardRefusal) as caught:
            guard.preflight(
                _input(call_id="duplicate", tokens_max=2),
                provider="fireworks",
                model="kimi-k3",
                transport="http",
                adapter_key="mock-paid",
            )
        assert caught.value.reason_class == "ledger_conflict"
        assert "replay conflicts" in caught.value.detail
    finally:
        guard.close()
        store.close()


def test_integrity_error_fails_closed_as_ledger_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    guard, store = _adapter_guard(tmp_path)

    def conflict(**_kwargs: Any) -> dict[str, Any]:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: provider_call_usage.call_id")

    monkeypatch.setattr(store, "reserve_provider_spend", conflict)
    monkeypatch.setattr(guard, "_ledger", lambda: store)
    try:
        with pytest.raises(SpendGuardRefusal) as caught:
            guard.preflight(
                _input(call_id="integrity-conflict"),
                provider="fireworks",
                model="kimi-k3",
                transport="http",
                adapter_key="mock-paid",
            )
        assert caught.value.reason_class == "ledger_conflict"
        assert "UNIQUE constraint" in caught.value.detail
    finally:
        guard.close()
        store.close()


def _adapter_guard(tmp_path: Path) -> tuple[SpendGuard, SqliteStore]:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "adapter.sqlite3"
    guard = SpendGuard(
        config_path=config,
        db_path=str(db_path),
        alert_sender=lambda *_args, **_kwargs: type("Alert", (), {"ok": True})(),
        now=lambda: NOW,
    )
    return guard, SqliteStore(str(db_path))


def test_fireworks_cap_refusal_never_calls_moonshot_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, store = _adapter_guard(tmp_path)
    _seed_exact(store, call_id="prior-fw", provider="fireworks", nanos=9_000_000)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    fallback_calls: list[bool] = []

    def forbidden_fallback() -> MoonshotKimiK3Adapter:
        fallback_calls.append(True)
        raise AssertionError("cap refusal reached Moonshot fallback")

    monkeypatch.setattr(adapter, "fallback_adapter", forbidden_fallback)
    http_calls: list[bool] = []
    monkeypatch.setattr(
        "requests.post",
        lambda *_args, **_kwargs: http_calls.append(True),
    )
    try:
        with pytest.raises(SpendGuardRefusal) as caught:
            adapter.run(_input(call_id="fireworks-cap", tokens_max=1000))
        assert caught.value.reason_class == "daily_cap_exceeded"
        assert fallback_calls == []
        assert http_calls == []
    finally:
        guard.close()
        store.close()


def test_fireworks_outage_falls_back_and_capped_kimi_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    guard, store = _adapter_guard(tmp_path)
    _seed_exact(store, call_id="prior-moonshot", provider="moonshot", nanos=9_000_000)
    fireworks = FireworksKimiK3Adapter()
    moonshot = MoonshotKimiK3Adapter()
    monkeypatch.setattr(fireworks, "spend_guard", lambda: guard)
    monkeypatch.setattr(moonshot, "spend_guard", lambda: guard)
    monkeypatch.setattr(fireworks, "api_key", lambda: "fireworks-key")
    monkeypatch.setattr(moonshot, "api_key", lambda: "moonshot-key")
    monkeypatch.setattr(fireworks, "fallback_adapter", lambda: moonshot)
    endpoints: list[str] = []

    def post(url: str, **_kwargs: Any) -> Any:
        endpoints.append(url)
        raise requests.ConnectionError("connection refused by simulated Fireworks outage")

    monkeypatch.setattr("requests.post", post)
    try:
        with pytest.raises(SpendGuardRefusal) as caught:
            fireworks.run(_input(call_id="outage", tokens_max=1000))
        assert caught.value.reason_class == "daily_cap_exceeded"
        assert endpoints == ["https://api.fireworks.ai/inference/v1/chat/completions"]
        fallback_rows = [
            row
            for row in store.list_provider_calls(limit=20)
            if row["provider"] == "moonshot" and row["call_id"].startswith("outage:outage-fallback:kimi:")
        ]
        assert len(fallback_rows) == 1
        fallback_row = fallback_rows[0]
        assert fallback_row is not None
        assert fallback_row["provider"] == "moonshot"
        assert fallback_row["request_state"] == "not_sent"
        assert fallback_row["provider_outcome"] == "terminally_parked"
    finally:
        guard.close()
        store.close()


def test_two_consecutive_fireworks_outages_both_reach_moonshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    guard, store = _adapter_guard(tmp_path)
    fireworks = FireworksKimiK3Adapter()
    moonshot = MoonshotKimiK3Adapter()
    monkeypatch.setattr(fireworks, "spend_guard", lambda: guard)
    monkeypatch.setattr(moonshot, "spend_guard", lambda: guard)
    monkeypatch.setattr(fireworks, "api_key", lambda: "fireworks-key")
    monkeypatch.setattr(moonshot, "api_key", lambda: "moonshot-key")
    monkeypatch.setattr(fireworks, "fallback_adapter", lambda: moonshot)
    endpoints: list[str] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "moonshot-success",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "fallback succeeded"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "cost": "0.000004"},
            }

    def post(url: str, **_kwargs: Any) -> Any:
        endpoints.append(url)
        if url.startswith("https://api.fireworks.ai/"):
            raise requests.ConnectionError("simulated Fireworks outage")
        return Response()

    monkeypatch.setattr("requests.post", post)
    try:
        inputs = [
            AgentInput(
                run_id="run-x",
                task_id="task-x",
                prompt="hello there",
                model="kimi-k3",
                budget=BudgetSpec(max_turns=1, wall_ms_max=30_000),
                metadata={},
            )
            for _ in range(2)
        ]
        results = [fireworks.run(input_obj) for input_obj in inputs]

        assert [result.status.value for result in results] == ["ok", "ok"]
        assert sum(url.startswith("https://api.fireworks.ai/") for url in endpoints) == 2
        assert sum(url.startswith("https://api.moonshot.ai/") for url in endpoints) == 2
        assert all(str(input_obj.metadata.get("call_id", "")).startswith("call_") for input_obj in inputs)
        assert inputs[0].metadata["call_id"] != inputs[1].metadata["call_id"]
        rows = [row for row in store.list_provider_calls(limit=20) if row["provider"] in {"fireworks", "moonshot"}]
        assert len(rows) == 4
        assert len({row["call_id"] for row in rows}) == 4
        for input_obj in inputs:
            root = str(input_obj.metadata["call_id"])
            assert any(
                row["provider"] == "moonshot"
                and str(row["call_id"]).startswith(f"{root}:outage-fallback:kimi:")
                for row in rows
            )
    finally:
        guard.close()
        store.close()


def test_provider_response_exact_cost_reconciles_reserved_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "fireworks-key")

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "provider-call-1",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "done"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "cost": "0.000004321",
                },
            }

    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: Response())
    try:
        result = adapter.run(_input(call_id="reconcile", tokens_max=1000))
        assert result.status.value == "ok"
        row = store.get_provider_call("reconcile")
        assert row is not None
        assert row["cost_quality"] == "exact"
        assert row["cost_usd_decimal"] == "0.000004321"
        assert row["cost_usd_nanos"] == 4321
        assert row["cost_upper_bound_usd_nanos"] is None
        assert row["settled_at"] is not None
    finally:
        guard.close()
        store.close()


def test_default_output_ceiling_is_reserved_but_not_forced_onto_an_unset_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updated 2026-08-06 review (M6): the reservation default is a COST bound,
    not a request ceiling. The guard still reserves conservatively against
    ``pricing.default_output_tokens`` for headroom purposes even with no
    caller ceiling, but that reserved value must not appear as ``max_tokens``
    on the wire -- see test_max_tokens_is_not_forced_onto_an_unset_caller_budget
    for the direct regression on the wire payload.
    """
    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "fireworks-key")
    seen: dict[str, Any] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "provider-default-ceiling",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0.000002"},
            }

    def post(_url: str, **kwargs: Any) -> Response:
        seen["payload"] = kwargs["json"]
        row = store.get_provider_call("default-ceiling")
        assert row is not None
        seen["reserved_nanos"] = row["cost_upper_bound_usd_nanos"]
        return Response()

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(
            AgentInput(
                run_id="run-default-ceiling",
                task_id="task-default-ceiling",
                prompt="xxxx",
                model="kimi-k3",
                budget=BudgetSpec(tokens_max=None),
                metadata={"call_id": "default-ceiling"},
            )
        )
        assert result.status.value == "ok"
        assert seen["reserved_nanos"] == 161_250
        assert "max_tokens" not in seen["payload"]
    finally:
        guard.close()
        store.close()


def test_default_output_tokens_must_not_exceed_provider_max(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "default_output_tokens: 128", "default_output_tokens: 1001"
        ),
        encoding="utf-8",
    )
    guard = SpendGuard(
        config_path=config,
        db_path=str(tmp_path / "state.sqlite3"),
        alert_sender=lambda *_args, **_kwargs: None,
        now=lambda: NOW,
    )
    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id="invalid-default"),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
    assert caught.value.reason_class == "config_unparseable"
    assert "default_output_tokens" in caught.value.detail


def test_startup_sweeps_stale_reservation_to_bounded_indeterminate(tmp_path: Path) -> None:
    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "stale.sqlite3"
    old_guard = SpendGuard(
        config_path=config,
        db_path=str(db_path),
        now=lambda: "2026-08-05T11:50:00Z",
    )
    ticket = old_guard.preflight(
        _input(call_id="stale-reservation", tokens_max=10),
        provider="fireworks",
        model="kimi-k3",
        transport="http",
        adapter_key="mock-paid",
    )
    old_guard.close()

    guard = SpendGuard(config_path=config, db_path=str(db_path), now=lambda: NOW)
    store = SqliteStore(str(db_path))
    try:
        assert guard.startup_swept_reservations == 1
        row = store.get_provider_call(ticket.call_id)
        assert row is not None
        assert row["request_state"] == "indeterminate"
        assert row["provider_outcome"] == "stale_reservation_swept"
        assert row["cost_upper_bound_usd_nanos"] == ticket.proposed_usd_nanos
        assert row["cost_source"] == "spend-cap-upper-bound:stale-reservation-sweep"
        assert row["settled_at"] is not None
    finally:
        guard.close()
        store.close()


def _exact_response() -> Any:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "provider-settlement-retry",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "billed answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "cost": "0.000003"},
            }

    return Response()


def test_billed_answer_retries_transient_settlement_and_keeps_exact_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "fireworks-key")
    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: _exact_response())
    real_settle = guard.settle_exact
    attempts: list[int] = []

    def transient(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient settlement failure")
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(guard, "settle_exact", transient)
    try:
        result = adapter.run(_input(call_id="settle-retry", tokens_max=10))
        row = store.get_provider_call("settle-retry")
        assert result.status.value == "ok"
        assert result.output_text == "billed answer"
        assert len(attempts) == 2
        assert row is not None and row["cost_quality"] == "exact"
        assert row["cost_usd_nanos"] == 3000
    finally:
        guard.close()
        store.close()


def test_billed_answer_degrades_to_settle_unknown_after_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "fireworks-key")
    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: _exact_response())
    attempts: list[int] = []

    def persistent(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        raise OSError("persistent settlement failure")

    monkeypatch.setattr(guard, "settle_exact", persistent)
    try:
        result = adapter.run(_input(call_id="settle-degraded", tokens_max=10))
        row = store.get_provider_call("settle-degraded")
        assert result.status.value == "ok"
        assert result.output_text == "billed answer"
        assert len(attempts) == 2
        assert row is not None
        assert row["cost_quality"] == "estimated"
        assert row["request_state"] == "sent"
        assert row["provider_outcome"] == "completed:settle_unknown"
        assert row["cost_upper_bound_usd_nanos"] is not None
    finally:
        guard.close()
        store.close()


def test_settle_unknown_keeps_its_own_cost_provenance_stamp(tmp_path: Path) -> None:
    """An indeterminate settlement must be readable AS indeterminate.

    ``settle_unknown`` is the third caller (with the stale-reservation sweep and
    the alert-claim release) whose deliberate ``cost_source`` the DAL's
    once-only pricing freeze used to discard, leaving the row still stamped
    ``spend-cap-upper-bound`` -- indistinguishable from a live reservation that
    nobody has settled at all.
    """

    guard, store = _adapter_guard(tmp_path)
    try:
        ticket = guard.preflight(
            _input(call_id="unknown-provenance", tokens_max=10),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
        guard.settle_unknown(ticket, provider_outcome="provider-cost-missing")
        row = store.get_provider_call(ticket.call_id)
        assert row is not None
        assert row["request_state"] == "indeterminate"
        assert row["provider_outcome"] == "provider-cost-missing"
        assert row["cost_source"] == "spend-cap-upper-bound:provider-cost-unavailable"
        # Unknown is never favourable: the full reservation stays held.
        assert row["cost_upper_bound_usd_nanos"] == ticket.proposed_usd_nanos
        assert row["cost_quality"] == "estimated"
    finally:
        guard.close()
        store.close()


def test_guard_row_uses_authoritative_lineage_family_not_raw_model(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    fireworks_models = document["providers"]["fireworks"]["models"]
    fireworks_models["moonshotai/kimi-k2.6"] = dict(fireworks_models["kimi-k3"])
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    db_path = tmp_path / "lineage.sqlite3"
    guard = SpendGuard(config_path=config, db_path=str(db_path), now=lambda: NOW)
    store = SqliteStore(str(db_path))
    try:
        ticket = guard.preflight(
            AgentInput(
                run_id="lineage-run",
                task_id="lineage-task",
                prompt="lineage",
                model="moonshotai/kimi-k2.6",
                budget=BudgetSpec(tokens_max=1),
                metadata={"call_id": "lineage-call"},
            ),
            provider="fireworks",
            model="moonshotai/kimi-k2.6",
            transport="http",
            adapter_key="mock-paid",
        )
        row = store.get_provider_call(ticket.call_id)
        assert row is not None
        assert row["model_lineage"] == "kimi"
        assert row["model_lineage"] != row["requested_model"]
    finally:
        guard.close()
        store.close()

def test_alert_side_channel_exception_never_defeats_a_cap_refusal(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 2 (2026-08-06 review): reproduces Opus's exact repro exception.

    Pre-fix, ``_alert_once``/``_audit_override`` ran BEFORE the terminal
    ``SpendGuardRefusal`` raise, unwrapped. A real write-contention exception
    escaping that alert path (here the codebase's own ``BusyRetryExhausted``,
    not a synthetic stand-in) fell through un-classified into a caller's
    generic ``except Exception`` and was treated as retryable -- exactly the
    B1 defect, reached through a different door. This must now raise
    ``SpendGuardRefusal`` with ``reason_class == "daily_cap_exceeded"``, not
    ``BusyRetryExhausted``, even though the alert path is guaranteed to run
    (this call is over the soft threshold on every over-cap preflight).
    """

    guard, store, _alerts = guard_parts
    _seed_exact(store, call_id="prior-over-cap", provider="fireworks", nanos=9_000_000)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise BusyRetryExhausted(
            "simulated live write contention during alert delivery", attempts=5
        )

    monkeypatch.setattr(guard, "_alert_once", _boom)

    with pytest.raises(SpendGuardRefusal) as caught:
        guard.preflight(
            _input(call_id="over-cap-alert-boom", tokens_max=1000),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )

    assert caught.value.reason_class == "daily_cap_exceeded"


def test_override_audit_exception_never_defeats_the_reservation(
    guard_parts: tuple[SpendGuard, SqliteStore, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 2: the same shape for the ``_audit_override`` side channel.

    An allowed override call must still return a valid ``SpendTicket`` even
    when the best-effort audit marker/alert write raises.
    """

    guard, store, _alerts = guard_parts
    _seed_exact(store, call_id="prior-over-cap-2", provider="fireworks", nanos=9_000_000)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_CAP_OVERRIDE", f"fireworks:{DAY}")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise BusyRetryExhausted(
            "simulated live write contention during override audit", attempts=5
        )

    monkeypatch.setattr(guard, "_audit_override", _boom)

    ticket = guard.preflight(
        _input(call_id="override-audit-boom", tokens_max=1000),
        provider="fireworks",
        model="kimi-k3",
        transport="http",
        adapter_key="mock-paid",
    )
    assert ticket.override_used is True


def test_legacy_kimi_billing_rows_count_toward_the_moonshot_cap(
    tmp_path: Path,
) -> None:
    """Blocker 1 (2026-08-06 review): dual-read alias, not a silent rename.

    A row written under either the legacy ``kimi`` billing_provider string
    (a pre-fix build of this guard, or any stray live writer using it) or the
    canonical ``moonshot`` string must count exactly once toward the SAME
    daily cap -- never as two independent $100/day allowances against one
    real Moonshot spend line, which is the live co-writer double-spend
    Blocker 1 identified.
    """

    config = tmp_path / "spend-caps.yaml"
    _write_config(config)
    db_path = tmp_path / "state.sqlite3"
    guard = SpendGuard(config_path=config, db_path=str(db_path), now=lambda: NOW)
    store = SqliteStore(str(db_path))
    try:
        # Split the same 9,000,000ns prior spend that
        # test_temp_one_cent_cap_refuses_and_records_attempt proves refuses
        # (against the default 0.01 cap) across BOTH the legacy ``kimi``
        # string and the canonical ``moonshot`` string. If they were not
        # summed together, neither half alone would exceed the cap and this
        # call would be wrongly allowed.
        _seed_exact(store, call_id="legacy-kimi-row", provider="kimi", nanos=4_500_000)
        _seed_exact(store, call_id="canonical-moonshot-row", provider="moonshot", nanos=4_500_000)

        with pytest.raises(SpendGuardRefusal) as caught:
            guard.preflight(
                _input(call_id="moonshot-third-call", tokens_max=1000),
                provider="moonshot",
                model="kimi-k3",
                transport="http",
                adapter_key="mock-paid",
            )
        assert caught.value.reason_class == "daily_cap_exceeded"
        assert "prior=9000000ns" in str(caught.value)
    finally:
        guard.close()
        store.close()
def test_repeated_401s_never_burn_cap_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 (2026-08-06 review): a rotated credential must not self-DOS the cap.

    Opus measured 25x HTTP 401 consuming $1.5360 of reserved headroom for
    $0.00 real spend; 3,255 401s exhausts the $200 cap and, post-Blocker-2,
    every subsequent call is terminally refused for the rest of the UTC day.
    A 401 PROVES the request never reached model execution, so N of them
    must leave the day's spend at exactly $0 -- release, not retain.
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "revoked-key")

    class Response:
        status_code = 401

        @staticmethod
        def json() -> dict[str, Any]:
            return {"error": {"message": "invalid_api_key"}}

    monkeypatch.setattr("requests.post", lambda *_a, **_kw: Response())
    try:
        for i in range(25):
            result = adapter.run(_input(call_id=f"revoked-{i}", tokens_max=1000))
            assert result.status.value == "error"
            assert "HTTP 401" in (result.error or "")

        rows = store.provider_spend_day(provider="fireworks", utc_day=DAY)
        spend_nanos = rows[0]["spend_usd_nanos"] if rows else 0
        assert spend_nanos == 0, (
            f"expected $0 real spend after 25x 401, ledger shows {spend_nanos}ns "
            "-- a rotated credential must not consume cap headroom"
        )

        # Cap headroom must still admit a normal, actually-billable call.
        ticket = guard.preflight(
            _input(call_id="after-401-storm", tokens_max=1000),
            provider="fireworks",
            model="kimi-k3",
            transport="http",
            adapter_key="mock-paid",
        )
        assert ticket is not None
    finally:
        guard.close()
        store.close()


def _wrapped_connection_error(reason: Any) -> Any:
    """Build a realistic ``requests.ConnectionError`` wrapping a urllib3 reason.

    Mirrors what ``requests`` actually raises: the ConnectionError's first
    positional arg is a ``MaxRetryError`` whose ``.reason`` is the real
    low-level urllib3 exception.
    """
    import requests
    from urllib3.exceptions import MaxRetryError

    wrapped = MaxRetryError(None, "http://example.invalid", reason=reason)
    return requests.exceptions.ConnectionError(wrapped)


def test_connection_refused_releases_the_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: a refused TCP connection proves no bytes reached the provider -- release."""
    from urllib3.exceptions import NewConnectionError

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(adapter, "fallback_adapter", lambda: MoonshotKimiK3Adapter())
    monkeypatch.setattr(
        MoonshotKimiK3Adapter,
        "spend_guard",
        lambda self: guard,
    )
    monkeypatch.setattr(MoonshotKimiK3Adapter, "api_key", lambda self: "moonshot-key")

    def post(url: str, **_kwargs: Any) -> Any:
        raise _wrapped_connection_error(
            NewConnectionError(None, "Failed to establish a new connection: [Errno 61] Connection refused")
        )

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(_input(call_id="refused", tokens_max=1000))
        assert result.status.value == "error"

        rows = store.provider_spend_day(utc_day=DAY)
        total_nanos = sum(int(row["spend_usd_nanos"] or 0) for row in rows)
        assert total_nanos == 0, (
            f"expected $0 real spend after a refused connection, ledger shows "
            f"{total_nanos}ns total across all providers"
        )
    finally:
        guard.close()
        store.close()


def test_dns_failure_releases_the_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: an unresolved DNS name also proves no bytes reached the provider."""
    from urllib3.exceptions import NameResolutionError

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")

    def post(url: str, **_kwargs: Any) -> Any:
        raise _wrapped_connection_error(
            NameResolutionError("api.fireworks.ai", None, "Name or service not known")
        )

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(_input(call_id="dns-failure", tokens_max=1000))
        assert result.status.value == "error"

        rows = store.provider_spend_day(utc_day=DAY)
        total_nanos = sum(int(row["spend_usd_nanos"] or 0) for row in rows)
        assert total_nanos == 0
    finally:
        guard.close()
        store.close()


def test_post_send_connection_reset_stays_conservative_not_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR (2026-08-06 review): a peer RESET after the request was sent must
    NOT be classified as provably-not-billed. Pre-fix, ANY
    ``requests.exceptions.ConnectionError`` released the reservation,
    including ``ProtocolError``/``RemoteDisconnected``/``ConnectionResetError``
    shapes that ``requests`` wraps in that same plain class -- but those can
    happen AFTER the provider already ran inference. This is the mirror
    image of the M4 bug: a favourable-unknown on the money path. The
    reservation must stay indeterminate (upper bound retained), not released.
    """
    import requests

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")

    def post(url: str, **_kwargs: Any) -> Any:
        # requests.exceptions.ConnectionError with a bare
        # ConnectionResetError cause -- no MaxRetryError/.reason wrapper,
        # exactly what a mid-response peer RESET looks like.
        raise requests.exceptions.ConnectionError(
            ConnectionResetError(54, "Connection reset by peer")
        )

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(_input(call_id="peer-reset", tokens_max=1000))
        assert result.status.value == "error"

        rows = store.provider_spend_day(provider="fireworks", utc_day=DAY)
        assert len(rows) == 1
        # Reservation retained: an indeterminate cost_upper_bound_usd_nanos,
        # not released to $0.
        assert rows[0]["spend_usd_nanos"] > 0
    finally:
        guard.close()
        store.close()


def test_max_tokens_is_not_forced_onto_an_unset_caller_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 half 1 (2026-08-06 review): BudgetSpec.tokens_max=None means unlimited.

    The guard's own conservative reservation default (e.g. 8192, used ONLY
    for worst-case cost containment) must never be forwarded as a hard
    ``max_tokens`` on the wire when the caller set no ceiling at all.
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    captured_payloads: list[dict[str, Any]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "resp-1",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "cost": "0.000004"},
            }

    def post(url: str, *, json: dict[str, Any], **_kwargs: Any) -> Any:
        captured_payloads.append(json)
        return Response()

    monkeypatch.setattr("requests.post", post)
    try:
        input_obj = AgentInput(
            run_id="run-unbounded",
            task_id="task-unbounded",
            prompt="hello",
            model="kimi-k3",
            budget=BudgetSpec(max_turns=1, wall_ms_max=30_000),  # tokens_max unset
            metadata={"call_id": "unbounded-call"},
        )
        result = adapter.run(input_obj)
        assert result.status.value == "ok"
        assert captured_payloads
        assert "max_tokens" not in captured_payloads[0]
    finally:
        guard.close()
        store.close()


def test_max_tokens_is_forwarded_when_the_caller_sets_a_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 half 1, the complementary case: an explicit caller ceiling IS forwarded."""

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    captured_payloads: list[dict[str, Any]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "resp-2",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "cost": "0.000004"},
            }

    def post(url: str, *, json: dict[str, Any], **_kwargs: Any) -> Any:
        captured_payloads.append(json)
        return Response()

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(_input(call_id="bounded-call", tokens_max=500))
        assert result.status.value == "ok"
        assert captured_payloads
        assert captured_payloads[0]["max_tokens"] == 500
    finally:
        guard.close()
        store.close()


def test_truncated_response_is_not_reported_as_a_clean_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 half 2: finish_reason=length must not read as status=ok/error=None
    when the caller set no ceiling of their own (the reservation default
    truncated a generation the caller never asked to bound).
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "resp-3",
                "model": "kimi-k3",
                "choices": [
                    {"message": {"content": "cut off mid-sen"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 500, "cost": "0.001"},
            }

    monkeypatch.setattr("requests.post", lambda *_a, **_kw: Response())
    try:
        input_obj = AgentInput(
            run_id="run-truncated",
            task_id="task-truncated",
            prompt="xxxx",
            model="kimi-k3",
            budget=BudgetSpec(max_turns=1, wall_ms_max=30_000),  # tokens_max unset
            metadata={"call_id": "truncated-call"},
        )
        result = adapter.run(input_obj)
        assert result.status.value != "ok"
        assert "truncat" in (result.error or "").lower()
    finally:
        guard.close()
        store.close()


def test_caller_requested_ceiling_truncation_is_not_escalated_to_another_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 (2026-08-06 review, fixed per coordinator's call): when the CALLER
    explicitly set ``budget.tokens_max``, finish_reason=length means the
    ceiling worked exactly as requested -- it must still read as a clean
    success, not an error that would let run_with_fallback escalate a
    deliberately-bounded short generation to another PAID provider.
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "resp-3b",
                "model": "kimi-k3",
                "choices": [
                    {"message": {"content": "short as asked"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 100, "cost": "0.0004"},
            }

    monkeypatch.setattr("requests.post", lambda *_a, **_kw: Response())
    try:
        result = adapter.run(_input(call_id="caller-bounded-call", tokens_max=100))
        assert result.status.value == "ok"
        assert result.output_text == "short as asked"
    finally:
        guard.close()
        store.close()


def test_403_terminally_parks_and_never_advances_to_moonshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quota/suspension/auth are TERMINAL (estate standing rule, 2026-08-06 review).

    A 403 must raise, not return a plain ERROR result -- a plain ERROR
    result is exactly what lets the WIDER planner fallback chain
    (omniagentos.intake.fallback.run_with_fallback) advance past this whole
    rung to an uncapped paid provider. It must also never advance to the
    Moonshot fallback within this adapter: an account-level suspension does
    not become less refused by retrying a sibling billing identity.
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    moonshot_calls: list[bool] = []

    def forbidden_fallback() -> MoonshotKimiK3Adapter:
        moonshot_calls.append(True)
        raise AssertionError("403 auth refusal reached Moonshot fallback")

    monkeypatch.setattr(adapter, "fallback_adapter", forbidden_fallback)

    class Response:
        status_code = 403

        @staticmethod
        def json() -> dict[str, Any]:
            return {"error": {"message": "account suspended"}}

    monkeypatch.setattr("requests.post", lambda *_a, **_kw: Response())
    try:
        with pytest.raises(ProviderAuthRefusal) as caught:
            adapter.run(_input(call_id="suspended-call", tokens_max=1000))
        assert caught.value.provider == "fireworks"
        assert moonshot_calls == []
    finally:
        guard.close()
        store.close()


def test_429_is_not_terminal_and_does_not_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 deviation ruling (2026-08-06 review): must NOT be in the terminal set.

    429 is the standard RATE-LIMIT status for routine per-minute RPM/TPM
    throttling on both Fireworks and Moonshot, not only quota exhaustion,
    and throttles are self-healing. First-strike parking on 429 (an earlier
    pass of this fix) was STRICTLY WORSE than pre-fix: it re-raised through
    run_with_fallback's carve-out and killed the ENTIRE chain including free
    /local rungs, for a condition that resolves on its own. A 429 must
    return a plain ERROR result like any other outage-shaped failure, not
    raise ProviderAuthRefusal.
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")

    class Response:
        status_code = 429

        @staticmethod
        def json() -> dict[str, Any]:
            return {"error": {"message": "rate limit exceeded, retry after 2s"}}

    monkeypatch.setattr("requests.post", lambda *_a, **_kw: Response())
    try:
        result = adapter.run(_input(call_id="throttled-call", tokens_max=1000))
        assert result.status.value == "error"
        assert "HTTP 429" in (result.error or "")
    finally:
        guard.close()
        store.close()


def test_auth_refusal_classification_reads_the_status_receipt_not_the_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-08-06 review: classification must read the structured status code,
    not regex-match the formatted error string -- a provider error body that
    embeds the literal token "HTTP 403" (a realistic upstream-gateway-style
    error detail) must not be misclassified as a 403 response when the real
    status_code was something else (500 here). A 500 is an ordinary outage:
    it must fall through to the Moonshot fallback and succeed normally, NOT
    raise ProviderAuthRefusal. Genuinely red on the pre-fix regex classifier
    (\bHTTP (403|429)\b matches this fixture's "HTTP 403" substring and
    misclassifies; the receipt-based classifier correctly ignores it).
    """

    guard, store = _adapter_guard(tmp_path)
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(adapter, "fallback_adapter", lambda: MoonshotKimiK3Adapter())
    monkeypatch.setattr(MoonshotKimiK3Adapter, "spend_guard", lambda self: guard)
    monkeypatch.setattr(MoonshotKimiK3Adapter, "api_key", lambda self: "moonshot-key")

    class ErrorResponse:
        status_code = 500

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "error": {
                    "message": "upstream gateway returned HTTP 403 from origin"
                }
            }

    class OkResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "id": "moonshot-ok",
                "model": "kimi-k3",
                "choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0.000002"},
            }

    def post(url: str, **_kwargs: Any) -> Any:
        if url.startswith("https://api.fireworks.ai/"):
            return ErrorResponse()
        return OkResponse()

    monkeypatch.setattr("requests.post", post)
    try:
        result = adapter.run(_input(call_id="misleading-error-text", tokens_max=1000))
        assert result.status.value == "ok"
    finally:
        guard.close()
        store.close()
