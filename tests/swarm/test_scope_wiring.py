"""The swarm lane's scope-lock wiring: dark by default, fenced under adoption.

Four properties, in the order they matter:

1. WITH THE MODE OFF, NOTHING CHANGES. Not "nothing important" — nothing. No
   rows, and no code path that so much as ASKS the dal for a lock store (the
   store property is booby-trapped in ``test_off_mode_never_reaches_the_lock_store``
   and a full run still completes). This is the acceptance criterion the whole
   package ships against.
2. In enforce mode a conflicting declaration is REFUSED and the lane parks: no
   attempt row, no spawn, a durable waiter naming the blocker — and the run
   resumes on its own once the blocker lets go.
3. Every terminal path releases, including the crash path, because the release
   lives in the one ``finally`` all of them funnel through.
4. Adoption re-keys the run's locks inside the generation bump, so the displaced
   coordinator can neither free them nor re-enter the fleet.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import realm_of
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.scheduler import SwarmScheduler
from omniagentos.swarm.scope_wiring import (
    ScopeCommitBusy,
    commit_guard,
    coordinator_holder,
    owned_path_claims,
    task_holder,
)
from tests.support.db_template import migrated_db
from tests.swarm.scheduler_fakes import make_harness, make_scheduler, wait_until


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch in these tests goes through the harness fixtures."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


@pytest.fixture
def default_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The SHIPPED default: no env var, and no config file to read either.

    Pointing the config at a path that does not exist is deliberate — it proves
    the hard-coded default is off, rather than proving that the repo's checked-in
    ``configs/parallelism.yaml`` currently says so.
    """
    monkeypatch.delenv("OMNIAGENTOS_SCOPE_LOCKS", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(tmp_path / "absent.yaml"))


@pytest.fixture
def enforcing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "enforce")
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(tmp_path / "absent.yaml"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rows(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _locks(db_path: str) -> list[dict[str, Any]]:
    return _rows(db_path, "SELECT * FROM resource_locks ORDER BY id")


def _live_locks(db_path: str) -> list[dict[str, Any]]:
    return _rows(db_path, "SELECT * FROM resource_locks WHERE released_at IS NULL ORDER BY id")


def _waiters(db_path: str) -> list[dict[str, Any]]:
    return _rows(db_path, "SELECT * FROM scope_waiters ORDER BY id")


class _StepClock:
    """Virtual monotonic time: ``sleep`` advances it, nothing ever blocks."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)


def _foreign_holder() -> LockHolder:
    """A holder from ANOTHER lane — the cross-lane collision this exists for."""
    return LockHolder(kind="run", id="run_from_the_runner_lane", lane="runner")


# ---------------------------------------------------------------------------
# 1. Off by default, and inert
# ---------------------------------------------------------------------------


class TestShipsDark:
    def test_full_run_writes_no_scope_rows_when_off(
        self, tmp_path: Path, default_off: None
    ) -> None:
        """A complete two-task run leaves both scope tables EMPTY."""
        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}])
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.dal.get_run(h.run_id)["status"] == "completed"
            for key in ("a", "b", "integration"):
                assert h.status_of(key) == "done"

            assert _locks(h.db_path) == []
            assert _waiters(h.db_path) == []
        finally:
            h.close()

    def test_off_mode_never_reaches_the_lock_store(
        self, tmp_path: Path, default_off: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The strongest form of "byte-for-byte": with the mode off, no code
        path even ASKS for the lock store.

        A row-count assertion would pass even if the wiring opened a
        transaction, resolved a realm and shelled out to ``git rev-parse`` on
        every attempt before deciding to do nothing. This one cannot.
        """

        def boom(_self: SwarmDal) -> PathLockStore:
            raise AssertionError("the lock store must not be touched while scope locks are off")

        monkeypatch.setattr(SwarmDal, "path_locks", property(boom))

        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}])
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_commit_guard_off_is_exactly_the_in_process_lock(self, default_off: None) -> None:
        """With the mode off the guard takes the RLock and does nothing else —
        so ``with self._git_guard(...)`` IS ``with state.git_lock:``."""

        class Bomb:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"the lock store must not be used: .{name}")

        lock = threading.RLock()
        with commit_guard(
            Bomb(),  # type: ignore[arg-type]
            realm="/some/realm",
            holder=coordinator_holder("swr_x"),
            generation=0,
            in_process_lock=lock,
            clock=_StepClock(),
        ) as result:
            assert result.result is None
            assert result.status == "disabled"
            assert result.granted is True
            # Held for the duration, exactly like the RLock it replaces.
            assert lock.acquire(blocking=False)
            lock.release()


# ---------------------------------------------------------------------------
# 2. The declaration: owned_paths -> root claims
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_every_owned_path_becomes_a_root_claim(self) -> None:
        claims = owned_path_claims("/realm", ["src/a", "docs"], owner_group="swr_1")
        assert [c.kind for c in claims] == ["root", "root"]
        assert [c.components for c in claims] == [("src", "a"), ("docs",)]
        assert {c.owner_group for c in claims} == {"swr_1"}

    def test_dot_is_the_whole_realm(self) -> None:
        """The integration task owns '.', which ``_path_owned`` treats as
        "owns everything" — the algebra spells that as the empty tuple."""
        (claim,) = owned_path_claims("/realm", ["."])
        assert claim.is_whole_realm and claim.is_root

    def test_an_unusable_path_widens_instead_of_disappearing(self) -> None:
        """Fail CLOSED: a path that will not normalize must never silently
        narrow the declared scope."""
        (claim,) = owned_path_claims("/realm", ["/etc/passwd"])
        assert claim.is_whole_realm and claim.is_root

    @pytest.mark.parametrize(
        ("owned", "candidate"),
        [
            ("src/a", "src/a"),
            ("src/a", "src/a/deep/file.py"),
            ("src", "src/a.txt"),
            (".", "anything/at/all"),
        ],
    )
    def test_root_claims_match_path_owned_where_it_is_correct(
        self, owned: str, candidate: str
    ) -> None:
        """The claim is never NARROWER than the ownership rule the scheduler
        already enforces on the post-attempt diff.

        Only "where it is correct": ``_path_owned`` strips with
        ``lstrip('./')``, a CHARACTER set, so it also eats the leading dot of a
        dotfile (``.github`` -> ``github``) and would call ``github/x`` owned.
        The component-wise algebra in ``scope.paths`` is the documented fix for
        exactly that, so the two deliberately disagree there and the claim is
        the correct one.
        """
        assert SwarmScheduler._path_owned(candidate, [owned]) is True
        (claim,) = owned_path_claims("/realm", [owned])
        probe = ScopeClaim.for_path("/realm", candidate, kind="file")
        from omniagentos.scope.conflict import conflicts_with

        assert conflicts_with(probe, claim) is not None


# ---------------------------------------------------------------------------
# 3. Enforce: refuse, park, resume
# ---------------------------------------------------------------------------


class TestEnforcedClaims:
    def test_conflicting_claim_parks_the_task_instead_of_executing(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        """Another lane holds ``src/a.txt``. Task 'a' must not run at all —
        no attempt row, no spawn — while its sibling proceeds; and it must be
        parked on the blocker. Releasing the blocker lets the run finish with
        no further intervention."""
        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}], integration=False)
        blocker = SwarmDal(h.db_path)
        try:
            realm = realm_of(str(h.workdir))
            assert realm is not None
            granted = blocker.path_locks.try_acquire_scope(
                [ScopeClaim.for_path(realm, "src/a.txt", kind="file")],
                _foreign_holder(),
                generation=0,
            )
            assert granted.status == "granted"

            scheduler = make_scheduler(h, scope_retry_seconds=0.05)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None

            # 'b' owns src/b.txt — untouched by the blocker — and completes.
            assert wait_until(lambda: h.status_of("b") == "done", timeout=20)
            # 'a' never executed: refused BEFORE the attempt was opened.
            assert h.attempts_of("a") == []
            assert "a" not in h.world.spawn_order
            assert h.status_of("a") != "done"

            # …and it is parked on the lock that refused it, not on a guess.
            assert wait_until(lambda: len(_waiters(h.db_path)) == 1, timeout=20)
            waiter = _waiters(h.db_path)[0]
            assert waiter["holder_kind"] == "swarm_task"
            assert waiter["holder_id"] == h.task_id("a")
            assert waiter["lane"] == "swarm"
            assert waiter["blocked_on"] == granted.lock_ids[0]
            assert waiter["blocked_path"] == "src/a.txt"

            # Release the blocker: the lane picks the task up by itself.
            assert blocker.path_locks.release_scope("run", "run_from_the_runner_lane", 0) == 1
            assert handle.join(timeout=30)
            assert h.status_of("a") == "done"
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # The park was cleared once the scope was granted.
            assert [w for w in _waiters(h.db_path) if w["cleared_at"] is None] == []
        finally:
            blocker.close()
            h.close()

    def test_granted_run_takes_and_then_releases_every_task_lock(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        """Release on the ordinary terminal paths: locks were really taken
        (rows exist) and none of them is still live at the end.

        Doubles as the no-deadlock drill for the Phase-1 shared-directory model,
        where a task's own work locks live in the SAME realm as the coordinator's
        commit claim: the run has to complete, and its guarded git ops have to
        actually run, rather than the two mechanisms wedging each other.
        """
        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}])
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)
            assert h.dal.get_run(h.run_id)["status"] == "completed"

            taken = {
                row["holder_id"] for row in _locks(h.db_path) if row["holder_kind"] == "swarm_task"
            }
            assert taken == {h.task_id(key) for key in ("a", "b", "integration")}
            assert _live_locks(h.db_path) == []
            # The guarded main-workspace git ops ran (they were not skipped or
            # blocked): a pre-attempt snapshot per attempt, a pathspec commit
            # per CONFIRM.
            assert len(h.git.snapshots) >= 3
            assert len(h.git.commits) >= 3
        finally:
            h.close()

    @pytest.mark.parametrize(
        ("disposition", "still_held"),
        [
            ("done", False),
            ("blocked", False),
            ("cancelled", False),
            ("requeue", False),
            ("CRASH", False),
            # The two that must KEEP their locks: an approval-parked attempt is
            # still alive and may still write, and a displaced coordinator's
            # locks belong to its adopter (its release is fenced anyway, but it
            # must not even try).
            ("parked", True),
            ("displaced", True),
        ],
    )
    def test_every_terminal_disposition_releases_except_parked_and_displaced(
        self,
        tmp_path: Path,
        enforcing: None,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        still_held: bool,
    ) -> None:
        """``_execute_item``'s ``finally`` is the ONE release point — including
        the ``except`` branch that catches a worker crash. Driven directly, and
        synchronously, so "including crash" is a fact rather than a race."""
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(tmp_path, [{"id": "a"}], integration=False)
        try:
            scheduler = make_scheduler(h)
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            scheduler._resolve_scope_realm(state)
            assert state.scope_realm is not None
            task_id = h.task_id("a")

            assert scheduler._claim_task_scope(state, task_id, str(h.workdir), ["src/a.txt"])
            assert len(h.dal.path_locks.held_by("swarm_task", task_id)) == 1

            def outcome(*_args: Any, **_kwargs: Any) -> str:
                if disposition == "CRASH":
                    raise RuntimeError("worker exploded after the scope claim")
                return disposition

            monkeypatch.setattr(scheduler, "_execute_task", outcome)
            scheduler._execute_item(state, 0, ("task", {"id": task_id}))

            held = h.dal.path_locks.held_by("swarm_task", task_id)
            assert bool(held) is still_held
        finally:
            h.close()


# ---------------------------------------------------------------------------
# 4. The cross-process commit lock
# ---------------------------------------------------------------------------


class TestCommitGuard:
    def _dal_pair(self, tmp_path: Path) -> tuple[SwarmDal, SwarmDal]:
        db = migrated_db(CollabStore, tmp_path / "commit.db")
        return SwarmDal(db), SwarmDal(db)

    def test_two_coordinators_cannot_hold_the_same_realm(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        """The whole point: two SEPARATE connections (two processes) cannot both
        be inside a main-workspace mutation. ``_RunState.git_lock`` is a
        threading.RLock and would not have stopped this."""
        first, second = self._dal_pair(tmp_path)
        clock = _StepClock()
        try:
            with commit_guard(
                first.path_locks,
                realm="/realm/main",
                holder=coordinator_holder("swr_first"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
                label="merge",
            ):
                with pytest.raises(ScopeCommitBusy):
                    with commit_guard(
                        second.path_locks,
                        realm="/realm/main",
                        holder=coordinator_holder("swr_second"),
                        generation=0,
                        in_process_lock=threading.RLock(),
                        clock=clock,
                        wait_s=1.0,
                        poll_s=0.25,
                        label="merge",
                    ):
                        raise AssertionError("the second coordinator must not get in")
            # It WAITED rather than failing instantly — a commit is short, and
            # a crashed holder's lock lapses inside the window.
            assert clock.sleeps

            # Once the first coordinator is out, the second gets straight in.
            with commit_guard(
                second.path_locks,
                realm="/realm/main",
                holder=coordinator_holder("swr_second"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
                wait_s=1.0,
                label="merge",
            ) as result:
                assert result is not None and result.status == "granted"
        finally:
            first.close()
            second.close()

    def test_a_work_lock_does_not_gate_a_git_op(self, tmp_path: Path, enforcing: None) -> None:
        """A work lock says "an agent is editing this file"; it was never a
        statement about the coordinator's own serialized git commands. Waiting
        on one (held for a whole attempt) while holding this task's locks is
        the hold-and-wait the store's deadlock-freedom argument does not cover,
        so the guard proceeds — exactly today's behaviour."""
        first, second = self._dal_pair(tmp_path)
        clock = _StepClock()
        try:
            first.path_locks.try_acquire_scope(
                [ScopeClaim.for_path("/realm/main", "src/a.txt", kind="file")],
                _foreign_holder(),
                generation=0,
            )
            with commit_guard(
                second.path_locks,
                realm="/realm/main",
                holder=coordinator_holder("swr_second"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=clock,
                wait_s=60.0,
                label="snapshot",
            ) as result:
                assert result.result is None  # proceeding WITHOUT the durable lock
                assert result.status == "disabled"
                assert result.granted is True
            assert clock.sleeps == []  # and without waiting for it
        finally:
            first.close()
            second.close()

    def test_the_guard_releases_only_its_own_lock(self, tmp_path: Path, enforcing: None) -> None:
        """A coordinator that also holds work locks must not lose them when a
        commit lock is dropped."""
        dal, _unused = self._dal_pair(tmp_path)
        holder = coordinator_holder("swr_first")
        try:
            dal.path_locks.try_acquire_scope(
                [ScopeClaim.for_path("/realm/main", "PLAN.md", kind="file")],
                holder,
                generation=0,
            )
            with commit_guard(
                dal.path_locks,
                realm="/realm/main",
                holder=holder,
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
            ):
                held = {lock.purpose for lock in dal.path_locks.held_by(holder.kind, holder.id)}
                assert held == {"work", COMMIT_PURPOSE}
            after = dal.path_locks.held_by(holder.kind, holder.id)
            assert [lock.purpose for lock in after] == ["work"]
        finally:
            dal.close()
            _unused.close()

    def test_a_broken_lock_table_fails_closed(self, tmp_path: Path, enforcing: None) -> None:
        """Acquire store errors must fail closed (raise ScopeCommitBusy)."""

        class Broken:
            def try_acquire_scope(self, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.OperationalError("no such table: resource_locks")

        with pytest.raises(ScopeCommitBusy, match="commit lock store error"):
            with commit_guard(
                Broken(),  # type: ignore[arg-type]
                realm="/realm/main",
                holder=coordinator_holder("swr_x"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
            ):
                pass

    def test_slow_live_holder_renews_and_blocks_second(
        self, tmp_path: Path, enforcing: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H-20: a slow live holder renews; a peer cannot take the commit lock."""
        import time

        from omniagentos.swarm import scope_wiring as sw

        # resource_locks timestamps are second-granularity, so TTL must be >= 2s
        # for a sub-second hold to still look live. A non-renewing holder would
        # still be reaped after this TTL; renewal is what keeps the peer out.
        monkeypatch.setenv("OMNIAGENTOS_SCOPE_TTL_S", "2")
        monkeypatch.setattr(sw, "commit_renew_interval_s", lambda ttl_s=None: 0.3)

        first, second = self._dal_pair(tmp_path)
        second_got_in = threading.Event()
        second_error: list[BaseException] = []
        hold_done = threading.Event()

        def _peer() -> None:
            try:
                with commit_guard(
                    second.path_locks,
                    realm="/realm/main",
                    holder=coordinator_holder("swr_second"),
                    generation=0,
                    in_process_lock=threading.RLock(),
                    clock=_StepClock(),
                    wait_s=2.5,
                    poll_s=0.1,
                    label="peer-merge",
                ):
                    # Only count entry that happens while the first still holds.
                    if not hold_done.is_set():
                        second_got_in.set()
            except ScopeCommitBusy as exc:
                second_error.append(exc)
            except Exception as exc:  # noqa: BLE001
                second_error.append(exc)

        try:
            with commit_guard(
                first.path_locks,
                realm="/realm/main",
                holder=coordinator_holder("swr_first"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
                label="slow-merge",
            ) as result:
                assert result is not None and result.status == "granted"
                assert first.path_locks.held_by("coordinator", "swr_first")
                peer = threading.Thread(target=_peer, daemon=True)
                peer.start()
                # Hold past at least one full TTL window; renew must keep us live.
                time.sleep(2.6)
                assert not second_got_in.is_set()
                # Lease must still be live for the original holder.
                assert first.path_locks.renew_scope("coordinator", "swr_first", 0) is True
            hold_done.set()
            peer.join(timeout=3.0)
            assert not second_got_in.is_set()
            # Fresh acquire after the first is out must succeed.
            with commit_guard(
                second.path_locks,
                realm="/realm/main",
                holder=coordinator_holder("swr_second"),
                generation=0,
                in_process_lock=threading.RLock(),
                clock=_StepClock(),
                wait_s=1.0,
                label="after",
            ) as late:
                assert late is not None and late.status == "granted"
        finally:
            first.close()
            second.close()

    def test_displaced_holder_gets_hard_fence_on_stale_generation(
        self, tmp_path: Path, enforcing: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H-20: a wrong fencing generation loses the lease mid-hold and raises."""
        import time

        from omniagentos.swarm import scope_wiring as sw

        monkeypatch.setenv("OMNIAGENTOS_SCOPE_TTL_S", "2.0")
        monkeypatch.setattr(sw, "commit_renew_interval_s", lambda ttl_s=None: 0.05)

        dal, _unused = self._dal_pair(tmp_path)
        holder = coordinator_holder("swr_displaced")
        try:
            with pytest.raises(ScopeCommitBusy, match="lost the commit lease|adopted away"):
                with commit_guard(
                    dal.path_locks,
                    realm="/realm/main",
                    holder=holder,
                    generation=0,
                    in_process_lock=threading.RLock(),
                    clock=_StepClock(),
                    label="stale-gen",
                ) as result:
                    assert result is not None and result.status == "granted"
                    # Simulate adoption: re-key the live commit lock to gen 1.
                    # The renew thread still carries generation=0 and must fail.
                    now_rows = dal.path_locks.held_by(holder.kind, holder.id)
                    assert now_rows
                    conn = sqlite3.connect(str(tmp_path / "commit.db"))
                    try:
                        conn.execute(
                            "UPDATE resource_locks SET holder_generation = 1 "
                            "WHERE holder_kind = ? AND holder_id = ? AND released_at IS NULL",
                            (holder.kind, holder.id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    # Wait for at least one refused renew cycle.
                    time.sleep(0.25)
            # After the fence, gen 0 must not still own a live commit lock.
            live = [
                row
                for row in dal.path_locks.held_by(holder.kind, holder.id)
                if row.purpose == COMMIT_PURPOSE
            ]
            # Either released on exit (gen mismatch → 0 rows freed, then reaped
            # or left at gen 1) — gen 0 must not be renewable.
            assert dal.path_locks.renew_scope(holder.kind, holder.id, 0) is False
            del live  # held_by filters by live rows; gen 1 rows may still exist
        finally:
            dal.close()
            _unused.close()

    def test_renew_store_error_blocks_mutation(
        self, tmp_path: Path, enforcing: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H-20: a renew store error is a hard fence, not a silent continue."""
        import time

        from omniagentos.swarm import scope_wiring as sw

        monkeypatch.setenv("OMNIAGENTOS_SCOPE_TTL_S", "2.0")
        monkeypatch.setattr(sw, "commit_renew_interval_s", lambda ttl_s=None: 0.05)

        dal, _unused = self._dal_pair(tmp_path)
        real_renew = dal.path_locks.renew_scope
        calls = {"n": 0}

        def _boom(holder_kind: str, holder_id: str, generation: int, *a: Any, **k: Any) -> bool:
            calls["n"] += 1
            if calls["n"] >= 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real_renew(holder_kind, holder_id, generation, *a, **k)

        monkeypatch.setattr(dal.path_locks, "renew_scope", _boom)
        try:
            with pytest.raises(ScopeCommitBusy, match="lost the commit lease"):
                with commit_guard(
                    dal.path_locks,
                    realm="/realm/main",
                    holder=coordinator_holder("swr_io"),
                    generation=0,
                    in_process_lock=threading.RLock(),
                    clock=_StepClock(),
                    label="io-fail",
                ):
                    time.sleep(0.2)
            assert calls["n"] >= 1
        finally:
            dal.close()
            _unused.close()

    def test_commit_fence_contract_is_exported(self) -> None:
        """L10 integration surface: fencing contract is inspectable with schema version and capabilities."""
        from omniagentos.swarm.scope_wiring import (
            COMMIT_FENCE_CONTRACT,
            commit_fence_contract,
        )

        contract = commit_fence_contract()
        assert contract["contract"] == COMMIT_FENCE_CONTRACT
        assert contract["version"] == 1
        assert contract["semantic_version"] == "1.0.0"
        assert contract["commit_guard"] is True
        assert contract["commit_claim"] is True
        caps = contract["capabilities"]
        assert isinstance(caps, (tuple, list, set))
        assert "commit_guard" in caps
        assert "commit_claim" in caps
        assert contract["renews_while_held"] is True
        assert contract["displaced_generation_raises"] == "ScopeCommitBusy"
        assert contract["renew_or_store_error"] == "blocks_mutation"
        assert contract["acquire_store_error"] == "blocks_mutation"
        assert contract["reap_basis"] == "expires_at"
        assert contract["fence_handle"] == "pollable_checkpoint"
        assert contract["checkpoint_supported"] is True

    def test_l10_actual_consumer_accepts_l11_contract_and_fails_closed(self) -> None:
        """Exercise actual L10 verify_l11_fence_capability against actual L11 contract.

        Proves production L10 consumer passes with actual L11 contract, and
        fails closed when version or capabilities are missing or malformed.
        """
        import importlib.util
        from unittest.mock import patch

        l10_path = Path(
            "/Users/youruser/OmniAgentOS-reliability-20260725/l10-swarm-lifecycle/omniagentos/swarm/collision_safety.py"
        )
        if not l10_path.is_file():
            l10_path = (
                Path(__file__).resolve().parents[3]
                / "l10-swarm-lifecycle"
                / "omniagentos"
                / "swarm"
                / "collision_safety.py"
            )
        if not l10_path.is_file():
            pytest.skip(f"L10 collision_safety.py not found at {l10_path}")

        spec = importlib.util.spec_from_file_location("l10_collision_safety_test", l10_path)
        assert spec is not None and spec.loader is not None
        l10_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(l10_mod)

        # 1. Actual un-mocked L10 consumer against actual L11 contract
        assert l10_mod.verify_l11_fence_capability() is True

        # 2. Fail closed on string or zero version
        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={
                "contract": "test",
                "version": "1.0.0",
                "capabilities": ("commit_guard", "commit_claim"),
            },
        ):
            assert l10_mod.verify_l11_fence_capability() is False

        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={
                "contract": "test",
                "version": 0,
                "capabilities": ("commit_guard", "commit_claim"),
            },
        ):
            assert l10_mod.verify_l11_fence_capability() is False

        # 3. Fail closed on missing required capability in capabilities
        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={
                "contract": "test",
                "version": 1,
                "capabilities": ("commit_guard",),
            },
        ):
            assert l10_mod.verify_l11_fence_capability() is False

        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={
                "contract": "test",
                "version": 1,
                "capabilities": ("commit_claim",),
            },
        ):
            assert l10_mod.verify_l11_fence_capability() is False

        # 4. Fail closed on empty or malformed capabilities
        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={"contract": "test", "version": 1, "capabilities": ()},
        ):
            assert l10_mod.verify_l11_fence_capability() is False

        with patch(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            return_value={
                "contract": "test",
                "version": 1,
                "capabilities": None,
                "commit_guard": True,
                "commit_claim": False,
            },
        ):
            assert l10_mod.verify_l11_fence_capability() is False

    def test_commit_fence_poll_and_checkpoint_on_lease_loss(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        """Destructive callers can poll handle state and checkpoint before mutating state."""
        dal, _unused = self._dal_pair(tmp_path)
        holder = coordinator_holder("swr_poll_cp")
        try:
            with pytest.raises(ScopeCommitBusy, match="refused for generation 1"):
                with commit_guard(
                    dal.path_locks,
                    realm="/realm/main",
                    holder=holder,
                    generation=1,
                    in_process_lock=threading.RLock(),
                    clock=_StepClock(),
                    label="poll-test",
                ) as fence:
                    assert fence.granted is True
                    assert fence.status == "granted"
                    assert fence.is_lost is False
                    assert fence.lost_reason is None

                    poll_data = fence.poll()
                    assert poll_data["granted"] is True
                    assert poll_data["status"] == "granted"
                    assert poll_data["lost"] is False
                    assert poll_data["generation"] == 1
                    assert poll_data["holder"] == "coordinator:swr_poll_cp"
                    assert poll_data["realm"] == "/realm/main"

                    # Calling checkpoint on healthy fence does not raise
                    fence.checkpoint()

                    # Simulate mid-operation lease loss
                    fence.mark_lost("refused for generation 1")
                    assert fence.is_lost is True
                    assert fence.lost_reason == "refused for generation 1"

                    poll_data = fence.poll()
                    assert poll_data["lost"] is True
                    assert poll_data["reason"] == "refused for generation 1"

                    # Checkpoint before destructive mutation raises ScopeCommitBusy
                    fence.checkpoint()
        finally:
            dal.close()
            _unused.close()


# ---------------------------------------------------------------------------
# 5. Adoption: the generation bump carries the locks
# ---------------------------------------------------------------------------


def _stale_heartbeat(db_path: str, run_id: str, minutes: float = 10.0) -> None:
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE swarm_runs SET heartbeat_at = ? WHERE id = ?", (stamp, run_id))
        connection.commit()
    finally:
        connection.close()


class TestAdoptionFencing:
    def _seeded(self, tmp_path: Path) -> tuple[SwarmDal, str, str]:
        db = migrated_db(CollabStore, tmp_path / "adopt.db")
        dal = SwarmDal(db)
        run_id = str(dal.create_run(working_dir=str(tmp_path), source="test", goal="adopt me")["id"])
        assert dal.try_activate_run(run_id)
        return dal, db, run_id

    def test_adoption_rekeys_the_runs_locks_and_fences_the_displaced(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        dal, db, run_id = self._seeded(tmp_path)
        holder = task_holder("tsk_a")
        claims = owned_path_claims("/realm/main", ["src/a"], owner_group=run_id)
        try:
            granted = dal.path_locks.try_acquire_scope(
                claims, holder, generation=0, owner_group=run_id
            )
            assert granted.status == "granted"

            _stale_heartbeat(db, run_id)
            assert dal.adopt_run(run_id, stale_minutes=2.0) is True
            assert int(dal.get_run(run_id)["lease_generation"]) == 1

            # The locks moved to the adopter's generation in the SAME transaction.
            assert [row["holder_generation"] for row in _live_locks(db)] == [1]

            # The displaced coordinator can neither free them …
            assert dal.path_locks.release_scope(holder.kind, holder.id, 0) == 0
            # … nor keep them alive …
            assert dal.path_locks.renew_scope(holder.kind, holder.id, 0) is False
            # … nor re-enter the fleet at its stale generation.
            assert dal.path_locks.try_acquire_scope(claims, holder, generation=0).status == "fenced"
            assert len(_live_locks(db)) == 1

            # The adopter owns them outright.
            assert dal.path_locks.renew_scope(holder.kind, holder.id, 1) is True
            assert dal.path_locks.release_scope(holder.kind, holder.id, 1) == 1
        finally:
            dal.close()

    def test_adoption_leaves_another_runs_locks_alone(
        self, tmp_path: Path, enforcing: None
    ) -> None:
        """The re-key is scoped to the adopted run's own owner group — a
        neighbouring run's locks are not swept along with it."""
        dal, db, run_id = self._seeded(tmp_path)
        other_run = str(dal.create_run(working_dir=str(tmp_path), source="test", goal="innocent")["id"])
        try:
            dal.path_locks.try_acquire_scope(
                owned_path_claims("/realm/main", ["src/a"], owner_group=run_id),
                task_holder("tsk_a"),
                generation=0,
                owner_group=run_id,
            )
            dal.path_locks.try_acquire_scope(
                owned_path_claims("/realm/main", ["src/b"], owner_group=other_run),
                task_holder("tsk_b"),
                generation=0,
                owner_group=other_run,
            )
            _stale_heartbeat(db, run_id)
            assert dal.adopt_run(run_id, stale_minutes=2.0) is True

            by_owner = {row["owner_group"]: row["holder_generation"] for row in _live_locks(db)}
            assert by_owner == {run_id: 1, other_run: 0}
        finally:
            dal.close()

    def test_adoption_touches_no_lock_rows_when_scope_locks_are_off(
        self, tmp_path: Path, default_off: None
    ) -> None:
        """With the mechanism off, adoption is byte-for-byte what it was: the
        re-key statement does not run at all (a seeded row keeps generation 0
        even though the run's generation moved)."""
        dal, db, run_id = self._seeded(tmp_path)
        try:
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    "INSERT INTO resource_locks (id, realm, rel_path, depth, kind, holder_kind, "
                    "holder_id, holder_generation, lane, owner_group, purpose, acquired_at, "
                    "expires_at) VALUES ('lck_seed', '/realm/main', 'src/a', 1, 'root', "
                    "'swarm_task', 'tsk_a', 0, 'swarm', ?, 'work', '2099-01-01T00:00:00Z', "
                    "'2099-01-01T00:00:00Z')",
                    (run_id,),
                )
                connection.commit()
            finally:
                connection.close()

            _stale_heartbeat(db, run_id)
            assert dal.adopt_run(run_id, stale_minutes=2.0) is True
            assert int(dal.get_run(run_id)["lease_generation"]) == 1
            assert [row["holder_generation"] for row in _live_locks(db)] == [0]
        finally:
            dal.close()
