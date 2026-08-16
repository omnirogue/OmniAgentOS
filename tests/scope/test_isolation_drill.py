"""T7.4 partial: worktree separation plus the durable lock-layer backstop.

This drill covers logical-path exclusion across two simulated worktrees. Full
macOS Seatbelt denial is intentionally optional and out of scope for this
partial; Linux CI must not depend on Seatbelt availability.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.store import SqliteStore
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim


def test_isolation_drill_blocks_overlap_and_allows_disjoint_worktrees(tmp_path: Path) -> None:
    """Separate trees isolate writes while one realm lock table excludes overlap."""
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    for worktree in (worktree_a, worktree_b):
        (worktree / "src").mkdir(parents=True)
    (worktree_a / "src" / "owned.py").write_text("A\n", encoding="utf-8")
    (worktree_b / "src" / "owned.py").write_text("B\n", encoding="utf-8")

    store = SqliteStore(str(tmp_path / "scope-locks.db"))
    locks = PathLockStore(store)
    realm = "project-realm"
    run_a = LockHolder(kind="run", id="run-a", lane="runner")
    run_b = LockHolder(kind="swarm_task", id="run-b", lane="swarm")
    try:
        held = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/owned.py")], run_a, enforce=True
        )
        overlap = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/owned.py")], run_b, enforce=True
        )

        assert held.granted
        assert overlap.status == "blocked"
        assert (worktree_a / "src" / "owned.py").read_text(encoding="utf-8") == "A\n"
        assert (worktree_b / "src" / "owned.py").read_text(encoding="utf-8") == "B\n"
    finally:
        store.close()


def test_isolation_drill_grants_disjoint_logical_paths(tmp_path: Path) -> None:
    """Separate runs may progress concurrently when their logical paths do not meet."""
    store = SqliteStore(str(tmp_path / "scope-locks.db"))
    locks = PathLockStore(store)
    realm = "project-realm"
    try:
        first = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/a.py")],
            LockHolder(kind="run", id="run-a", lane="runner"),
            enforce=True,
        )
        second = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "docs/b.md")],
            LockHolder(kind="swarm_task", id="run-b", lane="swarm"),
            enforce=True,
        )

        assert first.granted
        assert second.granted
    finally:
        store.close()
