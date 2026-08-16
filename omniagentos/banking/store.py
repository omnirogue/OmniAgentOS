"""DAL for the ``bank_accounts`` / ``bank_facts`` / ``bank_transactions`` store.

A thin, composed layer over an already-configured :class:`SqliteStore`, mirroring
:class:`omniagentos.revenue.store.RevenueStore`. Every write is an idempotent
UPSERT keyed so the hourly re-collect refreshes a row in place instead of
duplicating it — critical for real-money data, where a double-counted deposit or
expense is a lie.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore


@dataclass
class BankAccount:
    """One bank account we can see, across any provider."""

    id: str
    provider: str
    brand: str | None = None
    name: str | None = None
    mask: str | None = None
    currency: str = "usd"
    kind: str | None = None


@dataclass
class BankFact:
    """One (ET calendar day, account) cash fact.

    ``expenses_usd`` is TRUE cash out (debits that posted that day), a positive
    magnitude. ``net_flow_usd`` is ``deposits_usd - expenses_usd``.

    ``available_credit_usd`` / ``card_balance_usd`` are the account's CREDIT
    sub-ledger (migration 021), captured separately from cash: on a setup that
    sweeps cash to auto-pay a charge card daily, ``balance_usd`` (cash) is
    genuinely ~$0 while ``available_credit_usd`` is the spendable reserve.
    Both are ``None`` (not 0.0) when the account has no credit sub-ledger or the
    figure could not be read — never confuse "not captured" with "zero".
    """

    day: str
    account_id: str
    provider: str
    balance_usd: float = 0.0
    deposits_usd: float = 0.0
    expenses_usd: float = 0.0
    net_flow_usd: float = 0.0
    txn_count: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    available_credit_usd: float | None = None
    card_balance_usd: float | None = None


@dataclass
class BankTransaction:
    """One posted transaction. ``amount_usd`` is signed: + deposit / − expense."""

    day: str
    account_id: str
    posted_at: str | None
    amount_usd: float
    description: str | None
    category: str | None
    external_id: str


_ACCOUNT_COLUMNS = ("id", "provider", "brand", "name", "mask", "currency", "kind", "created_at")
_FACT_COLUMNS = (
    "day",
    "account_id",
    "provider",
    "balance_usd",
    "deposits_usd",
    "expenses_usd",
    "net_flow_usd",
    "txn_count",
    "captured_at",
    "meta_json",
    "available_credit_usd",
    "card_balance_usd",
)
_TXN_COLUMNS = (
    "day",
    "account_id",
    "posted_at",
    "amount_usd",
    "description",
    "category",
    "external_id",
)


def _decode_meta(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = out.pop("meta_json", "{}") or "{}"
    try:
        out["meta"] = json.loads(raw)
    except (ValueError, TypeError):
        out["meta"] = {}
    return out


class BankingStore:
    """Upsert and read bank accounts, daily cash facts, and transactions."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    # --- accounts --------------------------------------------------------- #
    def upsert_account(self, account: BankAccount) -> None:
        """Insert or refresh one account, keyed on its provider-namespaced id."""
        created_at = utc_now_iso()
        values = {
            "id": account.id,
            "provider": account.provider,
            "brand": account.brand,
            "name": account.name,
            "mask": account.mask,
            "currency": account.currency,
            "kind": account.kind,
            "created_at": created_at,
        }
        columns = ", ".join(_ACCOUNT_COLUMNS)
        placeholders = ", ".join("?" for _ in _ACCOUNT_COLUMNS)
        # Preserve the original created_at on refresh; only overwrite mutable fields.
        updates = ", ".join(
            f"{col}=excluded.{col}" for col in _ACCOUNT_COLUMNS if col not in ("id", "created_at")
        )
        with self._store._lock:
            self._store._begin()
            try:
                self._connection.execute(
                    f"INSERT INTO bank_accounts ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {updates}",
                    tuple(values[col] for col in _ACCOUNT_COLUMNS),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise

    def get_accounts(self) -> list[dict[str, Any]]:
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT * FROM bank_accounts ORDER BY provider ASC, brand ASC, name ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    # --- daily facts ------------------------------------------------------ #
    def upsert_fact(self, fact: BankFact) -> None:
        """Insert or refresh one (day, account) fact.

        Delete-then-insert inside one transaction — the same idempotency shape
        :class:`RevenueStore` uses — so an hourly re-collect refreshes the row as
        late transactions post, never appending a second row for the day.
        """
        captured_at = utc_now_iso()
        values = {
            "day": fact.day,
            "account_id": fact.account_id,
            "provider": fact.provider,
            "balance_usd": float(fact.balance_usd),
            "deposits_usd": float(fact.deposits_usd),
            "expenses_usd": float(fact.expenses_usd),
            "net_flow_usd": float(fact.net_flow_usd),
            "txn_count": int(fact.txn_count),
            "captured_at": captured_at,
            "meta_json": json.dumps(fact.meta, separators=(",", ":"), sort_keys=True),
            "available_credit_usd": None
            if fact.available_credit_usd is None
            else float(fact.available_credit_usd),
            "card_balance_usd": None
            if fact.card_balance_usd is None
            else float(fact.card_balance_usd),
        }
        columns = ", ".join(_FACT_COLUMNS)
        placeholders = ", ".join("?" for _ in _FACT_COLUMNS)
        with self._store._lock:
            self._store._begin()
            try:
                self._connection.execute(
                    "DELETE FROM bank_facts WHERE day = ? AND account_id = ?",
                    (fact.day, fact.account_id),
                )
                self._connection.execute(
                    f"INSERT INTO bank_facts ({columns}) VALUES ({placeholders})",
                    tuple(values[col] for col in _FACT_COLUMNS),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise

    def query_facts_day(self, day: str) -> list[dict[str, Any]]:
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT * FROM bank_facts WHERE day = ? ORDER BY account_id ASC", (day,)
            ).fetchall()
        return [_decode_meta(dict(row)) for row in rows]

    def sum_facts_between(self, start_day: str, end_day: str) -> dict[str, float]:
        """Sum deposits/expenses across an inclusive ET day range (for MTD).

        Balances are NOT summed here — a running-total of daily balance snapshots
        is meaningless. Only the flows (deposits, expenses) accumulate over a month.
        """
        with self._store._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(deposits_usd), 0) AS deposits, "
                "COALESCE(SUM(expenses_usd), 0) AS expenses "
                "FROM bank_facts WHERE day >= ? AND day <= ?",
                (start_day, end_day),
            ).fetchone()
        deposits = float(row["deposits"] if row else 0.0)
        expenses = float(row["expenses"] if row else 0.0)
        return {
            "money_in_usd": round(deposits, 2),
            "money_out_usd": round(expenses, 2),
            "net_flow_usd": round(deposits - expenses, 2),
        }

    # --- transactions ----------------------------------------------------- #
    def upsert_transaction(self, txn: BankTransaction) -> None:
        """Insert or refresh one transaction, keyed on its unique external id."""
        values = {
            "day": txn.day,
            "account_id": txn.account_id,
            "posted_at": txn.posted_at,
            "amount_usd": float(txn.amount_usd),
            "description": txn.description,
            "category": txn.category,
            "external_id": txn.external_id,
        }
        columns = ", ".join(_TXN_COLUMNS)
        placeholders = ", ".join("?" for _ in _TXN_COLUMNS)
        updates = ", ".join(f"{col}=excluded.{col}" for col in _TXN_COLUMNS if col != "external_id")
        with self._store._lock:
            self._store._begin()
            try:
                self._connection.execute(
                    f"INSERT INTO bank_transactions ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(external_id) DO UPDATE SET {updates}",
                    tuple(values[col] for col in _TXN_COLUMNS),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise

    def query_transactions_day(self, day: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Transactions that posted on ``day``, largest absolute amount first."""
        sql = (
            "SELECT * FROM bank_transactions WHERE day = ? "
            "ORDER BY ABS(amount_usd) DESC, posted_at DESC"
        )
        params: tuple[Any, ...] = (day,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (day, int(limit))
        with self._store._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


__all__ = ["BankAccount", "BankFact", "BankTransaction", "BankingStore"]
