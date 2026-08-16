"""The lock STORE, not the algebra: durability, atomicity, fencing, FIFO.

``test_conflict.py`` proves the four-case matrix as pure functions. Everything
here re-proves it *through SQLite*, because the interesting failures are the ones
that only exist once two transactions are involved — a partially applied claim
set, a stale worker freeing its adopter's locks, an unbounded reap, a waiter
queue that reorders itself.

Two tests carry more weight than the rest:

``test_no_partial_acquire``
    Injects a failure part-way through the insert loop and asserts ZERO rows
    survive. That is the proof of the deadlock-freedom argument in
    ``omniagentos/scope/locks.py``: all-or-nothing means no hold-and-wait, and no
    hold-and-wait means Coffman's conditions cannot all hold.

``test_multiprocess_no_conflicting_locks``
    Runs N real OS PROCESSES, not threads. Threads share the store's in-process
    writer lock, which would mask exactly the class of bug this phase exists to
    fix; only separate processes actually test that ``BEGIN IMMEDIATE`` is doing
    the work.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scope import config as scope_config
from omniagentos.scope.conflict import conflicts_with
from omniagentos.scope.locks import (
    REAP_LIMIT,
    HeldLock,
    LockHolder,
    PathLockStore,
    ScopeLockBusy,
    ScopeLockError,
    ScopeLockFenced,
)
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim
from tests.support.db_template import make_store, migrated_db

REALM = "/realm/one"
OTHER_REALM = "/realm/two"

T0 = "2026-01-01T00:00:00Z"
T_LATER = "2026-01-01T00:01:00Z"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    created = make_store(SqliteStore, tmp_path / "locks.db")
    yield created
    created.close()


@pytest.fixture
def locks(store: SqliteStore) -> PathLockStore:
    return PathLockStore(store)


def runner(run_id: str = "run_1") -> LockHolder:
    return LockHolder(kind="run", id=run_id, lane="runner")


def worker(task_id: str = "swt_1") -> LockHolder:
    return LockHolder(kind="swarm_task", id=task_id, lane="swarm")


def claim(path: str, kind: str = "file", *, realm: str = REALM, **kwargs: object) -> ScopeClaim:
    return ScopeClaim.for_path(realm, path, kind=kind, **kwargs)  # type: ignore[arg-type]


def row_count(store: SqliteStore, table: str = "resource_locks") -> int:
    with store._lock:
        return int(store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------


def test_migration_adds_lease_and_scope_columns(store: SqliteStore) -> None:
    """059's column additions, including the one that is easy to forget."""
    with store._lock:
        run_cols = {
            row["name"] for row in store._connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        session_cols = {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
    assert {"lease_generation", "lease_expires_at", "scope_json"} <= run_cols
    assert "scope_json" in session_cols


def test_scope_json_is_an_updatable_run_column() -> None:
    """The one line in db/store.py: without it every update_run carrying scope_json
    raises 'unknown columns' from _checked_fields."""
    from omniagentos.db.store import _RUN_COLUMNS

    assert "scope_json" in _RUN_COLUMNS


# ---------------------------------------------------------------------------
# The four-case matrix, THROUGH the store
# ---------------------------------------------------------------------------

# (held_path, held_kind, candidate_path, candidate_kind)
# Every kind-pair crossed with equal / candidate-deeper / held-deeper / disjoint,
# plus the whole-realm ('.') row that is an ancestor of everything.
MATRIX: list[tuple[str, str, str, str]] = [
    # candidate=file vs held=file
    ("src/a.py", "file", "src/a.py", "file"),
    ("src/a.py", "file", "src/b.py", "file"),
    # candidate=file vs held=root
    ("src", "root", "src/a.py", "file"),
    ("src", "root", "docs/a.py", "file"),
    ("src", "root", "src", "file"),
    # candidate=root vs held=file
    ("src/a.py", "file", "src", "root"),
    ("src/a.py", "file", "docs", "root"),
    # candidate=root vs held=root
    ("src", "root", "src", "root"),
    ("src", "root", "src/pkg", "root"),
    ("src/pkg", "root", "src", "root"),
    ("src", "root", "docs", "root"),
    # whole realm on either side
    (".", "root", "src/a.py", "file"),
    (".", "root", ".", "root"),
    ("src/a.py", "file", ".", "root"),
]


@pytest.mark.parametrize(("held_path", "held_kind", "cand_path", "cand_kind"), MATRIX)
def test_store_verdict_matches_the_algebra(
    locks: PathLockStore, held_path: str, held_kind: str, cand_path: str, cand_kind: str
) -> None:
    """The store must refuse exactly what ``conflicts_with`` says it should.

    Asserting store-against-algebra rather than against a hand-written expectation
    means the two can never drift: a change to the matrix that this store does not
    honour fails here even though ``test_conflict.py`` still passes.
    """
    held_claim = claim(held_path, held_kind)
    candidate = claim(cand_path, cand_kind)
    expected = conflicts_with(candidate, held_claim)

    first = locks.try_acquire_scope([held_claim], runner(), enforce=True, now=T0)
    assert first.status == "granted"

    second = locks.try_acquire_scope([candidate], worker(), enforce=True, now=T0)

    if expected is None:
        assert second.status == "granted", f"{candidate} should not collide with {held_claim}"
        assert second.conflict is None
    else:
        assert second.status == "blocked", f"{candidate} should collide with {held_claim}"
        assert second.conflict is not None
        assert second.conflict.reason == expected
        assert second.blocked_on == first.lock_ids[0]
        assert second.blocked_path == candidate.path_text
        # Refusal writes nothing: exactly the one held lock remains.
        assert len(locks.held_in_realm(REALM, now=T0)) == 1


def test_different_realms_never_collide(locks: PathLockStore) -> None:
    assert locks.try_acquire_scope([claim(".", "root")], runner(), enforce=True).granted
    other = claim(".", "root", realm=OTHER_REALM)
    assert locks.try_acquire_scope([other], worker(), enforce=True).granted


def test_a_holder_never_conflicts_with_itself(locks: PathLockStore) -> None:
    """Re-acquiring an overlapping path as the SAME identity is a renewal, not a
    collision — otherwise every heartbeat-shaped acquire would refuse itself."""
    holder = runner()
    first = locks.try_acquire_scope([claim("src/a.py")], holder, enforce=True, now=T0)
    again = locks.try_acquire_scope(
        [claim("src", "root"), claim("src/a.py")], holder, enforce=True, now=T0
    )
    assert again.status == "granted"
    # The duplicate claim reuses the existing row rather than inserting a second.
    assert first.lock_ids[0] in again.lock_ids
    assert row_count(locks._store) == 2  # src/** (new) + src/a.py (reused)


def test_repeated_acquire_is_idempotent(locks: PathLockStore) -> None:
    """A crash between COMMIT and the caller recording the result is recoverable."""
    holder = runner()
    spec = [claim("src/a.py"), claim("docs", "root")]
    first = locks.try_acquire_scope(spec, holder, enforce=True, now=T0)
    second = locks.try_acquire_scope(spec, holder, enforce=True, now=T0)
    assert set(first.lock_ids) == set(second.lock_ids)
    assert row_count(locks._store) == 2


# ---------------------------------------------------------------------------
# All-or-nothing — the deadlock-freedom proof
# ---------------------------------------------------------------------------


def test_no_partial_acquire(locks: PathLockStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure mid-transaction leaves ZERO lock rows.

    This is the proof, not a regression guard. try_acquire_scope is
    all-or-nothing inside one BEGIN IMMEDIATE, so a claimant is never a holder
    AND a waiter at the same time; hold-and-wait is therefore structurally absent
    and Coffman's four conditions cannot all hold. If this test ever fails, the
    deadlock-freedom argument in locks.py has stopped being true.
    """
    original = PathLockStore._insert_lock
    seen = {"n": 0}

    def exploding_insert(conn, claim_, holder, **kwargs):  # type: ignore[no-untyped-def]
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("injected failure mid-transaction")
        return original(conn, claim_, holder, **kwargs)

    monkeypatch.setattr(PathLockStore, "_insert_lock", staticmethod(exploding_insert))

    spec = [claim(f"src/f{i}.py") for i in range(5)]
    with pytest.raises(RuntimeError, match="injected failure"):
        locks.try_acquire_scope(spec, runner(), enforce=True, now=T0)

    # Not "no LIVE rows" — no rows at all. A released-but-present row would mean
    # the transaction had partially applied and then been tidied up, which is a
    # different and much weaker property.
    assert row_count(locks._store) == 0
    assert locks.held_in_realm(REALM, now=T0) == []


def test_refusal_is_all_or_nothing_across_the_set(locks: PathLockStore) -> None:
    """One blocked claim refuses the WHOLE set — no partial scope is ever held."""
    locks.try_acquire_scope([claim("src/b.py")], runner("run_hold"), enforce=True, now=T0)
    result = locks.try_acquire_scope(
        [claim("src/a.py"), claim("src/b.py"), claim("src/c.py")],
        worker(),
        enforce=True,
        now=T0,
    )
    assert result.status == "blocked"
    assert locks.held_by("swarm_task", "swt_1", now=T0) == []


def test_rollback_restores_a_reacquire(
    locks: PathLockStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed crash-resume must not strand the run with a surrendered scope."""
    holder = runner()
    first = locks.try_acquire_scope([claim("src/a.py")], holder, enforce=True, now=T0)

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("injected failure mid-transaction")

    monkeypatch.setattr(PathLockStore, "_insert_lock", staticmethod(boom))
    with pytest.raises(RuntimeError):
        locks.reacquire_scope([claim("docs", "root")], holder, generation=1, enforce=True, now=T0)

    live = locks.held_in_realm(REALM, now=T0)
    assert [lock.id for lock in live] == list(first.lock_ids)


# ---------------------------------------------------------------------------
# TTL reaping
# ---------------------------------------------------------------------------


def test_reap_is_bounded_at_the_limit(locks: PathLockStore) -> None:
    """The LIMIT subselect is what stops a restart storm rewriting the whole table.

    launchd restarting the fleet expires every lease in the same second; an
    unbounded UPDATE would make the first acquire afterwards rewrite every row
    while holding the write lock.
    """
    spec = [claim(f"src/f{i}.py") for i in range(REAP_LIMIT + 100)]
    granted = locks.try_acquire_scope(spec, runner(), enforce=True, ttl_s=1, now=T0)
    assert len(granted.lock_ids) == REAP_LIMIT + 100

    assert locks.reap_expired(now=T_LATER) == REAP_LIMIT
    assert locks.reap_expired(now=T_LATER) == 100
    assert locks.reap_expired(now=T_LATER) == 0


def test_expired_locks_never_block_even_before_the_reaper_reaches_them(
    locks: PathLockStore,
) -> None:
    """The bound is a GC bound, not a correctness bound.

    Every live read filters on expires_at, so a lock the bounded sweep has not
    reached yet is already invisible to the conflict scan. Without that filter the
    LIMIT would silently become a correctness bug at scale.
    """
    spec = [claim(f"src/f{i}.py") for i in range(REAP_LIMIT + 10)]
    locks.try_acquire_scope(spec, runner(), enforce=True, ttl_s=1, now=T0)
    assert locks.held_in_realm(REALM, now=T_LATER) == []

    taken = locks.try_acquire_scope([claim(".", "root")], worker(), enforce=True, now=T_LATER)
    assert taken.status == "granted"


def test_acquire_reaps_lazily(locks: PathLockStore) -> None:
    """The reserve_account idiom: reap at the head of the acquire, same transaction."""
    locks.try_acquire_scope([claim("src/a.py")], runner(), enforce=True, ttl_s=1, now=T0)
    locks.try_acquire_scope([claim("docs/x.md")], worker(), enforce=True, now=T_LATER)
    with locks._store._lock:
        released = locks._store._connection.execute(
            "SELECT COUNT(*) FROM resource_locks WHERE released_at IS NOT NULL"
        ).fetchone()[0]
    assert released == 1


# ---------------------------------------------------------------------------
# Generation fencing
# ---------------------------------------------------------------------------


def test_release_is_generation_fenced(locks: PathLockStore) -> None:
    """A stale worker must NOT free its adopter's locks.

    The wedged-not-dead case: run_1 is adopted (generation bumped to 1), then the
    original worker wakes up and runs its cleanup path with generation 0. If that
    released anything, the adopter would be working unprotected.
    """
    holder = runner()
    locks.try_acquire_scope([claim("src/a.py")], holder, generation=0, enforce=True, now=T0)
    adopted = locks.reacquire_scope([claim("src/a.py")], holder, generation=1, enforce=True, now=T0)
    assert adopted.status == "granted"

    assert locks.release_scope("run", "run_1", 0) == 0
    assert [lock.id for lock in locks.held_in_realm(REALM, now=T0)] == list(adopted.lock_ids)

    assert locks.release_scope("run", "run_1", 1) == 1
    assert locks.held_in_realm(REALM, now=T0) == []


def test_stale_generation_cannot_acquire(locks: PathLockStore) -> None:
    """Fencing is not only about release: a displaced worker must not re-enter."""
    holder = runner()
    locks.try_acquire_scope([claim("src/a.py")], holder, generation=2, enforce=True, now=T0)
    stale = locks.try_acquire_scope(
        [claim("docs/x.md")], holder, generation=1, enforce=True, now=T0
    )
    assert stale.status == "fenced"
    assert not stale.granted
    assert stale.lock_ids == ()


def test_renew_is_generation_fenced(locks: PathLockStore) -> None:
    holder = runner()
    locks.try_acquire_scope([claim("src/a.py")], holder, generation=1, enforce=True, now=T0)
    assert locks.renew_scope("run", "run_1", 0, 90, now=T0) is False
    assert locks.renew_scope("run", "run_1", 1, 90, now=T0) is True


def test_renew_never_resurrects_a_lapsed_lock(locks: PathLockStore) -> None:
    """The one way a lock table can hand the same path to two writers.

    Once a lease lapses another holder may legitimately take the path; extending
    the lapsed row would then leave two live claims on it. A lapsed holder must go
    back through try_acquire_scope and be told.
    """
    holder = runner()
    locks.try_acquire_scope([claim("src/a.py")], holder, enforce=True, ttl_s=1, now=T0)
    assert locks.renew_scope("run", "run_1", 0, 90, now=T_LATER) is False


def test_reacquire_replaces_rather_than_unions(locks: PathLockStore) -> None:
    """A resume that narrows its scope must give the surrendered paths back."""
    holder = runner()
    locks.try_acquire_scope(
        [claim("src/a.py"), claim("docs", "root")], holder, enforce=True, now=T0
    )
    locks.reacquire_scope([claim("src/a.py")], holder, generation=1, enforce=True, now=T0)

    live = locks.held_in_realm(REALM, now=T0)
    assert [lock.rel_path for lock in live] == ["src/a.py"]
    # And the surrendered subtree is genuinely available to somebody else.
    assert locks.try_acquire_scope([claim("docs", "root")], worker(), enforce=True, now=T0).granted


# ---------------------------------------------------------------------------
# The SQL backstop
# ---------------------------------------------------------------------------


def test_partial_unique_index_rejects_an_exact_duplicate(store: SqliteStore) -> None:
    """The index is a backstop against a caller that bypasses the store.

    Enforcement can never reach this — an exact duplicate always collides in the
    matrix first — so the only honest way to exercise it is raw SQL.
    """
    insert = (
        "INSERT INTO resource_locks (id, realm, rel_path, depth, kind, holder_kind, "
        "holder_id, holder_generation, lane, owner_group, purpose, acquired_at, expires_at) "
        "VALUES (?, ?, 'src/a.py', 2, 'file', 'run', ?, 0, 'runner', '', 'work', ?, ?)"
    )
    with store._lock:
        store._connection.execute(insert, ("lck_a", REALM, "run_1", T0, T_LATER))
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(insert, ("lck_b", REALM, "run_2", T0, T_LATER))

        # Releasing the first drops it out of the partial index; the second fits.
        store._connection.execute(
            "UPDATE resource_locks SET released_at = ? WHERE id = 'lck_a'", (T0,)
        )
        store._connection.execute(insert, ("lck_b", REALM, "run_2", T0, T_LATER))


def test_index_does_not_pretend_to_cover_containment(store: SqliteStore) -> None:
    """Ancestor/descendant exclusion is NOT expressible as an index.

    'src/**' and 'src/a.py' are different index keys, so SQLite accepts both. The
    exclusion is enforced by the SELECT inside BEGIN IMMEDIATE — this test exists
    so nobody later reads the unique index as a stronger guarantee than it is.
    """
    insert = (
        "INSERT INTO resource_locks (id, realm, rel_path, depth, kind, holder_kind, "
        "holder_id, holder_generation, lane, owner_group, purpose, acquired_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'run', ?, 0, 'runner', '', 'work', ?, ?)"
    )
    with store._lock:
        store._connection.execute(insert, ("lck_a", REALM, "src", 1, "root", "run_1", T0, T_LATER))
        store._connection.execute(
            insert, ("lck_b", REALM, "src/a.py", 2, "file", "run_2", T0, T_LATER)
        )
    # The store, unlike the index, refuses it.
    locks = PathLockStore(store)
    live = locks.held_in_realm(REALM, now=T0)
    assert len(live) == 2
    holder = LockHolder(kind="session", id="ses_9", lane="session")
    assert locks.try_acquire_scope([claim("src/a.py")], holder, enforce=True, now=T0).status == (
        "blocked"
    )


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


def test_shadow_mode_inserts_rows_without_refusing(locks: PathLockStore) -> None:
    """A shadow that writes no rows measures a hypothetical, not real contention.

    The rows are the point: the locks a shadow run declines to write are exactly
    the locks the NEXT claimant would have collided with, so a row-less shadow
    under-reports contention by construction.
    """
    locks.try_acquire_scope([claim("src/a.py")], runner(), enforce=True, now=T0)

    shadowed = locks.try_acquire_scope([claim("src", "root")], worker(), enforce=False, now=T0)

    assert shadowed.status == "granted"
    assert shadowed.granted
    assert shadowed.shadowed
    assert shadowed.conflict is not None
    assert shadowed.conflict.reason == "held_under_candidate"
    assert len(shadowed.lock_ids) == 1
    # The row really exists, so the next claimant sees real contention.
    assert {lock.rel_path for lock in locks.held_in_realm(REALM, now=T0)} == {"src/a.py", "src"}


def test_shadow_mode_skips_only_the_exact_duplicate(locks: PathLockStore) -> None:
    """The one row shadow mode cannot write, and why that is not a hole.

    An exact (realm, path, kind, purpose) duplicate would violate the partial
    unique index, and weakening that index to allow it would destroy the invariant
    it exists to express. The contention is still measured — it is in `conflict` —
    and every non-duplicate claim in the same set is still written.
    """
    locks.try_acquire_scope([claim("src/a.py")], runner(), enforce=True, now=T0)

    shadowed = locks.try_acquire_scope(
        [claim("src/a.py"), claim("src/b.py")], worker(), enforce=False, now=T0
    )
    assert shadowed.status == "granted"
    assert shadowed.skipped_duplicates == ("src/a.py",)
    assert len(shadowed.lock_ids) == 1
    assert {lock.rel_path for lock in locks.held_in_realm(REALM, now=T0)} == {
        "src/a.py",
        "src/b.py",
    }


def test_off_mode_writes_nothing(locks: PathLockStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring the store in before anyone has turned it on must be inert."""
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "off")
    result = locks.try_acquire_scope([claim("src/a.py")], runner(), now=T0)
    assert result.status == "disabled"
    assert result.granted  # 'off' means "do not interfere", not "refuse"
    assert row_count(locks._store) == 0


def test_mode_defaults_come_from_config(
    locks: PathLockStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "enforce")
    locks.try_acquire_scope([claim("src/a.py")], runner(), now=T0)
    blocked = locks.try_acquire_scope([claim("src", "root")], worker(), now=T0)
    assert blocked.status == "blocked"


# ---------------------------------------------------------------------------
# Commit lock
# ---------------------------------------------------------------------------


def test_commit_lock_excludes_everyone_else(locks: PathLockStore) -> None:
    holder = runner()
    with locks.commit_lock(REALM, holder, enforce=True, now=T0) as held:
        assert held.granted
        blocked = locks.try_acquire_scope([claim("docs/x.md")], worker(), enforce=True, now=T0)
        assert blocked.status == "blocked"
    # Released on exit.
    assert locks.try_acquire_scope([claim("docs/x.md")], worker(), enforce=True, now=T0).granted


def test_commit_lock_releases_only_itself(locks: PathLockStore) -> None:
    """Dropping a commit lock must not drop the work locks the same holder took."""
    holder = runner()
    work = locks.try_acquire_scope([claim("src/a.py")], holder, enforce=True, now=T0)
    with locks.commit_lock(REALM, holder, enforce=True, now=T0):
        pass
    live = locks.held_in_realm(REALM, now=T0)
    assert [lock.id for lock in live] == list(work.lock_ids)
    assert live[0].purpose == "work"


def test_commit_lock_raises_when_busy(locks: PathLockStore) -> None:
    """A `with` block that did not get the lock must not run its body."""
    locks.try_acquire_scope([claim("src/a.py")], worker(), enforce=True, now=T0)
    with pytest.raises(ScopeLockBusy) as excinfo:
        with locks.commit_lock(REALM, runner(), enforce=True, now=T0):
            pytest.fail("commit lock body ran despite the refusal")
    assert excinfo.value.result.conflict is not None


def test_commit_lock_is_fenced(locks: PathLockStore) -> None:
    holder = runner()
    locks.try_acquire_scope([claim("src/a.py")], holder, generation=3, enforce=True, now=T0)
    with pytest.raises(ScopeLockFenced):
        with locks.commit_lock(REALM, holder, generation=1, enforce=True, now=T0):
            pytest.fail("a displaced worker committed")


def test_commit_purpose_is_stored_as_commit(locks: PathLockStore) -> None:
    holder = runner()
    with locks.commit_lock(REALM, holder, enforce=True, now=T0):
        live = locks.held_in_realm(REALM, now=T0)
        assert [lock.purpose for lock in live] == [COMMIT_PURPOSE]
        assert live[0].rel_path == "."


# ---------------------------------------------------------------------------
# Waiters
# ---------------------------------------------------------------------------


def test_waiters_are_fifo(locks: PathLockStore) -> None:
    first = locks.park_waiter(REALM, worker("swt_a"), blocked_on="lck_x", blocked_path="src")
    second = locks.park_waiter(REALM, worker("swt_b"))
    third = locks.park_waiter(REALM, worker("swt_c"))
    assert first < second < third

    head = locks.next_waiter(REALM)
    assert head is not None
    assert head.holder_id == "swt_a"
    assert head.blocked_on == "lck_x"
    assert head.blocked_path == "src"

    assert locks.clear_waiter("swarm_task", "swt_a") == 1
    nxt = locks.next_waiter(REALM)
    assert nxt is not None and nxt.holder_id == "swt_b"

    assert [w.holder_id for w in locks.waiting_in_realm(REALM)] == ["swt_b", "swt_c"]


def test_reparking_keeps_the_queue_position(locks: PathLockStore) -> None:
    """Otherwise a holder that retries every few seconds starves itself forever.

    Each retry would append a fresh row at the BACK of the queue, so the most
    persistent waiter would be the last one served. The partial unique index makes
    that mistake impossible; this asserts the store takes the update branch.
    """
    first = locks.park_waiter(REALM, worker("swt_a"), blocked_on="lck_1")
    locks.park_waiter(REALM, worker("swt_b"))
    again = locks.park_waiter(REALM, worker("swt_a"), blocked_on="lck_2", blocked_path="src/a.py")

    assert again == first
    queue = locks.waiting_in_realm(REALM)
    assert [w.holder_id for w in queue] == ["swt_a", "swt_b"]
    assert queue[0].blocked_on == "lck_2"  # diagnostics refreshed
    assert queue[0].blocked_path == "src/a.py"


def test_waiters_are_realm_scoped(locks: PathLockStore) -> None:
    locks.park_waiter(OTHER_REALM, worker("swt_other"))
    assert locks.next_waiter(REALM) is None
    head = locks.next_waiter(OTHER_REALM)
    assert head is not None and head.holder_id == "swt_other"


def test_clearing_a_waiter_lets_it_park_again(locks: PathLockStore) -> None:
    first = locks.park_waiter(REALM, worker("swt_a"))
    assert locks.clear_waiter("swarm_task", "swt_a") == 1
    assert locks.clear_waiter("swarm_task", "swt_a") == 0
    second = locks.park_waiter(REALM, worker("swt_a"))
    assert second > first


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_holder_kind_and_lane_are_rejected_in_python() -> None:
    """Named failures, not an opaque IntegrityError from a CHECK constraint."""
    with pytest.raises(ScopeLockError, match="holder kind"):
        LockHolder(kind="daemon", id="x", lane="runner")  # type: ignore[arg-type]
    with pytest.raises(ScopeLockError, match="lane"):
        LockHolder(kind="run", id="x", lane="fastlane")  # type: ignore[arg-type]
    with pytest.raises(ScopeLockError, match="id"):
        LockHolder(kind="run", id="  ", lane="runner")


def test_non_positive_ttl_is_rejected(locks: PathLockStore) -> None:
    """A zero TTL expires every lock instantly — that reads as broken, not off."""
    with pytest.raises(ScopeLockError, match="ttl"):
        locks.try_acquire_scope([claim("src/a.py")], runner(), enforce=True, ttl_s=0)


def test_held_lock_round_trips_through_a_claim(locks: PathLockStore) -> None:
    """rel_path '.' must survive the round trip as the empty component tuple."""
    locks.try_acquire_scope([claim(".", "root")], runner(), enforce=True, now=T0)
    live: list[HeldLock] = locks.held_in_realm(REALM, now=T0)
    restored = live[0].as_claim()
    assert restored.components == ()
    assert restored.is_whole_realm
    assert restored.lock_id == live[0].id  # carries the id, so ordering is stable


# ---------------------------------------------------------------------------
# The multi-process race
# ---------------------------------------------------------------------------

_RACE_WORKER = '''
import pathlib
import random
import sqlite3
import sys
import time

from omniagentos.db.store import SqliteStore
from omniagentos.scope.conflict import conflicts_with
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim

REALM = "/race/realm"

# Deliberately overlapping: whole-realm roots, subtrees, files inside those
# subtrees, and disjoint paths. Every pair of entries here that CAN collide does.
POOL = [
    [(".", "root")],
    [("src", "root")],
    [("src/a.py", "file")],
    [("src/a.py", "file"), ("src/b.py", "file")],
    [("src/pkg", "root"), ("docs", "root")],
    [("src/pkg/deep/mod.py", "file")],
    [("docs/readme.md", "file")],
]


def build(spec):
    return [ScopeClaim.for_path(REALM, path, kind=kind) for path, kind in spec]


def invariant(locks):
    """No two live locks from DIFFERENT holders may conflict. Ever."""
    live = locks.held_in_realm(REALM)
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if a.holder == b.holder:
                continue
            reason = conflicts_with(a.as_claim(), b.as_claim())
            if reason is not None:
                return "%s [%s] <-> %s [%s] (%s)" % (
                    a.as_claim(), a.holder_id, b.as_claim(), b.holder_id, reason
                )
    return None


def retry(call):
    for attempt in range(60):
        try:
            return call()
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" not in text and "busy" not in text:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise RuntimeError("gave up waiting for the database")


def rendezvous(barrier_dir, index, n_procs):
    """Start every worker in the same millisecond.

    Without this the processes finish staggered and mostly miss each other, so a
    green run would prove only that nobody was around to conflict with.
    """
    (barrier_dir / ("ready_%d" % index)).write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if len(list(barrier_dir.glob("ready_*"))) >= n_procs:
            return
        time.sleep(0.005)
    raise RuntimeError("workers never assembled at the barrier")


def main():
    db_path, index, n_ops = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    n_procs, hold_s = int(sys.argv[4]), float(sys.argv[5])
    barrier_dir = pathlib.Path(sys.argv[6])
    store = SqliteStore(db_path)
    locks = PathLockStore(store)
    holder = LockHolder(kind="swarm_task", id="wk%d" % index, lane="swarm")
    rng = random.Random(9000 + index)
    granted = blocked = 0
    try:
        rendezvous(barrier_dir, index, n_procs)
        for _ in range(n_ops):
            spec = POOL[rng.randrange(len(POOL))]
            claims = build(spec)
            result = retry(
                lambda: locks.try_acquire_scope(claims, holder, enforce=True, ttl_s=300)
            )
            bad = retry(lambda: invariant(locks))
            if bad is not None:
                print("VIOLATION after acquire: " + bad)
                return 1
            if result.granted:
                granted += 1
                # HOLD the scope for a beat. A worker that releases in the same
                # microsecond it acquired generates no contention, and a race test
                # with no race is a green test that proves nothing.
                time.sleep(hold_s)
                bad = retry(lambda: invariant(locks))
                if bad is not None:
                    print("VIOLATION while holding: " + bad)
                    return 1
                retry(lambda: locks.release_scope(holder.kind, holder.id, 0))
            else:
                blocked += 1
                retry(
                    lambda: locks.park_waiter(
                        REALM,
                        holder,
                        blocked_on=result.blocked_on,
                        blocked_path=result.blocked_path,
                    )
                )
                retry(lambda: locks.clear_waiter(holder.kind, holder.id))
            bad = retry(lambda: invariant(locks))
            if bad is not None:
                print("VIOLATION after release: " + bad)
                return 1
        print("OK %d %d" % (granted, blocked))
        return 0
    finally:
        store.close()


sys.exit(main())
'''


def test_multiprocess_no_conflicting_locks(tmp_path: Path) -> None:
    """N real PROCESSES hammering overlapping scopes; the invariant must always hold.

    Threads would share ``SqliteStore._lock`` and therefore serialize in-process,
    masking exactly the bug this phase exists to remove — a check-then-act split
    across two connections. Only separate OS processes actually exercise
    ``BEGIN IMMEDIATE``.

    After EVERY operation each worker re-reads the whole live set and asserts that
    no two locks from different holders conflict under ``conflicts_with``. That is
    the end-to-end statement of the property: mutual exclusion over a containment
    lattice, enforced across process boundaries.

    Contention is MANUFACTURED, not hoped for: the workers rendezvous at a file
    barrier so they start together, and a granted scope is held for ``HOLD_S``
    before release. Without both, the processes finish staggered and never
    overlap — and a race test with no race is a green test that proves nothing,
    which is why ``total_blocked > 0`` is asserted at the end.
    """
    # Migrate ONCE, here: N processes racing to apply the same migration would
    # have one win BEGIN IMMEDIATE and the rest fail on "table already exists".
    db_path = Path(migrated_db(SqliteStore, tmp_path / "race.db"))

    script = tmp_path / "race_worker.py"
    script.write_text(_RACE_WORKER, encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    # The workers must not inherit an operator's live mode; they pass enforce
    # explicitly, but the DB path override matters for anything they touch.
    env["OMNIAGENTOS_DB"] = str(db_path)

    n_procs, n_ops, hold_s = 4, 40, 0.01
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(db_path),
                str(index),
                str(n_ops),
                str(n_procs),
                str(hold_s),
                str(barrier),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(repo_root),
        )
        for index in range(n_procs)
    ]
    outputs = [proc.communicate(timeout=120) for proc in procs]

    total_granted = 0
    total_blocked = 0
    for proc, (out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, f"worker failed:\nstdout={out}\nstderr={err}"
        assert out.startswith("OK "), f"worker reported a violation: {out}\n{err}"
        _, granted, blocked = out.split()
        total_granted += int(granted)
        total_blocked += int(blocked)

    assert total_granted + total_blocked == n_procs * n_ops
    # Both outcomes must actually have happened, or the test proved nothing: all
    # granted means no contention was generated, all blocked means no progress.
    assert total_granted > 0
    assert total_blocked > 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config loader at a path with no file (fresh-checkout behaviour)."""
    target = tmp_path / "parallelism.yaml"
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(target))
    for name in (
        scope_config.SCOPE_LOCKS_ENV,
        scope_config.SCOPE_TTL_ENV,
        scope_config.SCOPE_STARVATION_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return target


def test_defaults_are_off_and_documented(empty_config: Path) -> None:
    assert scope_config.parallelism_config() == {}
    assert scope_config.scope_locks_mode() == "off"
    assert scope_config.scope_locks_enabled() is False
    assert scope_config.scope_locks_enforcing() is False
    assert scope_config.scope_ttl_s() == 90.0
    assert scope_config.scope_starvation_s() == 300.0


def test_config_file_is_the_default(empty_config: Path) -> None:
    empty_config.write_text(
        "scope_locks:\n  mode: shadow\n  ttl_seconds: 45\n  starvation_seconds: 120\n",
        encoding="utf-8",
    )
    assert scope_config.scope_locks_mode() == "shadow"
    assert scope_config.scope_locks_enabled() is True
    assert scope_config.scope_locks_enforcing() is False
    assert scope_config.scope_ttl_s() == 45.0
    assert scope_config.scope_starvation_s() == 120.0


def test_env_overrides_in_both_directions(
    empty_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that is easy to get wrong: a falsy env must FORCE-DISABLE.

    A one-directional override is not a kill switch, and a mechanism that can
    refuse a worker's writes needs one that works from a shell.
    """
    empty_config.write_text("scope_locks:\n  mode: enforce\n", encoding="utf-8")
    assert scope_config.scope_locks_mode() == "enforce"

    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "off")
    assert scope_config.scope_locks_mode() == "off"

    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "0")
    assert scope_config.scope_locks_mode() == "off"

    empty_config.write_text("scope_locks:\n  mode: 'off'\n", encoding="utf-8")
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "shadow")
    assert scope_config.scope_locks_mode() == "shadow"

    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "1")
    assert scope_config.scope_locks_mode() == "enforce"


def test_yaml_bare_off_is_the_off_mode_not_a_boolean(empty_config: Path) -> None:
    """YAML parses an unquoted `off` as False. That must still mean the off MODE."""
    empty_config.write_text("scope_locks:\n  mode: off\n", encoding="utf-8")
    assert scope_config.parallelism_config()["scope_locks"]["mode"] is False
    assert scope_config.scope_locks_mode() == "off"


def test_unparseable_env_never_silently_enables(
    empty_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not start refusing writes."""
    empty_config.write_text("scope_locks:\n  mode: shadow\n", encoding="utf-8")
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "enfroce")
    assert scope_config.scope_locks_mode() == "shadow"


def test_broken_config_degrades_to_defaults(empty_config: Path) -> None:
    empty_config.write_text("scope_locks: [this is not a mapping\n", encoding="utf-8")
    assert scope_config.parallelism_config() == {}
    assert scope_config.scope_locks_mode() == "off"


def test_bad_durations_fall_through(empty_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_config.write_text("scope_locks:\n  ttl_seconds: nope\n", encoding="utf-8")
    assert scope_config.scope_ttl_s() == 90.0
    monkeypatch.setenv(scope_config.SCOPE_TTL_ENV, "0")
    assert scope_config.scope_ttl_s() == 90.0  # a zero TTL is broken, not "off"
    monkeypatch.setenv(scope_config.SCOPE_TTL_ENV, "12.5")
    assert scope_config.scope_ttl_s() == 12.5


def test_shipped_config_is_shadow_by_default() -> None:
    """Grok ships scope locks in shadow (measure contention, never refuse).

    Product divergence from OmniAgentOS (which may ship ``off``): Grok's
    STATUS/parallelism.yaml pin shadow-by-default so soak data is real while
    enforcement stays off until explicitly flipped.
    """
    shipped = scope_config.parallelism_config(
        Path(__file__).resolve().parents[2] / "configs" / "parallelism.yaml"
    )
    assert scope_config._coerce_mode(shipped["scope_locks"]["mode"]) == "shadow"
    assert shipped["scope_locks"]["ttl_seconds"] == 90
    assert shipped["scope_locks"]["starvation_seconds"] == 300
