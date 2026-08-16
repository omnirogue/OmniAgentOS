"""Per-provider, per-account cash collection with per-source isolation.

Fully offline — the broker is mocked. No test may touch the live network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from omniagentos.banking import collect
from omniagentos.banking.collect import collect_day
from omniagentos.goals.collect import _day_bounds

_DAY = date(2026, 7, 10)
_START, _END, _ = _day_bounds(_DAY)
_MID = (_START + _END) // 2  # an epoch squarely inside the ET day


def _accounts_body() -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [
                {
                    "id": "acc_1",
                    "name": "Operating",
                    "last_four": "5555",
                    "type": "checking",
                    # Minor units (cents): $12,345.67 available cash.
                    "available_balance": 1234567,
                }
            ]
        },
    }


def _transactions_body() -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [
                # + deposit $100.00
                {"id": "tx1", "amount": 10000, "posted_at": _MID, "description": "Stripe payout"},
                # − expense $250.00 (debit: negative amount)
                {"id": "tx2", "amount": -25000, "posted_at": _MID, "description": "AWS invoice"},
                # a transaction OUTSIDE the day window — must be filtered out
                {"id": "tx3", "amount": -99900, "posted_at": _START - 10000, "description": "old"},
            ]
        },
    }


def test_collects_slash_balance_deposits_and_true_expenses(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLASH_API_KEY", "sk_primary")
    monkeypatch.delenv("SLASH_API_KEY_ACMEUNI", raising=False)
    monkeypatch.delenv("SLASH_API_KEY_INITECH", raising=False)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        assert cap == "slash_bank.read"
        assert kwargs["method"] == "GET"
        if kwargs["path"] == "/account":
            return _accounts_body()
        if kwargs["path"] == "/transaction":
            return _transactions_body()
        raise AssertionError(f"unexpected path {kwargs['path']}")

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=_DAY)

    assert len(result.accounts) == 1
    ac = result.accounts[0]
    assert ac.account.id == "slash:acc_1"
    assert ac.account.brand == "primary"
    assert ac.fact.balance_usd == 12345.67
    assert ac.fact.deposits_usd == 100.0
    assert ac.fact.expenses_usd == 250.0  # true cash out, positive magnitude
    assert ac.fact.net_flow_usd == -150.0
    assert ac.fact.txn_count == 2  # the out-of-window txn is excluded

    # Persisted.
    from omniagentos.banking.store import BankingStore

    bs = BankingStore(store)
    assert len(bs.query_facts_day("2026-07-10")) == 1
    assert len(bs.query_transactions_day("2026-07-10")) == 2

    # Teller is always surfaced as not-wired.
    assert any("Teller" in n.message for n in result.notes)


def test_per_source_isolation_one_key_failing(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASH_API_KEY", "sk_primary")
    monkeypatch.setenv("SLASH_API_KEY_ACMEUNI", "sk_acmeuni")
    monkeypatch.delenv("SLASH_API_KEY_INITECH", raising=False)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if cap == "slash_bank.read_acmeuni":
            raise RuntimeError("broker denied AcmeUni")
        if kwargs["path"] == "/account":
            return _accounts_body()
        return _transactions_body()

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=_DAY)

    # Primary still collected despite AcmeUni failing.
    assert len(result.accounts) == 1
    assert any("AcmeUni" in n.message and n.level == "warn" for n in result.notes)
    # Initech unconfigured -> info note, run still completes.
    assert any("Initech" in n.message and n.level == "info" for n in result.notes)


def test_direction_field_overrides_amount_sign(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASH_API_KEY", "sk_primary")
    monkeypatch.delenv("SLASH_API_KEY_ACMEUNI", raising=False)
    monkeypatch.delenv("SLASH_API_KEY_INITECH", raising=False)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/account":
            return _accounts_body()
        return {
            "ok": True,
            "status": 200,
            "body": {
                "data": [
                    # positive amount but flagged a debit -> must count as expense
                    {
                        "id": "tx1",
                        "amount": 5000,
                        "direction": "debit",
                        "posted_at": _MID,
                        "description": "fee",
                    },
                ]
            },
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=_DAY)
    ac = result.accounts[0]
    assert ac.fact.expenses_usd == 50.0
    assert ac.fact.deposits_usd == 0.0


def test_no_keys_configured_records_notes_not_crash(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in ("SLASH_API_KEY", "SLASH_API_KEY_ACMEUNI", "SLASH_API_KEY_INITECH"):
        monkeypatch.delenv(env, raising=False)

    def fake_call(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("no broker call should happen when no keys are configured")

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=_DAY)
    assert result.accounts == []
    assert any("Teller" in n.message for n in result.notes)
