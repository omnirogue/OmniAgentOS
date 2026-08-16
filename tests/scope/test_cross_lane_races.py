"""Thread-level cross-lane race coverage; multiprocess coverage is in test_locks.py."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

from omniagentos.db.store import SqliteStore
from omniagentos.scope.conflict import conflicts_with
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim


def test_overlapping_cross_lane_claims_never_have_two_live_holders(tmp_path: Path) -> None:
    """Threads exercise the store's shared writer discipline; test_locks covers processes."""
    store = SqliteStore(str(tmp_path / "scope-race.db"))
    locks = PathLockStore(store)
    realm = "/tmp/cross-lane"
    holders = [
        LockHolder(kind="run", id="run-race", lane="runner"),
        LockHolder(kind="session", id="session-race", lane="session"),
        LockHolder(kind="swarm_task", id="swarm-race", lane="swarm"),
        LockHolder(kind="coordinator", id="coord-race", lane="longhaul"),
    ]
    claims = [
        ScopeClaim.for_path(realm, "src", kind="root"),
        ScopeClaim.for_path(realm, "src/worker.py"),
        ScopeClaim.for_path(realm, "src/api.py"),
        ScopeClaim.for_path(realm, "docs", kind="root"),
    ]
    start = Barrier(len(holders))
    inspection = Lock()

    def assert_no_foreign_conflicts() -> None:
        live = locks.held_in_realm(realm)
        for index, left in enumerate(live):
            for right in live[index + 1 :]:
                if left.holder != right.holder:
                    assert conflicts_with(left.as_claim(), right.as_claim()) is None

    def contend(index: int) -> None:
        holder = holders[index]
        start.wait()
        for round_number in range(30):
            result = locks.try_acquire_scope(
                [claims[(index + round_number) % len(claims)]], holder, enforce=True, ttl_s=30
            )
            if result.granted:
                with inspection:
                    assert_no_foreign_conflicts()
                locks.release_scope(holder.kind, holder.id, 0)

    try:
        with ThreadPoolExecutor(max_workers=len(holders)) as pool:
            list(pool.map(contend, range(len(holders))))
        assert_no_foreign_conflicts()
    finally:
        store.close()
