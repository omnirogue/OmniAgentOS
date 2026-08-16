from __future__ import annotations

from dataclasses import dataclass


class LedgerError(ValueError):
    """Raised when ledger invariants are violated or input is invalid."""

    pass


@dataclass(frozen=True)
class Entry:
    txn_id: str
    account: str
    delta: int
    kind: str


@dataclass
class Ledger:
    balances: dict[str, int]
    journal: list[Entry]  # append-only
    applied: set[str]  # txn ids already applied


def new_ledger(opening: dict[str, int]) -> Ledger:
    """Creates a new ledger with opening balances."""
    return Ledger(balances=dict(opening), journal=[], applied=set())


def total(ledger: Ledger) -> int:
    """Returns the total balances of the ledger."""
    return sum(ledger.balances.values())


def apply_txn(ledger: Ledger, txn_id: str, kind: str, moves: list[tuple[str, int]]) -> bool:
    """Atomically applies a transaction (list of moves) to the ledger.

    Returns True if the transaction was applied, or False if txn_id was already applied.
    Raises LedgerError if validation fails, leaving the ledger completely unchanged.
    """
    if txn_id in ledger.applied:
        return False

    if not moves:
        raise LedgerError("Transaction moves list cannot be empty")

    if kind == "transfer":
        if sum(delta for _, delta in moves) != 0:
            raise LedgerError("Transfer deltas must sum to zero")

    # Check accounts and compute resulting balances
    temp_balances = dict(ledger.balances)
    for account, delta in moves:
        if account not in temp_balances:
            raise LedgerError(f"Unknown account: {account}")
        temp_balances[account] += delta

    for account, bal in temp_balances.items():
        if bal < 0:
            raise LedgerError(
                f"Insufficient funds: resulting balance of {account} would be negative ({bal})"
            )

    # Apply atomically
    for account, delta in moves:
        ledger.journal.append(Entry(txn_id=txn_id, account=account, delta=delta, kind=kind))

    ledger.balances = temp_balances
    ledger.applied.add(txn_id)
    return True
