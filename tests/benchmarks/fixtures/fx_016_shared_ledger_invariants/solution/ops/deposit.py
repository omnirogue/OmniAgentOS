from __future__ import annotations

from store import Ledger, LedgerError, apply_txn


def deposit(ledger: Ledger, txn_id: str, account: str, amount: int) -> bool:
    """Deposit an amount into an account using apply_txn."""
    if amount <= 0:
        raise LedgerError("Amount must be positive")
    return apply_txn(ledger, txn_id, "deposit", [(account, amount)])
