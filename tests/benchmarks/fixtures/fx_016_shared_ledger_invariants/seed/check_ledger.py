from __future__ import annotations

import sys

from ops.deposit import deposit
from store import LedgerError, new_ledger


def check_basic_deposit() -> None:
    # Set up starting ledger
    ledger = new_ledger({"alice": 100, "bob": 50})

    # Try basic deposit
    success = deposit(ledger, "tx-1", "alice", 50)
    assert success is True, "Deposit should return True"
    assert ledger.balances["alice"] == 150, "Deposit should update alice balance to 150"

    # Try negative deposit
    try:
        deposit(ledger, "tx-2", "alice", -10)
        raise AssertionError("Should raise LedgerError for non-positive amount")
    except LedgerError:
        pass

    # Try unknown account
    try:
        deposit(ledger, "tx-3", "charlie", 10)
        raise AssertionError("Should raise LedgerError for unknown account")
    except LedgerError:
        pass

    print("Basic ledger check passed successfully!")


if __name__ == "__main__":
    try:
        check_basic_deposit()
    except AssertionError as e:
        print(f"Check failed: {e}", file=sys.stderr)
        sys.exit(1)
