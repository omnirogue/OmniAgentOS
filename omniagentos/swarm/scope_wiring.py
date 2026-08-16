"""The swarm lane's adapter onto the durable, cross-lane scope-lock store.

Everything here is INERT while :func:`~omniagentos.scope.config.scope_locks_mode`
is ``off`` (the default). Every entry point short-circuits on the mode BEFORE it
touches the database, and before it resolves a realm — :func:`realm_of` can shell
out to ``git rev-parse``, so a mode check that happened after it would still cost
the lane a subprocess per attempt. With the mode off, the only thing
:func:`commit_guard` does is take the in-process ``threading.RLock`` it was
handed, which is byte-for-byte what ``with state.git_lock:`` did before this
module existed. ``tests/swarm/test_scope_wiring.py`` pins that.

WHAT THE SWARM LANE DECLARES
============================
Nothing new. ``swarm_json.owned_paths`` is planner-assigned (``planner.py``,
``normalize_owned_path`` + ``add_ownership_overlap_edges``) and has been the
lane's ownership declaration since WP5a. :func:`owned_path_claims` maps each
owned path to a ``kind='root'`` claim, which is EXACTLY the semantics of
``SwarmScheduler._path_owned`` (``normalized == owned_norm or
normalized.startswith(owned_norm + '/')`` — path-at-or-below, i.e. a subtree),
so the claim set can never be narrower than the ownership rule the scheduler
already enforces on the post-attempt diff. ``owned_paths == ['.']`` (the
integration task) normalizes to the empty component tuple, which the algebra
already defines as the WHOLE realm — the same "owns everything" that
``_path_owned``'s ``owned == '.'`` branch returns True for.

A path that will not normalize at all (absolute, ``~``, an escape) is widened to
the whole realm rather than dropped. Dropping it would silently narrow the
declared scope, which is the one failure mode a lock table must not have; the
whole realm is the broadest reading and therefore fails closed.

WHICH REALM A TASK CLAIMS IN — the load-bearing decision
========================================================
A task claims its owned paths in the realm of the directory it will actually
WRITE in: its private worktree in Phase-2 worktree mode, the main workspace in
the Phase-1 shared-directory model.

The tempting alternative — always claim in the MAIN workspace realm, treating
the claim as a reservation on the paths the branch will eventually merge — is
WRONG, and not marginally so. A commit claim is the whole realm
(``components == ()``, ``purpose='commit'``; see
:meth:`~omniagentos.scope.locks.PathLockStore.commit_lock`), and the conflict
matrix says a whole-realm root collides with EVERY other claim in its realm.
So a main-realm work claim held for the length of an attempt (minutes) makes the
coordinator's commit lock unobtainable for the whole fleet, and a worker holding
work locks that then waits for the commit lock is precisely the hold-and-wait
that :mod:`omniagentos.scope.locks`'s deadlock-freedom argument does NOT
cover — that argument is about ONE all-or-nothing acquire, and a commit lock
taken while work locks are held is a second one.

Claiming where the writes land keeps the two mechanisms orthogonal:

* worktree mode — worktrees are private realms (see
  :func:`~omniagentos.scope.paths.private_workspace_bases`), so task claims never
  meet the main realm's commit claim and the cross-process commit lock is a true
  mutex. Two tasks cannot collide on files anyway (separate checkouts); they
  collide at MERGE time, which the merge machinery already routes to integration.
* Phase-1 mode — the worker writes into the main workspace, so the claim lands in
  the main realm and delivers real CROSS-LANE exclusion (a runner run may not
  edit a file a swarm worker owns). Sibling swarm tasks essentially never collide
  there, because ``planner.add_ownership_overlap_edges`` already turns
  overlapping ownership into a dependency edge. The commit guard degrades to the
  in-process lock in that mode (see below) — i.e. to exactly today's behaviour.

WHY THE COMMIT GUARD DEGRADES INSTEAD OF WAITING
================================================
``_RunState.git_lock`` is a ``threading.RLock``: IN-PROCESS ONLY. Two
coordinators in two processes (the API and the sessions daemon, or two dev
shells) can interleave a ``git merge`` on the same repo today. :func:`commit_guard`
adds the durable, cross-process half — and keeps the RLock, which is cheaper for
the common intra-process case and stops every worker thread in a process from
queueing on SQLite.

Its refusal policy has exactly two branches, and the split is the reason the
guard cannot deadlock:

* blocked by another COMMIT claim — that is the collision the guard exists to
  prevent. Retry until ``wait_s`` (the default comfortably exceeds the default
  lock TTL, so a holder that CRASHED always lapses inside the window), then raise
  :class:`ScopeCommitBusy`. Every call site degrades safely on that: the
  pre-attempt snapshot requeues the task, the merge routes to integration as a
  merge-infrastructure failure, the GC passes log and move on.
* blocked by a WORK claim — proceed under the in-process lock alone. Work locks
  gate an agent's file writes; they were never a statement about the
  coordinator's own serialized git commands, and waiting on one (held for the
  length of an attempt) while holding this task's own locks is the hold-and-wait
  case above. Proceeding here is exactly today's behaviour, so it can never be a
  regression — it is simply a place where the new guarantee does not extend.

GENERATION / ADOPTION
=====================
Every acquire, renew and release carries the coordinator's
``swarm_runs.lease_generation``. ``SwarmDal.adopt_run`` bumps that generation and
re-keys the run's live locks to it inside the SAME transaction, so a displaced
coordinator's generation-fenced release matches zero rows and cannot free the
adopter's locks — and its next acquire is refused outright (``status='fenced'``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

from omniagentos.scope.config import (
    ScopeLockMode,
    scope_locks_enabled,
    scope_locks_mode,
    scope_ttl_s,
)
from omniagentos.scope.locks import (
    AcquireResult,
    HolderKind,
    Lane,
    LockHolder,
    PathLockStore,
    ScopeConflict,
)
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import WHOLE_REALM, ScopePathError, normalize_rel, realm_of

__all__ = [
    "COMMIT_FENCE_CONTRACT",
    "COORDINATOR_HOLDER_KIND",
    "DEFAULT_COMMIT_POLL_S",
    "DEFAULT_COMMIT_WAIT_S",
    "SWARM_LANE",
    "TASK_HOLDER_KIND",
    "CommitFence",
    "ScopeCommitBusy",
    "claim_task_scope",
    "clear_task_waiter",
    "commit_claim",
    "commit_fence_contract",
    "commit_guard",
    "commit_renew_interval_s",
    "coordinator_holder",
    "owned_path_claims",
    "park_task_waiter",
    "realm_for",
    "release_holder_scope",
    "renew_holder_scope",
    "scope_locks_active",
    "task_holder",
]

LOG = logging.getLogger(__name__)

#: The ``lane`` column value for every lock this module takes (migration 059's
#: CHECK). One generic table with a lane COLUMN is the point — the conflicts
#: being prevented are cross-lane.
SWARM_LANE: Lane = "swarm"

#: ``holder_kind`` for a task's work locks. The identity is the TASK, not the
#: attempt: a same-tier retry re-runs the same task in the same worktree against
#: the same owned paths, and re-keying the locks per attempt would make each
#: retry look like a new claimant to the fence.
TASK_HOLDER_KIND: HolderKind = "swarm_task"

#: ``holder_kind`` for the run's commit locks. Every git mutation in the swarm
#: lane is coordinator-owned (see the scheduler's module docstring), so the
#: commit lock is held by the coordinator identity, not by whichever worker
#: thread happened to reach the git call.
COORDINATOR_HOLDER_KIND: HolderKind = "coordinator"

#: How long :func:`commit_guard` waits out ANOTHER commit lock before giving up.
#: Sits between two defaults, and both bounds are real:
#:
#: * ABOVE the default lock TTL (90s), so a holder that died mid-commit always
#:   lapses inside the window and nobody raises over a corpse;
#: * BELOW the default swarm adopt window (``adopt_stale_minutes``, 2 min), so a
#:   coordinator waiting here on its own thread cannot miss enough heartbeats to
#:   be adopted away by another process while it waits.
DEFAULT_COMMIT_WAIT_S = 100.0

#: Poll interval while waiting for a commit lock. The store never blocks, so the
#: wait has to be a poll; the interval is small enough to be invisible next to a
#: git command and large enough not to hammer the writer lock.
DEFAULT_COMMIT_POLL_S = 0.25

#: Integration contract L10 (and any enforcer) can inspect before flipping
#: ``scope_locks: enforce``. A coordinator may mutate git only while inside
#: :func:`commit_guard` AND its generation keeps renewing. A failed renew or
#: a stale generation is a hard :class:`ScopeCommitBusy`, never a silent
#: degrade to the in-process lock alone.
COMMIT_FENCE_CONTRACT = "generation-fenced-renewing-commit-lock"


def commit_fence_contract() -> dict[str, object]:
    """Stable description of the cross-process commit fence (L10 integration).

    L10 must not enable enforcement until this contract is present and the
    surrounding shadow soak has evidence. Keys are stable; values may grow.
    """
    return {
        "contract": COMMIT_FENCE_CONTRACT,
        "version": 1,
        "semantic_version": "1.0.0",
        "capabilities": ("commit_guard", "commit_claim"),
        "commit_guard": True,
        "commit_claim": True,
        "renews_while_held": True,
        "displaced_generation_raises": "ScopeCommitBusy",
        "renew_or_store_error": "blocks_mutation",
        "acquire_store_error": "blocks_mutation",
        "reap_basis": "expires_at",
        "fence_handle": "pollable_checkpoint",
        "checkpoint_supported": True,
        "module": "omniagentos.swarm.scope_wiring",
    }


class CommitFence:
    """Handle to a granted or attempted commit fence (L10/L11 join contract).

    Callers, especially destructive ones like git merge/commit, can poll
    :attr:`is_lost`, read :attr:`lost_reason`, call :meth:`poll`, or invoke
    :meth:`checkpoint` before mutating state.
    """

    def __init__(
        self,
        result: AcquireResult | None,
        holder: LockHolder | None,
        generation: int,
        realm: str | None = None,
    ) -> None:
        self.result = result
        self.holder = holder
        self.generation = generation
        self.realm = realm
        self._lost = False
        self._lost_reason: str | None = None
        self._lock = threading.Lock()

    def mark_lost(self, reason: str) -> None:
        """Mark the fence as lost due to refused renewal or store error."""
        with self._lock:
            if not self._lost:
                self._lost = True
                self._lost_reason = reason

    @property
    def is_lost(self) -> bool:
        """True if the lease was lost during execution."""
        with self._lock:
            return self._lost

    @property
    def lost_reason(self) -> str | None:
        """Description of why the lease was lost, or None if live."""
        with self._lock:
            return self._lost_reason

    @property
    def granted(self) -> bool:
        """True if the commit lock was granted or if locking is disabled."""
        return self.result is None or self.result.granted

    @property
    def status(self) -> str:
        """Acquire status ('granted', 'disabled', 'refused', 'fenced')."""
        return self.result.status if self.result is not None else "disabled"

    @property
    def mode(self) -> ScopeLockMode:
        """Scope lock mode at acquire time."""
        return self.result.mode if self.result is not None else "off"

    @property
    def lock_ids(self) -> tuple[str, ...]:
        """IDs of active lock rows held for this fence."""
        return self.result.lock_ids if self.result is not None else ()

    @property
    def conflict(self) -> ScopeConflict | None:
        """Observed conflict if refused or shadow-observed."""
        return self.result.conflict if self.result is not None else None

    @property
    def shadowed(self) -> bool:
        """True if acquired in shadow mode."""
        return self.result.shadowed if self.result is not None else False

    @property
    def skipped_duplicates(self) -> tuple[str, ...]:
        """Duplicate claims skipped during acquire."""
        return self.result.skipped_duplicates if self.result is not None else ()

    @property
    def blocked_on(self) -> str:
        """Lock ID of blocker if refused."""
        return self.result.blocked_on if self.result is not None else ""

    @property
    def blocked_path(self) -> str:
        """Path text of claim that blocked acquire."""
        return self.result.blocked_path if self.result is not None else ""

    def poll(self) -> dict[str, object]:
        """Pollable status dictionary for health/telemetry checks."""
        holder_str = f"{self.holder.kind}:{self.holder.id}" if self.holder else None
        return {
            "granted": self.granted,
            "status": self.status,
            "lost": self.is_lost,
            "reason": self.lost_reason,
            "generation": self.generation,
            "holder": holder_str,
            "realm": self.realm,
            "lock_ids": self.lock_ids,
        }

    def checkpoint(self) -> None:
        """Checkpoint lease health before performing a destructive operation.

        Raises :class:`ScopeCommitBusy` immediately if the lease was lost or
        refused during background renewal.
        """
        if self.is_lost:
            holder_str = f"{self.holder.kind}:{self.holder.id}" if self.holder else "coordinator"
            reason = self.lost_reason or "lease lost"
            raise ScopeCommitBusy(
                f"{holder_str} generation {self.generation} lost the commit lease: "
                f"{reason} — holder is fenced"
            )


def commit_renew_interval_s(ttl_s: float | None = None) -> float:
    """How often the commit guard refreshes a held lease.

    Well inside the TTL so a merely slow ``git merge`` cannot be reaped from
    under a live holder. Floored so short-TTL tests still exercise renewal.
    """
    ttl = float(scope_ttl_s() if ttl_s is None else ttl_s)
    if ttl <= 0:
        return DEFAULT_COMMIT_POLL_S
    return max(0.05, min(ttl / 3.0, ttl * 0.4))


class ScopeCommitBusy(RuntimeError):
    """Another process holds this realm's commit lock and would not let go.

    A ``RuntimeError`` on purpose: every git call site in the scheduler already
    sits inside an ``except Exception`` that degrades safely (requeue the task,
    route the merge to integration, log the GC failure), so raising this can
    never turn a lock timeout into a lost run.
    """


class _ClockProto(Protocol):
    """The two clock methods this module uses (the scheduler's injected clock).

    Structural, so the annotations read honestly without this module importing
    the scheduler — which imports this module.
    """

    def monotonic(self) -> float: ...  # noqa: D102

    def sleep(self, seconds: float) -> None: ...  # noqa: D102


def scope_locks_active() -> bool:
    """True when lock rows are written at all (``shadow`` or ``enforce``).

    Re-exported so the scheduler has ONE import surface for scope wiring and
    cannot accidentally couple to a different half of the config module.
    """
    return scope_locks_enabled()


# ---------------------------------------------------------------------------
# Identities and claims
# ---------------------------------------------------------------------------


def task_holder(task_id: str) -> LockHolder:
    """The lock identity of one swarm task's work locks."""
    return LockHolder(kind=TASK_HOLDER_KIND, id=str(task_id), lane=SWARM_LANE)


def coordinator_holder(run_id: str) -> LockHolder:
    """The lock identity of one run's coordinator (commit locks)."""
    return LockHolder(kind=COORDINATOR_HOLDER_KIND, id=str(run_id), lane=SWARM_LANE)


def realm_for(path: str) -> str | None:
    """The realm key for a workspace/worktree directory, or ``None``.

    ``None`` means the path could not be resolved at all, and the caller must
    then skip locking rather than invent a realm: a claim in a made-up realm
    conflicts with nothing and would be worse than no claim, because it would
    LOOK like protection.
    """
    try:
        realm = realm_of(path)
    except Exception:  # noqa: BLE001 -- realm resolution must never kill an attempt.
        LOG.warning("scope: realm resolution failed for %s", path, exc_info=True)
        return None
    if realm is None:
        LOG.warning("scope: no realm for %s — swarm scope locking skipped", path)
    return realm


def owned_path_claims(
    realm: str,
    owned_paths: Iterable[str],
    *,
    owner_group: str = "",
) -> tuple[ScopeClaim, ...]:
    """Map ``owned_paths`` to ``kind='root'`` claims — today's ``_path_owned``.

    Root (subtree) is not a widening: ``_path_owned`` already accepts any path
    at-or-below an owned path. An unnormalizable path widens to the whole realm
    (see the module docstring) instead of being dropped.
    """
    claims: list[ScopeClaim] = []
    for raw in owned_paths:
        text = str(raw)
        try:
            components = normalize_rel(text)
        except ScopePathError:
            LOG.warning(
                "scope: owned path %r is not realm-relative — widening the claim to the "
                "whole realm %s rather than narrowing the declared scope",
                text,
                realm,
            )
            components = WHOLE_REALM
        claims.append(
            ScopeClaim(
                realm=realm,
                components=components,
                kind="root",
                owner_group=owner_group,
            )
        )
    return tuple(claims)


def commit_claim(realm: str, *, owner_group: str = "") -> ScopeClaim:
    """The whole-realm commit claim, identical to ``PathLockStore.commit_lock``'s.

    Reproduced here (rather than calling ``commit_lock``) because
    :func:`commit_guard` needs the :class:`AcquireResult` in hand to decide
    between waiting, degrading and raising, and ``commit_lock`` converts a
    refusal into an exception before the caller can see WHAT refused it. The
    claim shape — whole realm, ``kind='root'``, ``purpose='commit'`` — and the
    release-by-lock-id are the same, so the two are interchangeable to every
    other holder in the table.
    """
    return ScopeClaim(
        realm=realm,
        components=(),
        kind="root",
        owner_group=owner_group,
        purpose=COMMIT_PURPOSE,
    )


def _disabled(mode: ScopeLockMode = "off") -> AcquireResult:
    return AcquireResult(status="disabled", mode=mode)


# ---------------------------------------------------------------------------
# Task work locks
# ---------------------------------------------------------------------------


def claim_task_scope(
    locks: PathLockStore | None,
    *,
    exec_dir: str,
    owned_paths: Sequence[str],
    task_id: str,
    run_id: str,
    generation: int,
    replace: bool = False,
) -> tuple[AcquireResult, str | None]:
    """Take a task's whole declared scope, or none of it. Returns (result, realm).

    ``exec_dir`` is the directory the worker will WRITE in (its worktree, or the
    main workspace in Phase-1 mode) — see the module docstring for why that, and
    not the main workspace, is the realm.

    ``replace=True`` uses :meth:`PathLockStore.reacquire_scope`, which releases
    this holder's previous locks in the same transaction: the crash-resume /
    re-attach path, where the task may be resuming with a narrowed scope and
    must actually give the surrendered paths back.

    Returns ``status='disabled'`` with a ``None`` realm when locking is off or
    the directory has no realm, and a bare ``status='granted'`` when the task
    declared nothing to claim. All three are grants as far as the caller is
    concerned (:attr:`AcquireResult.granted` covers them), and none of them
    carries lock ids — which is what stops the caller from recording a lease it
    does not hold.
    """
    if locks is None or not scope_locks_enabled():
        return _disabled(), None
    realm = realm_for(exec_dir)
    if realm is None:
        return _disabled(scope_locks_mode()), None
    claims = owned_path_claims(realm, owned_paths, owner_group=str(run_id))
    if not claims:
        # A task that declares no owned paths owns nothing (``_path_owned``
        # returns False for every path); there is nothing to claim, and opening
        # a write transaction to say so would be pure cost.
        return AcquireResult(status="granted", mode=scope_locks_mode()), realm
    holder = task_holder(task_id)
    acquire = locks.reacquire_scope if replace else locks.try_acquire_scope
    try:
        result = acquire(
            claims,
            holder,
            generation=int(generation),
            owner_group=str(run_id),
        )
    except Exception:  # noqa: BLE001 -- a broken lock table must not wedge the lane.
        LOG.warning(
            "scope: acquire failed for task %s — proceeding unlocked", task_id, exc_info=True
        )
        return _disabled(scope_locks_mode()), realm
    return result, realm


def renew_holder_scope(
    locks: PathLockStore | None,
    holder_kind: str,
    holder_id: str,
    generation: int,
) -> bool:
    """Extend a holder's live locks. False == adopted away, or already lapsed."""
    if locks is None or not scope_locks_enabled():
        return False
    try:
        return locks.renew_scope(holder_kind, holder_id, int(generation))
    except Exception:  # noqa: BLE001 -- renewal is a lease refresh, never control flow.
        LOG.warning("scope: renew failed for %s:%s", holder_kind, holder_id, exc_info=True)
        return False


def release_holder_scope(
    locks: PathLockStore | None,
    holder_kind: str,
    holder_id: str,
    generation: int,
) -> int:
    """Release a holder's locks; returns how many rows were released.

    NOT gated on the mode: a release must still work after an operator flips the
    kill switch off, or the rows a running fleet already holds would sit there
    until their TTL. It is generation-fenced, so a displaced coordinator calling
    this frees nothing.
    """
    if locks is None:
        return 0
    try:
        return locks.release_scope(holder_kind, holder_id, int(generation))
    except Exception:  # noqa: BLE001 -- cleanup is best-effort; the TTL is the backstop.
        LOG.warning("scope: release failed for %s:%s", holder_kind, holder_id, exc_info=True)
        return 0


def park_task_waiter(
    locks: PathLockStore | None,
    realm: str | None,
    task_id: str,
    result: AcquireResult,
) -> None:
    """Record a refused task at the back of the realm's FIFO queue.

    Idempotent in the store (re-parking keeps the original queue position), so
    the scheduler may call it on every refused retry without starving itself.
    """
    if locks is None or realm is None or not scope_locks_enabled():
        return
    try:
        locks.park_waiter(
            realm,
            task_holder(task_id),
            blocked_on=result.blocked_on,
            blocked_path=result.blocked_path,
        )
    except Exception:  # noqa: BLE001 -- the waiter row is diagnostics + FIFO, never a gate.
        LOG.warning("scope: could not park waiter for task %s", task_id, exc_info=True)


def clear_task_waiter(locks: PathLockStore | None, task_id: str) -> None:
    """Clear a task's park row once it has its scope (or has stopped wanting it)."""
    if locks is None:
        return
    try:
        locks.clear_waiter(TASK_HOLDER_KIND, str(task_id))
    except Exception:  # noqa: BLE001
        LOG.debug("scope: could not clear waiter for task %s", task_id, exc_info=True)


# ---------------------------------------------------------------------------
# The cross-process commit lock
# ---------------------------------------------------------------------------


def _acquire_commit_lock(
    locks: PathLockStore,
    realm: str,
    holder: LockHolder,
    *,
    generation: int,
    clock: _ClockProto,
    wait_s: float,
    poll_s: float,
    label: str,
) -> AcquireResult | None:
    """Take the realm's commit lock, or decide what to do instead.

    Returns the granted result, or ``None`` meaning "proceed under the in-process
    lock alone". Raises :class:`ScopeCommitBusy` on acquire store error or when
    another COMMIT holder outlasted ``wait_s``.
    """
    # ``owner_group`` is the holder's own id — for the coordinator identity that
    # IS the run id, which is exactly the key ``SwarmDal.adopt_run`` re-keys on,
    # so a commit lock held across an adoption travels with the run's generation
    # like every other lock the run holds. It changes no conflict decision: the
    # co-ownership relaxation never relaxes a root claim, and never a commit.
    claims = (commit_claim(realm, owner_group=holder.id),)
    deadline = clock.monotonic() + max(0.0, wait_s)
    while True:
        try:
            result = locks.try_acquire_scope(claims, holder, generation=int(generation))
        except Exception as exc:  # noqa: BLE001 -- fail closed on acquire/store errors
            LOG.warning(
                "scope: commit lock store error for %s — failing closed", label, exc_info=True
            )
            raise ScopeCommitBusy(
                f"commit lock store error for {label} ({holder.kind}:{holder.id}): {exc}"
            ) from exc
        if result.status == "fenced":
            # Adopted away. The scheduler's own m10 fencing (`state.displaced`)
            # is the primary signal; this is the second line of defense, and it
            # must stop the git op rather than degrade to the in-process lock —
            # the adopter is the one allowed to touch this workspace now.
            raise ScopeCommitBusy(
                f"{holder.kind}:{holder.id} generation {generation} has been adopted away ({label})"
            )
        if result.granted:
            return result
        conflict = result.conflict
        if conflict is None or conflict.held.purpose != COMMIT_PURPOSE:
            LOG.debug(
                "scope: %s proceeding without the cross-process commit lock (blocked by %s)",
                label,
                conflict.describe() if conflict is not None else "an unnamed holder",
            )
            return None
        if clock.monotonic() >= deadline:
            raise ScopeCommitBusy(f"{label}: {conflict.describe()}")
        clock.sleep(poll_s)


@contextmanager
def commit_guard(
    locks: PathLockStore | None,
    *,
    realm: str | None,
    holder: LockHolder | None,
    generation: int,
    in_process_lock: threading.RLock,
    clock: _ClockProto,
    wait_s: float = DEFAULT_COMMIT_WAIT_S,
    poll_s: float = DEFAULT_COMMIT_POLL_S,
    label: str = "git",
) -> Iterator[CommitFence]:
    """``in_process_lock`` PLUS, when enabled, the realm's durable commit lock.

    The in-process lock is taken FIRST and always, which is both the fast path
    and a fixed lock order (in-process, then SQLite) that no caller can invert.
    With scope locking off this is the ONLY thing that happens, so the guard is
    byte-for-byte ``with state.git_lock:``.

    Returns a :class:`CommitFence` handle yielding lock health, status, and
    a :meth:`CommitFence.checkpoint` method for destructive callers.

    H-20: once the durable commit lock is granted it is RENEWED on a fraction
    of the lease TTL for the whole critical section. A refused renew (stale
    generation / already reaped) or a store error is a hard
    :class:`ScopeCommitBusy` — the displaced holder must not keep mutating
    silently under a lease it no longer owns. Reaping itself remains
    ``expires_at``-based durable proof; renewal is what stops a live slow
    holder from looking expired.
    """
    with in_process_lock:
        if locks is None or realm is None or holder is None or not scope_locks_enabled():
            yield CommitFence(None, holder, generation, realm)
            return
        result = _acquire_commit_lock(
            locks,
            realm,
            holder,
            generation=generation,
            clock=clock,
            wait_s=wait_s,
            poll_s=poll_s,
            label=label,
        )
        fence = CommitFence(result, holder, generation, realm)
        stop_renew = threading.Event()
        renew_thread: threading.Thread | None = None

        def _renew_loop() -> None:
            interval = commit_renew_interval_s()
            # Wall-clock wait: resource_locks.expires_at is wall-clock, so the
            # injected scheduler clock must not drive lease renewal.
            while not stop_renew.wait(interval):
                try:
                    ok = locks.renew_scope(holder.kind, holder.id, int(generation))
                except Exception as exc:  # noqa: BLE001 -- store errors block mutation (H-20).
                    LOG.warning(
                        "scope: commit lock renew failed for %s (%s) — fencing holder",
                        label,
                        f"{holder.kind}:{holder.id}",
                        exc_info=True,
                    )
                    fence.mark_lost(f"renew store error: {exc}")
                    return
                if not ok:
                    # Generation fenced or lease already reaped — stop, hard-fail.
                    LOG.warning(
                        "scope: commit lock renew refused for %s generation %s (%s)",
                        f"{holder.kind}:{holder.id}",
                        generation,
                        label,
                    )
                    fence.mark_lost(f"renew refused for generation {generation}")
                    return

        if result is not None and result.lock_ids:
            renew_thread = threading.Thread(
                target=_renew_loop,
                name=f"commit-guard-renew:{holder.id}",
                daemon=True,
            )
            renew_thread.start()
        try:
            yield fence
        finally:
            stop_renew.set()
            if renew_thread is not None:
                renew_thread.join(timeout=max(1.0, commit_renew_interval_s() * 2))
            if result is not None and result.lock_ids:
                try:
                    locks.release_scope(
                        holder.kind, holder.id, int(generation), lock_ids=result.lock_ids
                    )
                except Exception:  # noqa: BLE001 -- the TTL is the backstop.
                    LOG.warning(
                        "scope: could not release the commit lock (%s)", label, exc_info=True
                    )
            fence.checkpoint()
