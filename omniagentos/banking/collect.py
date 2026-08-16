"""Per-provider, per-account cash collection: balances, deposits, true expenses.

Populates ``bank_accounts`` / ``bank_facts`` / ``bank_transactions`` for one
*Eastern* calendar day. Structurally a sibling of the revenue collector, and it
inherits the same load-bearing rule — **per-source isolation**: each provider,
each API key, and each account is collected inside its own try/except. One bank
failing records a ``data_quality`` note and is skipped; it never sinks the run and
never zeroes another account (the store UPSERTs, so a skipped account keeps its
last figure).

Context that shapes what this module records: a typical Slash setup sweeps
incoming cash to auto-pay a charge card daily, so the CASH sub-ledger balance is
genuinely ~$0.00 while the spendable reserve is the CREDIT sub-ledger's
``available`` figure. ``GET /account/{id}/balance`` returns both sub-ledgers in
one call, so this collector also records ``available_credit_usd`` (credit
``available``) and ``card_balance_usd`` (credit ``posted`` — current balance
owed) on ``bank_facts`` (migration 021), alongside the cash balance/flows.

READ-ONLY. Every external call is a GET through the broker. This package has no
path to ``slash_bank.transfer`` or any money-movement capability, by construction.

The ET day window and day math come from :mod:`omniagentos.goals.collect` so the
timezone handling is single-sourced with the rest of the system.

Slash response-shape (confirmed against the api.joinslash.com API):
  * Lists are wrapped under ``items`` with paging in ``metadata.nextCursor``.
  * ``GET /account`` records carry NO balance — the balance lives at the
    sub-resource ``GET /account/{id}/balance`` as a per-sub-ledger list
    (``type`` cash/credit) with ``available.amountCents`` / ``posted.amountCents``.
  * Transactions carry ``amountCents`` (integer minor units), signed
    (negative = debit/expense, positive = credit/deposit); there is no direction
    field. ``status`` is ``posted`` for settled money, ``failed`` for declined
    card auths, ``pending`` for unsettled — and most rows are declined auths,
    so only settled ``posted`` rows on the ``cash`` sub-ledger are real cash.
All Slash parsing is isolated in the small ``_slash_*`` helpers below, and money
units are governed by the single ``SLASH_AMOUNTS_IN_MINOR_UNITS`` switch (confirmed
cents).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omniagentos.banking.store import BankAccount, BankFact, BankingStore, BankTransaction
from omniagentos.connectors import broker, load_registry
from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.goals.collect import _body, _day_bounds, _number, _skip_reason, eastern_yesterday

logger = logging.getLogger(__name__)


def _configured(cap_id: str, env_name: str) -> bool:
    """Ask the broker whether one source credential is available.

    This is deliberately a resolution rather than an ambient-environment probe:
    it produces a durable audit row even for a skipped source and lets the
    secret catalog/permission guard refuse a quarantined key before collection.
    """
    try:
        return bool(broker.resolve_one_for(load_registry().capability(cap_id), env_name))
    except broker.BrokerDenied:
        return False

# Safety bound on transaction pagination: 50 pages is far more than a day of
# activity for these accounts, and stops a broken/looping cursor from paging
# forever.
_MAX_PAGES = 50

# Slash returns money as integer minor units (cents) — VERIFIED 2026-07-21: a
# ``amountCents`` of -20000 is a $200.00 charge, matched by originalCurrency
# {amountCents: 20000, code: USD}. This is the single place that assumption lives;
# flip it if a live pull ever shows decimal dollars instead.
SLASH_AMOUNTS_IN_MINOR_UNITS = True

# The broker resolves auth via bearer / basic / query / header schemes only
# (see omniagentos/connectors/broker.py:_auth_headers). It has NO client-cert /
# mutual-TLS plumbing, which is what Teller requires. Until that is wired, Teller
# cannot authenticate, so the collector skips it honestly rather than making a
# doomed live call. This flag is the one place that fact is asserted.
BROKER_SUPPORTS_CLIENT_CERT = True


@dataclass(frozen=True)
class SlashSource:
    """One Slash API key and the read capability that fronts it.

    Each key is (per the operator's setup) a separate Slash account/brand. The
    ``key_env`` is only probed for presence to decide whether to attempt the
    source — the credential itself is resolved by the broker, never read here.
    """

    brand: str
    cap: str
    key_env: str


# Ordered primary-first so that if a brand key happens to point at the same Slash
# org as the primary key, the brand label wins on the shared account (accounts and
# transactions dedupe by id regardless, so totals are never double-counted).
SLASH_SOURCES: tuple[SlashSource, ...] = (
    SlashSource(brand="primary", cap="slash_bank.read", key_env="SLASH_API_KEY"),
    SlashSource(brand="AcmeUni", cap="slash_bank.read_acmeuni", key_env="SLASH_API_KEY_ACMEUNI"),
    SlashSource(
        brand="Initech", cap="slash_bank.read_initech", key_env="SLASH_API_KEY_INITECH"
    ),
)


@dataclass
class Note:
    """A data-quality note surfaced to the operator. ``level`` is 'warn' or 'info'."""

    level: str
    message: str

    def row(self) -> dict[str, str]:
        return {"level": self.level, "message": self.message}


@dataclass
class AccountCollection:
    """The fully-collected picture for one account on one ET day."""

    account: BankAccount
    fact: BankFact
    transactions: list[BankTransaction] = field(default_factory=list)


@dataclass
class CollectionResult:
    day: str
    accounts: list[AccountCollection] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)


class SourceError(Exception):
    """A controlled per-source failure. Caught by the isolation wrapper."""


# --------------------------------------------------------------------------- #
# Generic parsing helpers                                                     #
# --------------------------------------------------------------------------- #
def _as_list(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """Pull a list of records from a payload that may nest it under any of keys."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _epoch(value: Any) -> float | None:
    """Best-effort UTC epoch seconds from an int/float epoch or an ISO-8601 string."""
    if isinstance(value, (int, float)):
        # Heuristic: values that look like milliseconds get scaled to seconds.
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str) and value:
        text = value.strip()
        if text.replace(".", "", 1).isdigit():
            return _epoch(float(text))
        try:
            iso = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Slash — the one provider that returns live data today                        #
# --------------------------------------------------------------------------- #
def _slash_money(raw: Any) -> float:
    """Slash amount -> USD dollars, honouring ``SLASH_AMOUNTS_IN_MINOR_UNITS``."""
    amount = _number(raw)
    return amount / 100.0 if SLASH_AMOUNTS_IN_MINOR_UNITS else amount


def _slash_account(source: SlashSource, record: dict[str, Any]) -> BankAccount:
    external_id = str(_first(record, "id", "account_id", "uuid") or "")
    mask = _first(record, "last_four", "last4", "mask", "number_last4")
    kind = _first(record, "kind", "type", "account_type")
    return BankAccount(
        id=f"slash:{external_id}",
        provider="slash",
        brand=source.brand,
        name=str(_first(record, "name", "nickname", "label") or "Slash account"),
        mask=None if mask is None else str(mask),
        currency=str(_first(record, "currency") or "usd").lower(),
        kind=None if kind is None else str(kind).lower(),
    )


def _slash_ledger_entries(cap: str, external_id: str) -> list[dict[str, Any]]:
    """Raw per-sub-ledger balance entries for a Slash account.

    VERIFIED shape (``GET /account/{id}/balance``): ``{"balances": [{"type":
    "cash", "available": {"amountCents": N}, "posted": {"amountCents": N}},
    {"type": "credit", "available": {...}, "posted": {...}}]}``. Slash does NOT
    inline a balance on the ``/account`` list record, so both the cash balance
    AND the credit balance are read from this one sub-resource — fetched once
    per account and shared by :func:`_slash_cash_balance` /
    :func:`_slash_ledger_amount` below. Returns ``[]`` on any failure so callers
    degrade to "unknown"/``None`` rather than raising; isolated so a
    balance-endpoint failure here never sinks the account's flows.
    """
    try:
        result = broker.call(cap, [cap], method="GET", path=f"/account/{external_id}/balance")
    except Exception as exc:  # noqa: BLE001 - stay isolated; report unknown
        logger.warning("Slash balance fetch failed for %s: %s", external_id, exc)
        return []
    payload = _body(result)
    return _as_list(payload, "balances", "data", "items") if payload else []


def _slash_cash_balance(entries: list[dict[str, Any]]) -> tuple[float, str]:
    """Latest CASH balance from the sub-ledger ``entries`` (see ``_slash_ledger_entries``).

    For the Cash dashboard we take the ``cash`` sub-ledger's available balance
    (falling back to its posted amount, then to the first entry). Returns
    ``(usd, balance_kind)``; ``(0.0, "unknown")`` if the balance could not be
    read, so the caller can fall back and the fact is flagged rather than a
    false 0 shown.
    """
    if not entries:
        return 0.0, "unknown"
    chosen = next((e for e in entries if str(_first(e, "type") or "").lower() == "cash"), None)
    chosen = chosen or entries[0]
    container = chosen.get("available") if isinstance(chosen.get("available"), dict) else None
    container = container or (
        chosen.get("posted") if isinstance(chosen.get("posted"), dict) else None
    )
    raw = _first(container or chosen, "amountCents", "amount", "value")
    if raw is None:
        return 0.0, "unknown"
    kind = str(_first(chosen, "type") or "cash").lower()
    return round(_slash_money(raw), 2), f"{kind}_available"


def _slash_ledger_amount(
    entries: list[dict[str, Any]], ledger_type: str, field_name: str
) -> float | None:
    """One sub-ledger's ``field_name`` (``"available"`` or ``"posted"``) in USD.

    Used to pull the CREDIT sub-ledger's ``available.amountCents`` (available
    credit — the spendable reserve when cash sweeps daily to auto-pay the
    card) and ``posted.amountCents`` (current card balance owed). Returns
    ``None`` — not ``0.0`` — when the account has no such sub-ledger or the
    figure could not be read, so a summed report never confuses "not captured"
    with "zero available".
    """
    chosen = next((e for e in entries if str(_first(e, "type") or "").lower() == ledger_type), None)
    if chosen is None:
        return None
    container = chosen.get(field_name) if isinstance(chosen.get(field_name), dict) else None
    raw = _first(container or {}, "amountCents", "amount", "value")
    if raw is None:
        return None
    return round(_slash_money(raw), 2)


def _slash_balance_usd(record: dict[str, Any]) -> tuple[float, str]:
    """Fallback: latest balance inlined on a Slash account record, if any.

    The live ``/account`` list does NOT inline balances (see
    ``_slash_balance_for_account`` for the real source), but this is kept as a
    defensive fallback in case a future/other Slash shape embeds one. The field it
    resolved is returned so the report can record ``balance_kind`` and never
    silently blend a card's liability into cash.
    """
    for key in ("available_balance", "current_balance", "balance", "amount"):
        value = record.get(key)
        if isinstance(value, dict):  # e.g. {"amount": 12345, "currency": "usd"}
            inner = _first(value, "amount", "value")
            if inner is not None:
                return round(_slash_money(inner), 2), key
        elif value is not None:
            return round(_slash_money(value), 2), key
    return 0.0, "unknown"


def _slash_txn_signed_usd(record: dict[str, Any]) -> float:
    """Signed USD for a Slash transaction: + deposit (credit), − expense (debit).

    Uses an explicit direction/type field when present; otherwise falls back to
    the sign of the raw amount (Slash convention: debits negative). Centralised
    here so the credit/debit rule is defined exactly once.
    """
    signed = _slash_money(_first(record, "amountCents", "amount", "amount_usd", "value") or 0)
    direction = str(_first(record, "direction", "type", "flow") or "").lower()
    if any(token in direction for token in ("credit", "deposit", "inbound", "in")):
        return abs(signed)
    if any(token in direction for token in ("debit", "withdrawal", "outbound", "out", "expense")):
        return -abs(signed)
    return signed


def _slash_txn_settled(record: dict[str, Any]) -> bool:
    """True only for transactions that actually moved bank CASH.

    VERIFIED: Slash returns declined card authorizations (``status`` ``failed`` /
    ``detailedStatus`` ``declined``) and unsettled ``pending`` rows interleaved with
    real postings — on typical accounts most rows are declined auths. Counting
    those as expenses would grossly overstate cash out, so keep only settled
    (``posted``) rows. We also keep only the ``cash`` sub-ledger: ``credit``-ledger
    rows are charge-card activity, not bank cash, and their cash-side effect posts
    separately (the "Daily Credit Card Payment" debit). Lenient when a field is
    absent so non-Slash / future shapes are unaffected.
    """
    status = str(_first(record, "status", "detailedStatus") or "").lower()
    if status and status not in ("posted", "settled", "completed", "complete"):
        return False
    subtype = str(_first(record, "accountSubtype", "account_subtype", "subtype") or "").lower()
    if subtype and subtype != "cash":
        return False
    return True


def _slash_txn_external_id(account_id: str, record: dict[str, Any]) -> str:
    external = _first(record, "id", "transaction_id", "uuid")
    if external is not None:
        return f"{account_id}:{external}"
    # No stable id from the provider — synthesize a deterministic one so a
    # re-collect UPSERTs the same row instead of double-counting the money.
    stamp = _first(record, "posted_at", "created_at", "date", "timestamp") or ""
    amount = _first(record, "amountCents", "amount", "value") or ""
    desc = _first(record, "description", "memo", "name") or ""
    return f"{account_id}:{stamp}:{amount}:{desc}"


def _slash_list_accounts(cap: str) -> list[dict[str, Any]]:
    try:
        result = broker.call(cap, [cap], method="GET", path="/account")
    except Exception as exc:
        raise SourceError(f"broker denied or unavailable: {exc}") from exc
    payload = _body(result)
    if payload is None:
        raise SourceError(_skip_reason(result))
    accounts = _as_list(payload, "items", "data", "accounts", "results")
    if not accounts and isinstance(payload, dict) and payload.get("id"):
        accounts = [payload]  # a single-account payload returned bare
    return accounts


def _slash_transactions(cap: str, start: int, end: int) -> tuple[list[dict[str, Any]], bool]:
    """Fetch transactions overlapping the ET window via cursor pagination.

    Slash paginates with ``?cursor=`` and no date filter, so we page and filter
    client-side to ``[start, end]``. Returns ``(rows_in_window, truncated)``;
    ``truncated`` flags that the page budget was exhausted, so an undercount is
    surfaced rather than silently trusted.
    """
    in_window: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = True
    for _ in range(_MAX_PAGES):
        query: dict[str, Any] = {}
        if cursor:
            query["cursor"] = cursor
        try:
            result = broker.call(cap, [cap], method="GET", path="/transaction", query=query)
        except Exception as exc:
            raise SourceError(f"broker denied or unavailable: {exc}") from exc
        payload = _body(result)
        if payload is None:
            raise SourceError(_skip_reason(result))
        page = _as_list(payload, "items", "data", "transactions", "results")
        oldest_before_window = False
        for row in page:
            epoch = _epoch(_first(row, "posted_at", "created_at", "date", "timestamp"))
            if epoch is None:
                in_window.append(row)  # undated: keep, let the day filter decide
                continue
            if start <= epoch <= end:
                in_window.append(row)
            elif epoch < start:
                oldest_before_window = True
        # VERIFIED: Slash pages via ``metadata.nextCursor`` (camelCase, nested),
        # not a top-level cursor. Fall back to a top-level cursor for other shapes.
        meta = payload.get("metadata") if isinstance(payload, dict) else None
        cursor = (
            _first(meta, "nextCursor", "next_cursor", "cursor", "next")
            if isinstance(meta, dict)
            else None
        )
        if cursor is None and isinstance(payload, dict):
            cursor = _first(payload, "cursor", "next_cursor", "next")
        cursor = str(cursor) if cursor else None
        if not page or oldest_before_window or not cursor:
            truncated = False
            break
    return in_window, truncated


def _collect_slash_account(
    source: SlashSource,
    record: dict[str, Any],
    day: str,
    start: int,
    end: int,
    shared_txns: list[dict[str, Any]],
    shared_truncated: bool,
) -> AccountCollection:
    account = _slash_account(source, record)
    external_id = account.id.split(":", 1)[1] if ":" in account.id else account.id
    entries = _slash_ledger_entries(source.cap, external_id)
    balance, balance_kind = _slash_cash_balance(entries)
    if balance_kind == "unknown":
        # Defensive fallback for a shape that inlines a balance on the record.
        balance, balance_kind = _slash_balance_usd(record)
    # CREDIT sub-ledger: the spendable reserve on a setup that sweeps cash
    # to auto-pay a charge card daily (available), and what is currently owed
    # on that card (posted). Both None when the account has no credit ledger.
    available_credit = _slash_ledger_amount(entries, "credit", "available")
    card_balance = _slash_ledger_amount(entries, "credit", "posted")

    txn_rows, truncated = _scope_transactions(account.id, record, shared_txns, shared_truncated)

    deposits = 0.0
    expenses = 0.0
    transactions: list[BankTransaction] = []
    for row in txn_rows:
        if not _slash_txn_settled(row):
            continue  # declined auth / pending / non-cash ledger — never moved bank cash
        signed = round(_slash_txn_signed_usd(row), 2)
        if signed >= 0:
            deposits += signed
        else:
            expenses += -signed
        posted_raw = _first(row, "posted_at", "created_at", "date", "timestamp")
        transactions.append(
            BankTransaction(
                day=day,
                account_id=account.id,
                posted_at=None if posted_raw is None else str(posted_raw),
                amount_usd=signed,
                description=(
                    None
                    if _first(row, "description", "memo", "name") is None
                    else str(_first(row, "description", "memo", "name"))
                ),
                category=(
                    None if _first(row, "category") is None else str(_first(row, "category"))
                ),
                external_id=_slash_txn_external_id(account.id, row),
            )
        )

    meta: dict[str, Any] = {"balance_kind": balance_kind}
    if truncated:
        meta["truncated"] = True
    fact = BankFact(
        day=day,
        account_id=account.id,
        provider="slash",
        balance_usd=round(balance, 2),
        deposits_usd=round(deposits, 2),
        expenses_usd=round(expenses, 2),
        net_flow_usd=round(deposits - expenses, 2),
        txn_count=len(transactions),
        meta=meta,
        available_credit_usd=available_credit,
        card_balance_usd=card_balance,
    )
    return AccountCollection(account=account, fact=fact, transactions=transactions)


def _scope_transactions(
    account_id: str,
    account_record: dict[str, Any],
    shared_txns: list[dict[str, Any]],
    shared_truncated: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Resolve the in-window transactions belonging to one account.

    Prefers activity embedded on the account record (some Slash responses inline
    recent transactions); otherwise takes the account's slice of the key-level
    ``/transaction`` pull. When the shared pull carries no per-row account id and
    the key exposes a single account, the whole slice belongs to it.
    """
    embedded = _as_list(account_record, "transactions", "recent_transactions")
    if embedded:
        return embedded, False

    external = account_id.split(":", 1)[1] if ":" in account_id else account_id
    scoped = [
        row
        for row in shared_txns
        if str(_first(row, "accountId", "account_id", "account", "bank_account_id") or external)
        == external
    ]
    return scoped, shared_truncated


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def collect_day(
    store: SqliteStore,
    *,
    target_day: date | None = None,
    dry_run: bool = False,
) -> CollectionResult:
    """Collect every configured bank/account for one ET day and UPSERT the facts.

    Per-source isolation: a failure in one key or account is recorded as a
    ``data_quality`` note and skipped; the run always completes.
    """
    day_obj = target_day or eastern_yesterday()
    day = day_obj.isoformat()
    start, end, _ = _day_bounds(day_obj)
    result = CollectionResult(day=day)

    # --- Slash (per key / per brand) ---
    for source in SLASH_SOURCES:
        if not _configured(source.cap, source.key_env):
            result.notes.append(
                Note("info", f"Slash {source.brand}: {source.key_env} not configured — skipped")
            )
            continue
        try:
            records = _slash_list_accounts(source.cap)
        except Exception as exc:
            result.notes.append(
                Note(
                    "warn",
                    f"Slash {source.brand} account list failed: {exc} — recorded nothing, "
                    "prior figures left intact",
                )
            )
            continue
        if not records:
            result.notes.append(Note("info", f"Slash {source.brand}: no accounts returned"))
            continue

        # Fetch the key's transactions once; distribute per account below. A
        # failure here still lets balances through (recorded with empty flows).
        shared_txns: list[dict[str, Any]] = []
        shared_truncated = False
        try:
            shared_txns, shared_truncated = _slash_transactions(source.cap, start, end)
        except Exception as exc:
            result.notes.append(
                Note(
                    "warn",
                    f"Slash {source.brand} transactions failed: {exc} — balances recorded, "
                    "deposits/expenses for the day are incomplete",
                )
            )

        for record in records:
            try:
                collection = _collect_slash_account(
                    source, record, day, start, end, shared_txns, shared_truncated
                )
            except Exception as exc:
                result.notes.append(
                    Note("warn", f"Slash {source.brand} account collection failed: {exc}")
                )
                continue
            result.accounts.append(collection)

    # --- Teller (linked banks) — degrade gracefully, do not make a doomed call ---
    _collect_teller(result)

    if not dry_run:
        _persist(store, result)

    return result


def _teller_account(record: dict[str, Any]) -> BankAccount:
    """Parse a Teller account record into a BankAccount."""
    external_id = str(_first(record, "id", "account_id", "uuid") or "")
    mask = _first(record, "last_four", "last4", "mask", "number_last4")
    kind = _first(record, "kind", "type", "account_type")
    return BankAccount(
        id=f"teller:{external_id}",
        provider="teller",
        brand="teller",
        name=str(_first(record, "name", "nickname", "label") or "Teller account"),
        mask=None if mask is None else str(mask),
        currency=str(_first(record, "currency") or "usd").lower(),
        kind=None if kind is None else str(kind).lower(),
    )


def _teller_balance_usd(record: dict[str, Any]) -> tuple[float, str]:
    """Extract Teller's decimal-dollar ``available`` or ``ledger`` balance."""
    for key in ("available", "ledger"):
        raw = record.get(key)
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            continue
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            continue
        if amount.is_finite():
            return round(float(amount), 2), key
    return 0.0, "unknown"


def _teller_payload(result: dict[str, Any]) -> Any:
    """Return a successful Teller JSON body, including its documented bare lists."""
    if not result.get("ok") or int(result.get("status", 0)) >= 400:
        raise SourceError(_skip_reason(result))
    return result.get("body")


def _teller_rows(payload: Any, *wrapper_keys: str) -> list[dict[str, Any]]:
    """Validate a Teller list response without silently dropping malformed rows."""
    value = payload
    if isinstance(payload, dict):
        value = next((payload[key] for key in wrapper_keys if key in payload), None)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceError("invalid Teller list response shape")
    return value


def _teller_list_accounts(cap: str) -> list[dict[str, Any]]:
    """Fetch the list of accounts from Teller via the broker."""
    try:
        result = broker.call(cap, [cap], method="GET", path="/accounts")
    except Exception as exc:
        raise SourceError(f"broker denied or unavailable: {exc}") from exc
    return _teller_rows(_teller_payload(result), "accounts", "data", "items")


def _teller_balance(cap: str, external_id: str) -> tuple[float, str]:
    """Fetch one account's ``{available, ledger}`` decimal-dollar balance."""
    try:
        result = broker.call(cap, [cap], method="GET", path=f"/accounts/{external_id}/balances")
    except Exception as exc:
        raise SourceError(f"broker denied or unavailable: {exc}") from exc
    payload = _teller_payload(result)
    if not isinstance(payload, dict):
        raise SourceError("invalid Teller balance response shape")
    balance, kind = _teller_balance_usd(payload)
    if kind == "unknown":
        raise SourceError("invalid Teller balance response shape")
    return balance, kind


def _teller_transactions(cap: str, external_id: str, day: str) -> list[dict[str, Any]]:
    """Fetch and validate one account's transactions for a calendar day."""
    try:
        result = broker.call(cap, [cap], method="GET", path=f"/accounts/{external_id}/transactions")
    except Exception as exc:
        raise SourceError(f"broker denied or unavailable: {exc}") from exc
    rows = _teller_rows(_teller_payload(result), "transactions", "data", "items")
    in_window: list[dict[str, Any]] = []
    for row in rows:
        expected_keys = {"id", "account_id", "amount", "date", "description", "details", "status"}
        required = (row.get("id"), row.get("account_id"), row.get("amount"), row.get("date"))
        if not expected_keys.issubset(row) or any(value is None for value in required):
            raise SourceError("invalid Teller transaction response shape")
        if str(row["account_id"]) != external_id:
            raise SourceError("Teller transaction account_id does not match request path")
        try:
            amount = Decimal(str(row["amount"]))
        except InvalidOperation as exc:
            raise SourceError("invalid Teller transaction amount") from exc
        if (
            not amount.is_finite()
            or not isinstance(row["date"], str)
            or not isinstance(row["status"], str)
            or not isinstance(row["details"], dict)
        ):
            raise SourceError("invalid Teller transaction response shape")
        try:
            transaction_day = date.fromisoformat(row["date"])
        except ValueError as exc:
            raise SourceError("invalid Teller transaction date") from exc
        if transaction_day.isoformat() == day:
            in_window.append(row)
    return in_window


def _teller_txn_signed_usd(record: dict[str, Any]) -> float:
    """Signed USD for a Teller transaction: + deposit (credit), − expense (debit).

    Teller returns signed decimal-dollar strings, not integer minor units.
    """
    return float(Decimal(str(record["amount"])))


def _teller_txn_settled(record: dict[str, Any]) -> bool:
    """True only for transactions that actually moved bank cash.

    Teller's status field indicates settlement state.
    """
    status = str(_first(record, "status", "detailedStatus") or "").lower()
    if status and status not in ("posted", "settled", "completed", "complete"):
        return False
    return True


def _teller_txn_external_id(account_id: str, record: dict[str, Any]) -> str:
    """Generate a stable external ID for a Teller transaction."""
    external = _first(record, "id", "transaction_id", "uuid")
    if external is not None:
        return f"{account_id}:{external}"
    # Fallback: synthesize a deterministic ID
    stamp = _first(record, "posted_at", "created_at", "date", "timestamp") or ""
    amount = _first(record, "amount") or ""
    desc = _first(record, "description", "memo", "name") or ""
    return f"{account_id}:{stamp}:{amount}:{desc}"


def _collect_teller(result: CollectionResult) -> None:
    """Collect Teller linked-bank accounts and transactions when certs/token are configured.

    Teller uses mutual-TLS client cert authentication plus HTTP Basic auth with the
    access token as username. If all required env vars (TELLER_CERT_PATH,
    TELLER_CERT_KEY_PATH, TELLER_ACCESS_TOKEN) are present, pull accounts and
    transactions; otherwise skip gracefully with a data_quality note.
    """
    # Check if all required env vars are present
    cert_path = _configured("teller.read", "TELLER_CERT_PATH")
    key_path = _configured("teller.read", "TELLER_CERT_KEY_PATH")
    access_token = _configured("teller.read", "TELLER_ACCESS_TOKEN")

    if not (BROKER_SUPPORTS_CLIENT_CERT and cert_path and key_path and access_token):
        reason = (
            "broker has no client-cert (mTLS) support"
            if not BROKER_SUPPORTS_CLIENT_CERT
            else "TELLER_CERT_PATH / TELLER_CERT_KEY_PATH / TELLER_ACCESS_TOKEN not configured"
        )
        result.notes.append(
            Note(
                "warn",
                f"Teller cert auth not available ({reason}) — linked-bank balances/transactions "
                "are NOT included. Configure Teller certs and access token to enable.",
            )
        )
        return

    try:
        records = _teller_list_accounts("teller.read")
    except Exception as exc:
        result.notes.append(Note("warn", f"Teller account list failed: {exc} — recorded nothing"))
        return

    if not records:
        result.notes.append(Note("info", "Teller: no accounts returned"))
        return

    # Fetch each account's restricted balance and transaction subresources.
    for record in records:
        try:
            external_id = str(record.get("id") or "")
            if not external_id:
                result.notes.append(
                    Note("warn", "Teller account response missing id — account skipped")
                )
                continue
            account = _teller_account(record)
            quality: list[str] = []
            try:
                balance, balance_kind = _teller_balance("teller.read", external_id)
            except Exception as exc:
                balance, balance_kind = 0.0, "unknown"
                quality.append(f"balance unavailable: {exc}")
            try:
                txn_rows = _teller_transactions("teller.read", external_id, result.day)
            except Exception as exc:
                txn_rows = []
                quality.append(f"transactions unavailable: {exc}")

            deposits = 0.0
            expenses = 0.0
            transactions: list[BankTransaction] = []
            for row in txn_rows:
                if not _teller_txn_settled(row):
                    continue
                signed = round(_teller_txn_signed_usd(row), 2)
                if signed >= 0:
                    deposits += signed
                else:
                    expenses += -signed
                posted_raw = _first(row, "posted_at", "created_at", "date", "timestamp")
                transactions.append(
                    BankTransaction(
                        day=result.day,
                        account_id=account.id,
                        posted_at=None if posted_raw is None else str(posted_raw),
                        amount_usd=signed,
                        description=(
                            None
                            if _first(row, "description", "memo", "name") is None
                            else str(_first(row, "description", "memo", "name"))
                        ),
                        category=(
                            None
                            if _first(row, "category") is None
                            else str(_first(row, "category"))
                        ),
                        external_id=_teller_txn_external_id(account.id, row),
                    )
                )

            source_status = "ok" if not quality else "partial"
            meta: dict[str, Any] = {
                "balance_kind": balance_kind,
                "source_status": source_status,
                "data_quality": quality,
            }
            fact = BankFact(
                day=result.day,
                account_id=account.id,
                provider="teller",
                balance_usd=round(balance, 2),
                deposits_usd=round(deposits, 2),
                expenses_usd=round(expenses, 2),
                net_flow_usd=round(deposits - expenses, 2),
                txn_count=len(transactions),
                meta=meta,
            )
            result.accounts.append(
                AccountCollection(account=account, fact=fact, transactions=transactions)
            )
            if quality:
                result.notes.append(
                    Note(
                        "warn",
                        f"Teller account {external_id} source_status=partial — "
                        + "; ".join(quality),
                    )
                )
        except Exception as exc:
            result.notes.append(Note("warn", f"Teller account collection failed: {exc}"))
            continue


def _persist(store: SqliteStore, result: CollectionResult) -> None:
    banking = BankingStore(store)
    for collection in result.accounts:
        banking.upsert_account(collection.account)
        banking.upsert_fact(collection.fact)
        for txn in collection.transactions:
            banking.upsert_transaction(txn)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect bank cash facts (balances / deposits / true expenses)"
    )
    parser.add_argument("--once", action="store_true", help="run one collection pass")
    parser.add_argument("--dry-run", action="store_true", help="report facts without writing")
    parser.add_argument(
        "--date", type=_parse_day, help="Eastern (America/New_York) target date (YYYY-MM-DD)"
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")
    result = collect_day(SqliteStore(default_db_path()), target_day=args.date, dry_run=args.dry_run)
    summary = {
        "day": result.day,
        "accounts": len(result.accounts),
        "accounts_written": 0 if args.dry_run else len(result.accounts),
        "transactions": sum(len(c.transactions) for c in result.accounts),
        "data_quality": [note.row() for note in result.notes],
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module command
    raise SystemExit(main())
