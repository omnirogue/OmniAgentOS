from __future__ import annotations

from store import Ledger, LedgerError


def withdraw(ledger: Ledger, txn_id: str, account: str, amount: int) -> bool:
    """Withdraw an amount from an account.

    Warning: This seed implementation is naive and mutates balances directly
    with no journal logging, no idempotency, and no transaction safety.
    """
    if amount <= 0:
        raise LedgerError("Amount must be positive")
    if account not in ledger.balances:
        raise LedgerError("Account does not exist")
    if ledger.balances[account] < amount:
        raise LedgerError("Insufficient funds")
    ledger.balances[account] -= amount
    return True
