"""bank_accounts / bank_facts / bank_transactions store — schema + UPSERT."""

from __future__ import annotations

from typing import Any

from omniagentos.banking.store import BankAccount, BankFact, BankingStore, BankTransaction


def test_account_upsert_is_idempotent_and_refreshes(store: Any) -> None:
    bs = BankingStore(store)
    bs.upsert_account(
        BankAccount(id="slash:a1", provider="slash", brand="AcmeUni", name="Ops", mask="1234")
    )
    # Re-collect with a renamed account -> refreshed in place, not duplicated.
    bs.upsert_account(
        BankAccount(id="slash:a1", provider="slash", brand="AcmeUni", name="Operating", mask="1234")
    )
    accounts = bs.get_accounts()
    assert len(accounts) == 1
    assert accounts[0]["name"] == "Operating"


def test_fact_upsert_keys_on_day_and_account(store: Any) -> None:
    bs = BankingStore(store)
    bs.upsert_fact(
        BankFact(day="2026-07-10", account_id="slash:a1", provider="slash", balance_usd=100.0)
    )
    # Hourly re-collect: late deposit landed.
    bs.upsert_fact(
        BankFact(
            day="2026-07-10",
            account_id="slash:a1",
            provider="slash",
            balance_usd=150.0,
            deposits_usd=50.0,
            net_flow_usd=50.0,
        )
    )
    rows = bs.query_facts_day("2026-07-10")
    assert len(rows) == 1, "one row per (day, account) — must UPSERT"
    assert rows[0]["balance_usd"] == 150.0
    assert rows[0]["deposits_usd"] == 50.0


def test_fact_meta_roundtrips(store: Any) -> None:
    bs = BankingStore(store)
    bs.upsert_fact(
        BankFact(
            day="2026-07-10",
            account_id="slash:a1",
            provider="slash",
            meta={"balance_kind": "available_balance"},
        )
    )
    row = bs.query_facts_day("2026-07-10")[0]
    assert row["meta"]["balance_kind"] == "available_balance"


def test_transaction_upsert_dedupes_on_external_id(store: Any) -> None:
    bs = BankingStore(store)
    txn = BankTransaction(
        day="2026-07-10",
        account_id="slash:a1",
        posted_at="2026-07-10T15:00:00Z",
        amount_usd=-42.5,
        description="AWS",
        category=None,
        external_id="slash:a1:tx1",
    )
    bs.upsert_transaction(txn)
    bs.upsert_transaction(txn)  # re-collect same txn
    rows = bs.query_transactions_day("2026-07-10")
    assert len(rows) == 1, "same external_id must not double-count real money"


def test_transactions_sorted_by_abs_amount_with_limit(store: Any) -> None:
    bs = BankingStore(store)
    for i, amount in enumerate([-5.0, 200.0, -1000.0, 30.0]):
        bs.upsert_transaction(
            BankTransaction(
                day="2026-07-10",
                account_id="slash:a1",
                posted_at=f"2026-07-10T0{i}:00:00Z",
                amount_usd=amount,
                description=f"txn {i}",
                category=None,
                external_id=f"slash:a1:tx{i}",
            )
        )
    rows = bs.query_transactions_day("2026-07-10", limit=2)
    assert [r["amount_usd"] for r in rows] == [-1000.0, 200.0]


def test_sum_facts_between_accumulates_flows_only(store: Any) -> None:
    bs = BankingStore(store)
    bs.upsert_fact(
        BankFact(
            day="2026-07-01",
            account_id="slash:a1",
            provider="slash",
            balance_usd=100.0,
            deposits_usd=10.0,
            expenses_usd=4.0,
            net_flow_usd=6.0,
        )
    )
    bs.upsert_fact(
        BankFact(
            day="2026-07-10",
            account_id="slash:a1",
            provider="slash",
            balance_usd=250.0,
            deposits_usd=20.0,
            expenses_usd=5.0,
            net_flow_usd=15.0,
        )
    )
    mtd = bs.sum_facts_between("2026-07-01", "2026-07-10")
    assert mtd["money_in_usd"] == 30.0
    assert mtd["money_out_usd"] == 9.0
    assert mtd["net_flow_usd"] == 21.0
