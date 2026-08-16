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
