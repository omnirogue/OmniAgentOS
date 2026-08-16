from __future__ import annotations

from store import Ledger, LedgerError, apply_txn


def withdraw(ledger: Ledger, txn_id: str, account: str, amount: int) -> bool:
    """Withdraw an amount from an account using apply_txn."""
    if amount <= 0:
        raise LedgerError("Amount must be positive")
    return apply_txn(ledger, txn_id, "withdraw", [(account, -amount)])
