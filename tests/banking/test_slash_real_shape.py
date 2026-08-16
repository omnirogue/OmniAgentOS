"""Regression test pinning the Slash API's confirmed response shape.

Confirmed against the api.joinslash.com API (see the header comment of
omniagentos/banking/collect.py for the full context). A prior refactor once
assumed a `data`-wrapped, `amount`-keyed, inline-balance shape that does not
exist on the actual API and silently collected ZERO records — this test
exists so that regression can never happen again unnoticed.

Pinned here:
  * List envelopes are wrapped under ``items`` (not ``data``/``accounts``), both
    for ``GET /account`` and ``GET /transaction``.
  * Transaction amounts are ``amountCents`` (signed integer minor units).
  * The account balance is NOT inlined on the ``/account`` record — it is read
    from the sub-resource ``GET /account/{id}/balance``, which returns BOTH the
    ``cash`` sub-ledger (``available``/``posted`` -> cash balance) and the
    ``credit`` sub-ledger (``available`` -> available credit / spendable
    reserve; ``posted`` -> current card balance owed).
  * Only settled (``posted``) rows count as real cash flow — declined auths
    (``status: "failed"``) and unsettled (``status: "pending"``) rows are
    excluded.
  * Transaction pagination follows ``metadata.nextCursor`` (nested, camelCase),
    not a top-level cursor field.

Fully offline — the broker is mocked. No test may touch the live network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from omniagentos.banking import collect
from omniagentos.banking.collect import collect_day
from omniagentos.banking.store import BankingStore
from omniagentos.goals.collect import _day_bounds

_DAY = date(2026, 7, 20)
_START, _END, _ = _day_bounds(_DAY)
_MID = (_START + _END) // 2  # an epoch squarely inside the ET day


def _real_shape_account_list() -> dict[str, Any]:
    """VERIFIED: GET /account wraps its list under ``items``, not ``data``."""
    return {
        "ok": True,
        "status": 200,
        "body": {
            "items": [
                {
                    "id": "acc_real1",
                    "name": "AcmeUni Operating",
                    "last_four": "5555",
                    "type": "checking",
                    # No balance field here at all — confirmed the /account
                    # record carries no balance; a regression that reads one
                    # inline off this record would silently show a stale $0.
                }
            ]
        },
    }


def _real_shape_balance() -> dict[str, Any]:
    """Confirmed: GET /account/{id}/balance -> per-sub-ledger list, cash + credit.

    Cash sub-ledger available is $0.00 (swept daily to auto-pay the charge
    card). Credit sub-ledger available is $2,500.00 (the spendable
    reserve) and posted is $875.25 (currently owed on the card).
    """
    return {
        "ok": True,
        "status": 200,
        "body": {
            "balances": [
                {"type": "cash", "available": {"amountCents": 0}, "posted": {"amountCents": 0}},
                {
                    "type": "credit",
                    "available": {"amountCents": 250000},
                    "posted": {"amountCents": 87525},
                },
            ]
        },
    }


def _real_shape_transactions_page1() -> dict[str, Any]:
    """First page: a declined auth, a pending row, and one settled wire deposit.

    Confirmed: declined card auths (``status: "failed"``) are commonly
    interleaved with real postings; counting them would grossly overstate cash
    out. Paging continues via ``metadata.nextCursor`` (nested, camelCase).
    """
    return {
        "ok": True,
        "status": 200,
        "body": {
            "items": [
                {
                    "id": "tx_declined",
                    "amountCents": -9999,
                    "status": "failed",
                    "detailedStatus": "declined",
                    "accountId": "acc_real1",
                    "posted_at": _MID,
                    "description": "Declined card auth",
                },
                {
                    "id": "tx_pending",
                    "amountCents": -4321,
                    "status": "pending",
                    "accountId": "acc_real1",
                    "posted_at": _MID,
                    "description": "Unsettled auth",
                },
                {
                    "id": "tx_wire",
                    "amountCents": 500000,
                    "status": "posted",
                    "accountId": "acc_real1",
                    "posted_at": _MID,
                    "description": "Client wire transfer",
                },
            ],
            "metadata": {"nextCursor": "cursor_page2"},
        },
    }


def _real_shape_transactions_page2() -> dict[str, Any]:
    """Second (final) page: the settled daily card-autopay debit. No further cursor."""
    return {
        "ok": True,
        "status": 200,
        "body": {
            "items": [
                {
                    "id": "tx_autopay",
                    "amountCents": -150000,
                    "status": "posted",
                    "accountId": "acc_real1",
                    "posted_at": _MID,
                    "description": "Daily Credit Card Payment",
                }
            ],
            "metadata": {},
        },
    }


def test_pins_real_slash_shape_items_amountcents_credit_and_pagination(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLASH_API_KEY", "sk_primary")
    monkeypatch.delenv("SLASH_API_KEY_ACMEUNI", raising=False)
    monkeypatch.delenv("SLASH_API_KEY_INITECH", raising=False)

    calls: list[dict[str, Any]] = []

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        assert cap == "slash_bank.read"
        assert kwargs["method"] == "GET"
        path = kwargs["path"]
        if path == "/account":
            return _real_shape_account_list()
        if path == "/account/acc_real1/balance":
            return _real_shape_balance()
        if path == "/transaction":
            query = kwargs.get("query") or {}
            if not query.get("cursor"):
                return _real_shape_transactions_page1()
            if query.get("cursor") == "cursor_page2":
                return _real_shape_transactions_page2()
            raise AssertionError(f"unexpected cursor {query.get('cursor')!r}")
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=_DAY)

    assert len(result.accounts) == 1, "the `items`-wrapped /account list must be read"
    ac = result.accounts[0]
    assert ac.account.id == "slash:acc_real1"

    # Balance came from the sub-resource GET /account/{id}/balance, not inlined
    # on the /account record (which carried none).
    assert any(c["path"] == "/account/acc_real1/balance" for c in calls), (
        "balance must be fetched from GET /account/{id}/balance"
    )
    assert ac.fact.balance_usd == 0.0  # cash sub-ledger available -> swept to $0

    # CREDIT sub-ledger: the spendable reserve + what's owed on the card.
    assert ac.fact.available_credit_usd == 2500.0
    assert ac.fact.card_balance_usd == 875.25

    # Only the two SETTLED (posted) rows count; declined + pending are excluded.
    assert ac.fact.txn_count == 2
    assert ac.fact.deposits_usd == 5000.0  # the wire, amountCents -> dollars
    assert ac.fact.expenses_usd == 1500.0  # the card autopay debit
    assert ac.fact.net_flow_usd == 3500.0

    # Pagination followed metadata.nextCursor across both pages.
    transaction_calls = [c for c in calls if c["path"] == "/transaction"]
    assert len(transaction_calls) == 2, "must follow metadata.nextCursor to page 2"
    assert transaction_calls[1]["query"] == {"cursor": "cursor_page2"}

    # Declined/pending rows never became stored transactions or moved money.
    persisted_ids = {t.external_id for t in ac.transactions}
    assert "slash:acc_real1:tx_declined" not in persisted_ids
    assert "slash:acc_real1:tx_pending" not in persisted_ids
    assert "slash:acc_real1:tx_wire" in persisted_ids
    assert "slash:acc_real1:tx_autopay" in persisted_ids

    # Round-trips through the store (migration 021 columns) unchanged.
    bs = BankingStore(store)
    row = bs.query_facts_day("2026-07-20")[0]
    assert row["available_credit_usd"] == 2500.0
    assert row["card_balance_usd"] == 875.25
    assert row["balance_usd"] == 0.0
