from __future__ import annotations

from store import Ledger, LedgerError, apply_txn


def transfer(ledger: Ledger, txn_id: str, src: str, dst: str, amount: int) -> bool:
    """Transfer an amount from src to dst using apply_txn."""
    if amount <= 0:
        raise LedgerError("Amount must be positive")
    if src == dst:
        raise LedgerError("Source and destination must be different")
    return apply_txn(ledger, txn_id, "transfer", [(src, -amount), (dst, amount)])
