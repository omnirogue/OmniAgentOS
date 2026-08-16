from __future__ import annotations

from store import Ledger, LedgerError


def transfer(ledger: Ledger, txn_id: str, src: str, dst: str, amount: int) -> bool:
    """Transfer an amount from src to dst.

    Warning: This seed implementation is naive and non-atomic. It mutates balances
    directly and can leave the store torn if the credit side fails.
    """
    if amount <= 0:
        raise LedgerError("Amount must be positive")
    if src == dst:
        raise LedgerError("Source and destination must be different")
    if src not in ledger.balances:
        raise LedgerError("Source account does not exist")

    # Non-atomic mutation: first deduct from src
    if ledger.balances[src] < amount:
        raise LedgerError("Insufficient funds")

    ledger.balances[src] -= amount

    # Then attempt to credit dst (which might fail if dst does not exist)
    if dst not in ledger.balances:
        # Ledger is now in a torn/inconsistent state! src was debited, dst not credited.
        raise LedgerError("Destination account does not exist")

    ledger.balances[dst] += amount
    return True
