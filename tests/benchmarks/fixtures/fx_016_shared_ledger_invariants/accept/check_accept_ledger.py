"""FROZEN acceptance check for fx_016_shared_ledger_invariants.

This file is copied in after the agent finishes to verify the solution.
The agent cannot modify or weaken this file.
"""

from __future__ import annotations

import copy

import ops.deposit
import ops.transfer
import ops.withdraw
import store
from store import Entry, LedgerError, new_ledger, total


def test_mixed_sequence_and_replay() -> None:
    # 1. mixed sequence of deposits, withdrawals and transfers, then assert balances,
    # journal length, journal replay == balances, and conserved total.
    opening_balances = {"alice": 100, "bob": 50, "charlie": 0}
    ledger = new_ledger(opening_balances)

    # Assert initial total
    assert total(ledger) == 150

    # Sequence of valid operations
    assert ops.deposit.deposit(ledger, "tx-1", "alice", 50) is True
    # alice is now 150
    assert ledger.balances["alice"] == 150

    assert ops.withdraw.withdraw(ledger, "tx-2", "bob", 20) is True
    # bob is now 30
    assert ledger.balances["bob"] == 30

    assert ops.transfer.transfer(ledger, "tx-3", "alice", "charlie", 40) is True
    # alice: 110, charlie: 40
    assert ledger.balances["alice"] == 110
    assert ledger.balances["charlie"] == 40

    # Check current balances
    expected_balances = {"alice": 110, "bob": 30, "charlie": 40}
    assert ledger.balances == expected_balances
    assert total(ledger) == 180  # 150 + 50 - 20 = 180 (transfers conserve total)

    # Check journal length (1 for deposit, 1 for withdraw, 2 for transfer = 4)
    assert len(ledger.journal) == 4

    # Journal entries:
    # 1. deposit: tx-1, alice, 50
    # 2. withdraw: tx-2, bob, -20
    # 3. transfer: tx-3, alice, -40
    # 4. transfer: tx-3, charlie, 40
    assert ledger.journal[0] == Entry("tx-1", "alice", 50, "deposit")
    assert ledger.journal[1] == Entry("tx-2", "bob", -20, "withdraw")
    assert ledger.journal[2] == Entry("tx-3", "alice", -40, "transfer")
    assert ledger.journal[3] == Entry("tx-3", "charlie", 40, "transfer")

    # Check journal replay == balances
    replayed = dict(opening_balances)
    for entry in ledger.journal:
        replayed[entry.account] += entry.delta
    assert replayed == ledger.balances


def test_failed_operations_atomicity() -> None:
    # 2. failed transfer (overdraft, unknown account, src == dst) leaves balances AND
    # journal AND applied exactly as they were — snapshot with copy.deepcopy and compare.
    opening_balances = {"alice": 100, "bob": 50, "charlie": 0}
    ledger = new_ledger(opening_balances)

    # Make a few initial changes
    assert ops.deposit.deposit(ledger, "tx-init", "charlie", 10) is True

    # Snapshot before failures
    snapshot = copy.deepcopy(ledger)

    # Overdraft transfer
    try:
        ops.transfer.transfer(ledger, "tx-fail-1", "bob", "alice", 100)
        raise AssertionError("Should raise LedgerError on overdraft")
    except LedgerError:
        pass

    # Unknown source account
    try:
        ops.transfer.transfer(ledger, "tx-fail-2", "unknown", "alice", 10)
        raise AssertionError("Should raise LedgerError on unknown source account")
    except LedgerError:
        pass

    # Unknown destination account
    try:
        ops.transfer.transfer(ledger, "tx-fail-3", "alice", "unknown", 10)
        raise AssertionError("Should raise LedgerError on unknown destination account")
    except LedgerError:
        pass

    # Source == Destination
    try:
        ops.transfer.transfer(ledger, "tx-fail-4", "alice", "alice", 10)
        raise AssertionError("Should raise LedgerError when source == destination")
    except LedgerError:
        pass

    # Verify ledger is absolutely unchanged after all failures
    assert ledger.balances == snapshot.balances
    assert ledger.journal == snapshot.journal
    assert ledger.applied == snapshot.applied


def test_idempotency() -> None:
    # 3. idempotency: the same txn id replayed through each of the three ops returns False and changes nothing.
    ledger = new_ledger({"alice": 100, "bob": 50})

    # First apply a deposit
    assert ops.deposit.deposit(ledger, "tx-idemp", "alice", 10) is True
    assert ledger.balances["alice"] == 110

    snapshot = copy.deepcopy(ledger)

    # Attempting to re-apply the same tx-idemp via deposit should return False and do nothing
    assert ops.deposit.deposit(ledger, "tx-idemp", "alice", 10) is False
    assert ledger.balances == snapshot.balances
    assert ledger.journal == snapshot.journal
    assert ledger.applied == snapshot.applied

    # Attempting to re-apply the same tx-idemp via withdraw should return False and do nothing
    assert ops.withdraw.withdraw(ledger, "tx-idemp", "alice", 10) is False
    assert ledger.balances == snapshot.balances
    assert ledger.journal == snapshot.journal
    assert ledger.applied == snapshot.applied

    # Attempting to re-apply the same tx-idemp via transfer should return False and do nothing
    assert ops.transfer.transfer(ledger, "tx-idemp", "alice", "bob", 10) is False
    assert ledger.balances == snapshot.balances
    assert ledger.journal == snapshot.journal
    assert ledger.applied == snapshot.applied


def test_routing_via_apply_txn() -> None:
    # 4. the ops modules really route through the shared entry point, by monkeypatching
    # store.apply_txn with a counting wrapper.
    ledger = new_ledger({"alice": 100, "bob": 50})

    orig_store_apply = store.apply_txn
    orig_dep_apply = getattr(ops.deposit, "apply_txn", None)
    orig_with_apply = getattr(ops.withdraw, "apply_txn", None)
    orig_trans_apply = getattr(ops.transfer, "apply_txn", None)

    call_count = 0

    def mock_apply_txn(ledger_obj, txn_id, kind, moves):
        nonlocal call_count
        call_count += 1
        return orig_store_apply(ledger_obj, txn_id, kind, moves)

    # Apply patches
    store.apply_txn = mock_apply_txn
    if hasattr(ops.deposit, "apply_txn"):
        ops.deposit.apply_txn = mock_apply_txn
    if hasattr(ops.withdraw, "apply_txn"):
        ops.withdraw.apply_txn = mock_apply_txn
    if hasattr(ops.transfer, "apply_txn"):
        ops.transfer.apply_txn = mock_apply_txn

    try:
        call_count = 0
        ops.deposit.deposit(ledger, "tx-route-1", "alice", 10)
        assert call_count == 1, f"Expected 1 call to apply_txn, got {call_count}"

        call_count = 0
        ops.withdraw.withdraw(ledger, "tx-route-2", "bob", 10)
        assert call_count == 1, f"Expected 1 call to apply_txn, got {call_count}"

        call_count = 0
        ops.transfer.transfer(ledger, "tx-route-3", "alice", "bob", 10)
        assert call_count == 1, f"Expected 1 call to apply_txn, got {call_count}"

    finally:
        # Restore patches
        store.apply_txn = orig_store_apply
        if orig_dep_apply is not None:
            ops.deposit.apply_txn = orig_dep_apply
        if orig_with_apply is not None:
            ops.withdraw.apply_txn = orig_with_apply
        if orig_trans_apply is not None:
            ops.transfer.apply_txn = orig_trans_apply
