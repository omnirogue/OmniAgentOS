"""Build the ``/api/banking`` payload from stored bank facts.

Pure and side-effect free: it folds one ET day's ``bank_accounts`` /
``bank_facts`` / ``bank_transactions`` rows into the shape the Cash dashboard
reads. The honesty rules live here, and this is REAL money so they are strict:

  * ``money_out_usd`` is TRUE cash out — actual debits from the bank accounts,
    including the daily card autopay sweep — never accrued cost, COGS, or ad
    spend (those live in the revenue store).
  * ``cash_balance_usd`` is the sum of latest-available balance snapshots; the
    note says so, and a card account's balance is flagged rather than blended
    silently into cash.
  * ``available_credit_usd`` is the CREDIT sub-ledger's available balance
    (migration 021) — on a setup that sweeps cash to auto-pay a charge card
    daily, this is the REAL spendable reserve, not the ~$0 cash balance. It is
    a credit line, not owned cash, and is always labeled as such — never
    blended into ``cash_balance_usd``.
  * A balance the collector COULD NOT READ is never presented as a measured
    $0.00. The collector already marks such a fact (`meta.balance_kind ==
    "unknown"`, see `UNREAD_BALANCE_KIND`); this module surfaces it as a warn
    note naming the accounts, and withholds `CASH_SWEEP_NOTE`, which would
    otherwise supply a confident causal story for a figure nobody read.
    The figure itself is still summed — see issue #141, where whether an unread
    balance should be omitted from the total or hold its previous value is left
    to the owner as a decision about real money.
  * Coverage is honest about what is NOT wired (Teller linked banks need
    client-cert auth the broker does not yet support), so the page never implies
    the cash picture is complete.
"""

from __future__ import annotations

from typing import Any

TIMEZONE = "America/New_York"

MONEY_OUT_NOTE = (
    "Money out = actual debits from your bank accounts, incl. the daily card "
    "autopay (your true cash expenses); balances are latest available."
)

# Always attached to `available_credit_usd` so the figure never reads as owned
# cash: it is a credit line (spendable reserve), not money in the bank.
AVAILABLE_CREDIT_NOTE = (
    "Available credit is your charge-card credit line (spendable reserve), not owned cash."
)

# Surfaced only when the day's cash balance is effectively $0 and there IS
# available credit to point to — the honest explanation for why, on this
# setup, a ~$0 cash balance does not mean "no money": cash sweeps daily to
# auto-pay the charge card, so the real reserve is the available credit line.
CASH_SWEEP_NOTE = (
    "Cash sweeps daily to auto-pay your charge card — your spendable reserve is "
    "available credit, not cash."
)

# Cash balances at/under this magnitude are treated as "effectively $0" for the
# purpose of CASH_SWEEP_NOTE — a fresh collection can leave a stray cent or two
# of float rounding rather than an exact 0.0.
_ZERO_CASH_EPSILON = 0.01

# `meta.balance_kind` on a bank_fact whose balance the collector COULD NOT READ.
# `_slash_cash_balance` (banking/collect.py:232) records `(0.0, "unknown")` when
# the balance sub-resource fails, and its docstring states the intent: "the fact
# is flagged rather than a false 0 shown". Migration 020 says the same. Until
# this constant existed, nothing read the marker — the 0.0 was summed into
# `cash_balance_usd` and the day's data_quality was byte-identical to a healthy
# run, so a 503 on one account's balance endpoint was indistinguishable from
# that account genuinely holding nothing. See issue #141.
UNREAD_BALANCE_KIND = "unknown"

# Banks known to exist but NOT returning data through this collector yet. Listed
# so the dashboard never implies the cash picture is complete. Teller is first
# because its cert auth is the concrete next wiring step (see banking/collect.py).
NOT_WIRED = [
    "Teller (linked banks — broker mTLS/client-cert auth not wired)",
    "Mercury",
    "Brex",
    "Relay",
    "Wise",
]


def _round2(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _account_label(account: dict[str, Any] | None, account_id: str) -> str:
    if account is None:
        return account_id
    name = str(account.get("name") or "").strip()
    mask = str(account.get("mask") or "").strip()
    if name and mask:
        return f"{name} ••{mask}"
    return name or account_id


def build_banking_report(
    day: str,
    accounts: list[dict[str, Any]],
    day_facts: list[dict[str, Any]],
    month_to_date: dict[str, float],
    day_transactions: list[dict[str, Any]],
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Fold one day's bank facts into the ``/api/banking`` contract."""
    accounts_by_id = {str(a.get("id")): a for a in accounts}

    cash_balance = 0.0
    money_in = 0.0
    money_out = 0.0
    available_credit = 0.0
    have_credit_data = False
    by_account: list[dict[str, Any]] = []
    has_card_balance = False
    truncated = False

    unread_balances: list[str] = []

    for fact in day_facts:
        account_id = str(fact.get("account_id"))
        account = accounts_by_id.get(account_id, {})
        balance = _round2(fact.get("balance_usd"))
        deposits = _round2(fact.get("deposits_usd"))
        expenses = _round2(fact.get("expenses_usd"))

        cash_balance += balance
        money_in += deposits
        money_out += expenses

        if fact.get("available_credit_usd") is not None:
            available_credit += _round2(fact.get("available_credit_usd"))
            have_credit_data = True

        kind = str(account.get("kind") or "")
        if kind == "card":
            has_card_balance = True
        meta = fact.get("meta") or {}
        if meta.get("truncated"):
            truncated = True
        if str(meta.get("balance_kind") or "") == UNREAD_BALANCE_KIND:
            unread_balances.append(_account_label(accounts_by_id.get(account_id), account_id))

        by_account.append(
            {
                "provider": str(fact.get("provider") or account.get("provider") or "unknown"),
                "brand": str(account.get("brand") or ""),
                "name": str(account.get("name") or account_id),
                "mask": str(account.get("mask") or ""),
                "kind": kind,
                "balance_usd": balance,
                "money_in_usd": deposits,
                "money_out_usd": expenses,
            }
        )

    by_account.sort(key=lambda row: row["balance_usd"], reverse=True)

    rounded_cash_balance = _round2(cash_balance)
    rounded_available_credit = _round2(available_credit) if have_credit_data else 0.0
    # CASH_SWEEP_NOTE explains a cash balance that is REALLY ~$0 because cash swept
    # to the card. It must not be offered as the explanation for a figure nobody
    # could read: that turns a hole into an authoritative one, which is the more
    # dangerous half of the same defect.
    cash_sweep_note = (
        CASH_SWEEP_NOTE
        if abs(rounded_cash_balance) <= _ZERO_CASH_EPSILON
        and rounded_available_credit > 0
        and not unread_balances
        else None
    )

    totals = {
        "cash_balance_usd": rounded_cash_balance,
        "money_in_usd": _round2(money_in),
        "money_out_usd": _round2(money_out),
        "net_flow_usd": _round2(money_in - money_out),
        "note": MONEY_OUT_NOTE,
        # CREDIT sub-ledger (migration 021) — a credit line, not owned cash. See
        # AVAILABLE_CREDIT_NOTE / CASH_SWEEP_NOTE module docs.
        "available_credit_usd": rounded_available_credit,
        "available_credit_note": AVAILABLE_CREDIT_NOTE,
        "cash_sweep_note": cash_sweep_note,
    }

    largest_transactions = [
        {
            "account": _account_label(
                accounts_by_id.get(str(txn.get("account_id"))), str(txn.get("account_id"))
            ),
            "posted_at": None if txn.get("posted_at") is None else str(txn.get("posted_at")),
            "amount_usd": _round2(txn.get("amount_usd")),
            "description": str(txn.get("description") or ""),
        }
        for txn in day_transactions
    ]

    return {
        "day": day,
        "timezone": TIMEZONE,
        "generated_at": generated_at,
        "totals": totals,
        "month_to_date": {
            "money_in_usd": _round2(month_to_date.get("money_in_usd")),
            "money_out_usd": _round2(month_to_date.get("money_out_usd")),
            "net_flow_usd": _round2(month_to_date.get("net_flow_usd")),
        },
        "by_account": by_account,
        "largest_transactions": largest_transactions,
        "data_quality": _data_quality(
            day, day_facts, has_card_balance, truncated, unread_balances
        ),
        "coverage": _coverage(by_account),
    }


def _data_quality(
    day: str,
    day_facts: list[dict[str, Any]],
    has_card_balance: bool,
    truncated: bool,
    unread_balances: list[str],
) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []

    if unread_balances:
        named = ", ".join(sorted(unread_balances))
        notes.append(
            {
                "level": "warn",
                "message": (
                    f"The balance could not be read for {named} — those accounts are "
                    "counted as $0.00 in Total Cash Balance, which UNDERSTATES it. This "
                    "is not the same as an empty account; re-run the collector."
                ),
            }
        )

    if not day_facts:
        notes.append(
            {
                "level": "warn",
                "message": (
                    f"No bank facts stored for {day} — the collector may not have run yet, "
                    "or every bank source is unconfigured or failed. No balances are shown."
                ),
            }
        )

    # Teller is a known, deliberate gap — surface it every time so the cash total
    # is never mistaken for the whole banking picture.
    notes.append(
        {
            "level": "warn",
            "message": (
                "Teller (linked checking/savings) is NOT included: it needs mutual-TLS "
                "client-cert auth the broker does not yet support. The figures above are "
                "Slash-only until that is wired."
            ),
        }
    )

    if has_card_balance:
        notes.append(
            {
                "level": "info",
                "message": (
                    "A card account's balance is its OUTSTANDING balance (money owed), not "
                    "spendable cash — it is summed into Total Cash Balance; read that total "
                    "with that in mind."
                ),
            }
        )

    if truncated:
        notes.append(
            {
                "level": "warn",
                "message": (
                    f"Transaction pagination was truncated for {day} — deposits/expenses for "
                    "the day are an UNDERCOUNT."
                ),
            }
        )

    return notes


def _coverage(by_account: list[dict[str, Any]]) -> dict[str, list[str]]:
    live: set[str] = set()
    for row in by_account:
        provider = str(row.get("provider") or "").strip()
        brand = str(row.get("brand") or "").strip()
        if provider:
            live.add(f"{provider} ({brand})" if brand else provider)
    return {"banks_live": sorted(live), "not_wired": list(NOT_WIRED)}


__all__ = [
    "build_banking_report",
    "TIMEZONE",
    "MONEY_OUT_NOTE",
    "AVAILABLE_CREDIT_NOTE",
    "CASH_SWEEP_NOTE",
    "NOT_WIRED",
]
