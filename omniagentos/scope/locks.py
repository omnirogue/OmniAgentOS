"""``PathLockStore`` — durable, cross-lane, deadlock-free path ownership.

This is the store behind the algebra in :mod:`omniagentos.scope.conflict`. The
algebra decides *whether* two claims collide; this decides *who holds what*, and
does it durably so that a claim survives a process restart and is visible to
every other lane in the fleet.

DEADLOCK-FREEDOM IS STRUCTURAL, NOT PROCEDURAL
==============================================
:meth:`PathLockStore.try_acquire_scope` is ALL-OR-NOTHING inside ONE
``BEGIN IMMEDIATE``: reap expired locks, read the realm's live set, run
:func:`~omniagentos.scope.conflict.first_conflict` over the WHOLE claim set, then
insert every lock or none of them.

That single property is the entire safety argument. Deadlock requires all four of
Coffman's conditions simultaneously, and **hold-and-wait cannot occur here**: a
claimant either leaves the transaction holding its complete scope or holding
nothing at all, so there is never a holder that is also a waiter. With one of the
four conditions structurally absent, no cycle can form — there is nothing for a
cycle detector to detect, which is why this package has none.

It is also why cross-owner handoff ("hold your locks, I'll pass them to you when
I'm done") is rejected rather than deferred. Handoff is precisely the feature that
reintroduces hold-and-wait, and having reintroduced it you then need a cycle
detector, a victim-selection policy and a rollback path to survive it: strictly
more machinery, in the transaction that must be shortest, for strictly less
safety. On conflict the claimant instead parks a durable waiter row (with a
diagnostic ``blocked_on`` pointer) and is woken FIFO.

``tests/scope/test_locks.py::test_no_partial_acquire`` injects a failure part-way
through the insert loop and asserts ZERO lock rows survive. That test is not a
regression guard for a bug that once happened; it IS the proof of the paragraph
above, and it should be read as such.

THE TRANSACTION SHAPE
=====================
Lifted from ``routing/limit_state.py::reserve_account``, which is this codebase's
proven precedent for "reap + check + claim atomically": one ``BEGIN IMMEDIATE``
covering a lazy TTL reap, the read that the decision depends on, and the write
that acts on it. IMMEDIATE (not DEFERRED) is load-bearing — a deferred
transaction's read does not lock out a concurrent writer, so the conflict check
would be advisory and two processes could both pass it.

The TTL reap is LAZY (at the head of every acquire) and BOUNDED to
:data:`REAP_LIMIT` rows. The bound is what stops a restart storm: when launchd
restarts the fleet, every lease in the database expires within the same second,
and an unbounded ``UPDATE`` would make the first acquire after the restart
rewrite the entire table while holding the write lock.

The bound is a GARBAGE-COLLECTION bound, never a correctness bound. Every read of
"live locks" in this module also filters ``expires_at > now``, so an expired row
the reaper has not reached yet still cannot block anybody. If that filter were
removed, the ``LIMIT`` would silently become a correctness bug.

WHY ONE GENERIC TABLE
=====================
``resource_locks`` is one cross-lane table with a ``lane`` COLUMN, not one table
per lane. The conflict being prevented is inherently cross-lane (a runner run and
a swarm worker writing the same file), and per-lane tables would force a UNION
inside ``BEGIN IMMEDIATE`` — a multi-source read-then-decide, the exact bug class
this phase exists to remove. See the header of migration 059 for the full
argument and the ``account_reservations`` precedent.

FENCING
=======
Every mutation that could affect somebody ELSE'S locks is conditional on
``holder_generation``. Adoption bumps the generation (migration 059 does for
``runs`` what 052 did for ``swarm_runs``), so a merely-wedged worker that wakes up
after being adopted away cannot renew or release the adopter's locks, and cannot
acquire new ones either — :meth:`try_acquire_scope` refuses a generation lower
than one already recorded for the same holder identity. A stale worker's writes
are not merely ignored; the worker is told, and stops.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.scope.config import ScopeLockMode, scope_locks_mode, scope_ttl_s
from omniagentos.scope.conflict import ScopeConflict, first_conflict
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim, ScopeKind
from omniagentos.scope.paths import normalize_rel

__all__ = [
    "HOLDER_KINDS",
    "LANES",
    "REAP_LIMIT",
    "STORED_PURPOSES",
    "AcquireResult",
    "AcquireStatus",
    "HeldLock",
    "HolderKind",
    "Lane",
    "LockHolder",
    "PathLockStore",
    "ScopeLockBusy",
    "ScopeLockError",
    "ScopeLockFenced",
    "StoredPurpose",
    "Waiter",
]

HolderKind = Literal["run", "session", "attempt", "swarm_task", "coordinator"]
Lane = Literal["runner", "session", "longhaul", "swarm"]
StoredPurpose = Literal["work", "commit"]
AcquireStatus = Literal["granted", "blocked", "fenced", "disabled"]

#: Mirrors the ``holder_kind`` CHECK in migration 059. Validated in Python too so
#: a typo names itself instead of surfacing as an opaque IntegrityError.
HOLDER_KINDS: tuple[HolderKind, ...] = (
    "run",
    "session",
    "attempt",
    "swarm_task",
    "coordinator",
)

#: Mirrors the ``lane`` CHECK in migration 059.
LANES: tuple[Lane, ...] = ("runner", "session", "longhaul", "swarm")

#: Mirrors the ``purpose`` CHECK in migration 059. See :func:`_stored_purpose` —
#: the column is a two-valued CLASSIFICATION of ``ScopeClaim.purpose``, which is
#: an open string.
STORED_PURPOSES: tuple[StoredPurpose, ...] = ("work", "commit")

#: Ordinary editing intent as the column spells it. ``ScopeClaim``'s open-string
#: default ("edit") and every other non-commit intent classify to this.
WORK_PURPOSE: StoredPurpose = "work"

#: Rows reaped per lazy sweep. Bounded so the first acquire after a fleet restart
#: does not rewrite every expired row while holding the write lock. See the module
#: docstring: this is a GC bound, not a correctness bound.
REAP_LIMIT = 500

_LOCK_COLUMNS = (
    "id",
    "realm",
    "rel_path",
    "depth",
    "kind",
    "holder_kind",
    "holder_id",
    "holder_generation",
    "lane",
    "owner_group",
    "purpose",
    "acquired_at",
    "expires_at",
)

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ScopeLockError(RuntimeError):
    """Base class for scope-lock failures."""


class ScopeLockBusy(ScopeLockError):
    """A claim was refused because something else holds an overlapping path.

    Carries the :class:`AcquireResult` so the caller can park a waiter pointing at
    the blocker it was actually told about (``result.blocked_on``) rather than
    re-deriving one and pointing at a different, equally-valid lock.
    """

    def __init__(self, result: AcquireResult) -> None:
        self.result = result
        conflict = result.conflict
        detail = conflict.describe() if conflict is not None else "scope unavailable"
        super().__init__(detail)


class ScopeLockFenced(ScopeLockError):
    """The caller's generation is older than one already recorded for its identity.

    It has been adopted away. It must stop, not retry: a retry at the same
    generation will be refused again, and a retry at a bumped generation would be
    the wedged worker stealing its adopter's locks back.
    """


class _WriterStore(Protocol):
    """The slice of :class:`omniagentos.db.store.SqliteStore` this module uses.

    Structural, not a real import, so ``omniagentos.scope`` keeps sitting UNDER
    ``omniagentos.db`` in the import graph exactly as the package docstring
    promises. Reaching through to ``_lock``/``_connection`` is the established
    idiom for a DAL that must share the Phase-2 writer serialization (the store's
    own docstring notes sixteen such call sites); ``_begin`` even asserts the
    writer lock is held, which turns a mistake here into a named failure.
    """

    @property
    def _lock(self) -> Any: ...  # noqa: D105
    @property
    def _read_lock(self) -> Any: ...  # noqa: D105
    @property
    def _connection(self) -> sqlite3.Connection: ...  # noqa: D105
    @property
    def _shared_connection(self) -> sqlite3.Connection | None: ...  # noqa: D105
    def _begin(self) -> None: ...  # noqa: D105
    def _commit(self) -> None: ...  # noqa: D105
    def _rollback(self) -> None: ...  # noqa: D105


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockHolder:
    """WHO is claiming: an identity plus the lane it executes in.

    ``generation`` is NOT part of this: it changes on adoption while the identity
    does not, and every fenced call takes it as an explicit argument so that
    passing a stale one is a visible act rather than a stale field nobody
    noticed.
    """

    kind: HolderKind
    id: str
    lane: Lane

    def __post_init__(self) -> None:
        if self.kind not in HOLDER_KINDS:
            raise ScopeLockError(f"unknown holder kind: {self.kind!r}")
        if self.lane not in LANES:
            raise ScopeLockError(f"unknown lane: {self.lane!r}")
        if not self.id or not str(self.id).strip():
            raise ScopeLockError("lock holder requires an id")


@dataclass(frozen=True, slots=True)
class HeldLock:
    """One live row of ``resource_locks``."""

    id: str
    realm: str
    rel_path: str
    depth: int
    kind: ScopeKind
    holder_kind: str
    holder_id: str
    holder_generation: int
    lane: str
    owner_group: str
    purpose: str
    acquired_at: str
    expires_at: str

    def as_claim(self) -> ScopeClaim:
        """The :class:`ScopeClaim` this row represents, carrying its lock id.

        ``lock_id`` matters: :func:`~omniagentos.scope.conflict.first_conflict`
        orders blockers by ``(depth, lock_id, path, kind)``, so two racing
        claimants only report the SAME blocker because the id travels with the
        claim.
        """
        return ScopeClaim(
            realm=self.realm,
            components=normalize_rel(self.rel_path),
            kind=self.kind,
            owner_group=self.owner_group,
            purpose=self.purpose,
            lock_id=self.id,
        )

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The tuple the partial unique index is built on."""
        return (self.realm, self.rel_path, self.kind, self.purpose)

    @property
    def holder(self) -> tuple[str, str]:
        """The holder identity, for "is this mine" comparisons."""
        return (self.holder_kind, self.holder_id)


@dataclass(frozen=True, slots=True)
class Waiter:
    """One live row of ``scope_waiters``."""

    id: int
    realm: str
    lane: str
    holder_kind: str
    holder_id: str
    blocked_on: str
    blocked_path: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """The outcome of one all-or-nothing acquire.

    ``conflict`` is populated whenever a collision was OBSERVED, which includes
    the shadow-mode case where the claim was granted anyway — that observation is
    the entire product of a shadow soak, so it must survive into the result even
    though it changed no decision.
    """

    status: AcquireStatus
    mode: ScopeLockMode
    lock_ids: tuple[str, ...] = ()
    conflict: ScopeConflict | None = None
    shadowed: bool = False
    skipped_duplicates: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        """May the caller proceed? True for ``granted`` and for ``disabled``."""
        return self.status in ("granted", "disabled")

    @property
    def blocked_on(self) -> str:
        """The observed blocker's lock id, or ``""``. Park a waiter pointing here."""
        return self.conflict.held.lock_id if self.conflict is not None else ""

    @property
    def blocked_path(self) -> str:
        """Realm-relative text of the claim that could not be granted."""
        return self.conflict.candidate.path_text if self.conflict is not None else ""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_iso(now: str | datetime | None) -> str:
    """Canonical UTC ISO stamp; tests pass an explicit ``now`` to control TTLs.

    Second granularity, matching ``contracts.utc_now_iso`` and therefore every
    other timestamp column in this database. The format is fixed-width, so the
    ``expires_at > now`` string comparisons in this module are correct ordinal
    comparisons without any parsing in SQL.
    """
    if now is None:
        return utc_now_iso()
    if isinstance(now, datetime):
        moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime(_ISO_FORMAT)
    return str(now)


def _parse_iso(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ScopeLockError(f"unparseable timestamp: {text!r}") from exc


def _plus_seconds(now_iso: str, seconds: float) -> str:
    return (_parse_iso(now_iso) + timedelta(seconds=seconds)).strftime(_ISO_FORMAT)


# ---------------------------------------------------------------------------
# Claim <-> row translation
# ---------------------------------------------------------------------------


def _stored_purpose(purpose: str) -> StoredPurpose:
    """Classify an open-string ``ScopeClaim.purpose`` into the column's two values.

    ``ScopeClaim.purpose`` is deliberately an open string (see
    :mod:`omniagentos.scope.model`) so later work packages can add intents without
    touching that module — but the conflict rules special-case exactly ONE value,
    ``COMMIT_PURPOSE``, and nothing reads the free text. The column therefore
    stores the two-valued classification the rules actually consume, and the
    CHECK constraint keeps it honest.

    The projection is lossless where it matters and lossy only in the free text:
    a lock read back carries ``'work'`` or ``'commit'``, which is precisely what
    :func:`~omniagentos.scope.conflict.conflicts_with` needs.
    """
    return COMMIT_PURPOSE if purpose == COMMIT_PURPOSE else WORK_PURPOSE  # type: ignore[return-value]


def _claim_key(claim: ScopeClaim) -> tuple[str, str, str, str]:
    """The unique-index tuple for a candidate claim."""
    return (claim.realm, claim.path_text, claim.kind, _stored_purpose(claim.purpose))


def _order_key(claim: ScopeClaim) -> tuple[str, int, str, str, str]:
    """Total order over a claim set, so a refusal is reproducible.

    Broadest-first within a realm (depth ascending) mirrors the blocker ordering
    in :mod:`omniagentos.scope.conflict`: two processes submitting the same claim
    set must evaluate it in the same order and therefore report the same blocker,
    or a waiter queue built on ``blocked_on`` thrashes between equally-valid
    answers.
    """
    return (claim.realm, claim.depth, claim.path_text, claim.kind, claim.purpose)


def _held_from_row(row: sqlite3.Row) -> HeldLock:
    return HeldLock(
        id=str(row["id"]),
        realm=str(row["realm"]),
        rel_path=str(row["rel_path"]),
        depth=int(row["depth"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        holder_kind=str(row["holder_kind"]),
        holder_id=str(row["holder_id"]),
        holder_generation=int(row["holder_generation"]),
        lane=str(row["lane"]),
        owner_group=str(row["owner_group"] or ""),
        purpose=str(row["purpose"]),
        acquired_at=str(row["acquired_at"]),
        expires_at=str(row["expires_at"]),
    )


def _waiter_from_row(row: sqlite3.Row) -> Waiter:
    return Waiter(
        id=int(row["id"]),
        realm=str(row["realm"]),
        lane=str(row["lane"]),
        holder_kind=str(row["holder_kind"]),
        holder_id=str(row["holder_id"]),
        blocked_on=str(row["blocked_on"] or ""),
        blocked_path=str(row["blocked_path"] or ""),
        created_at=str(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class _ScopeBlocked(Exception):
    """Internal signal: a conflict was found INSIDE the acquire transaction.

    Exists so the blocked path unwinds `_write` through its except-branch (a real
    rollback) instead of `return`ing, which a @contextmanager treats as a clean
    exit and therefore COMMITS. Never escapes this module -- `_acquire` catches it
    and returns the ordinary blocked AcquireResult.
    """

    def __init__(self, conflict: ScopeConflict) -> None:
        super().__init__("scope blocked")
        self.conflict = conflict


class PathLockStore:
    """Durable cross-lane path locks over an existing :class:`SqliteStore`.

    Construct it around the store the lane already owns — it deliberately does
    NOT open its own connection. Phase 2 serialized every writer behind one
    process-wide lock precisely so that two DALs in one process cannot open two
    overlapping transactions on two connections; a private connection here would
    opt straight back out of that guarantee.
    """

    def __init__(self, store: _WriterStore) -> None:
        self._store = store

    # --- connection plumbing ------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` under the store's writer lock.

        Commits on clean exit, rolls back on ANY exception (including
        ``KeyboardInterrupt`` — ``BaseException``, matching every other DAL here,
        because a Ctrl-C mid-transaction must not leave a half-written claim set).
        """
        store = self._store
        with store._lock:
            store._begin()
            try:
                yield store._connection
            except BaseException:
                store._rollback()
                raise
            store._commit()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A pure-SELECT connection, mirroring ``SqliteStore._reader`` exactly.

        Readers take the store's separate ``_read_lock`` so they are never delayed
        by a writer sitting inside ``BEGIN IMMEDIATE``; a ``:memory:`` store has
        no per-thread connections to spread over and keeps taking the writer lock,
        exactly as ``_reader`` does. The ``getattr`` fallbacks keep this working
        against a store that predates the Phase-2 split rather than failing with
        an AttributeError from inside a read.
        """
        store = self._store
        shared = getattr(store, "_shared_connection", None)
        read_lock = getattr(store, "_read_lock", None)
        guard = read_lock if (shared is None and read_lock is not None) else store._lock
        with guard:
            yield store._connection

    # --- reaping ------------------------------------------------------------

    @staticmethod
    def _reap_locked(conn: sqlite3.Connection, now_iso: str, limit: int) -> int:
        """The bounded lazy reap. Caller is inside the transaction.

        The ``id IN (SELECT ... LIMIT n)`` subselect is MANDATORY and not a
        stylistic choice: plain ``UPDATE ... LIMIT`` requires SQLite to have been
        compiled with ``SQLITE_ENABLE_UPDATE_DELETE_LIMIT``, which the stdlib
        ``sqlite3`` build is not guaranteed to have. Writing it the portable way
        means the bound exists on every host rather than on the ones that happen
        to have the option.

        Oldest-first (``ORDER BY expires_at``) so repeated bounded sweeps drain
        the backlog in a defined order instead of revisiting an arbitrary slice.
        """
        cursor = conn.execute(
            "UPDATE resource_locks SET released_at = :now "
            "WHERE released_at IS NULL AND expires_at <= :now "
            "AND id IN (SELECT id FROM resource_locks "
            "           WHERE released_at IS NULL AND expires_at <= :now "
            "           ORDER BY expires_at ASC LIMIT :limit)",
            {"now": now_iso, "limit": int(limit)},
        )
        return int(cursor.rowcount or 0)

    def reap_expired(self, now: str | datetime | None = None, limit: int = REAP_LIMIT) -> int:
        """Release up to ``limit`` expired locks; returns how many were released.

        Callable on its own (a maintenance tick), but never REQUIRED for
        correctness — the acquire path reaps lazily and, more importantly, filters
        expired rows out of its live read regardless of whether any reap has run.
        """
        now_iso = _now_iso(now)
        with self._write() as conn:
            return self._reap_locked(conn, now_iso, limit)

    # --- reads --------------------------------------------------------------

    def held_in_realm(self, realm: str, *, now: str | datetime | None = None) -> list[HeldLock]:
        """Every LIVE lock in ``realm``, broadest-first.

        "Live" means not released AND not expired. Filtering on ``expires_at``
        here — not only in the reaper — is what lets the reaper be bounded: a lock
        the sweep has not reached yet is already invisible to every decision.
        """
        now_iso = _now_iso(now)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_LOCK_COLUMNS)} FROM resource_locks "
                "WHERE realm = ? AND released_at IS NULL AND expires_at > ? "
                "ORDER BY depth ASC, id ASC",
                (realm, now_iso),
            ).fetchall()
        return [_held_from_row(row) for row in rows]

    def held_by(
        self,
        holder_kind: str,
        holder_id: str,
        *,
        now: str | datetime | None = None,
    ) -> list[HeldLock]:
        """Every LIVE lock held by one identity (crash-resume + renew inspection)."""
        now_iso = _now_iso(now)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_LOCK_COLUMNS)} FROM resource_locks "
                "WHERE holder_kind = ? AND holder_id = ? "
                "AND released_at IS NULL AND expires_at > ? "
                "ORDER BY realm ASC, depth ASC, id ASC",
                (holder_kind, holder_id, now_iso),
            ).fetchall()
        return [_held_from_row(row) for row in rows]

    # --- acquire ------------------------------------------------------------

    def try_acquire_scope(
        self,
        claims: Iterable[ScopeClaim],
        holder: LockHolder,
        *,
        generation: int = 0,
        ttl_s: float | None = None,
        enforce: bool | None = None,
        owner_group: str | None = None,
        now: str | datetime | None = None,
    ) -> AcquireResult:
        """Take the WHOLE claim set, or none of it. Never blocks, never waits.

        One ``BEGIN IMMEDIATE`` covers: bounded reap, the realm's live read, the
        conflict scan over every candidate, and the inserts. There is no point at
        which this method holds a lock and waits for another, which is the whole
        safety argument (see the module docstring).

        ``enforce`` resolves the three-way config mode:

        * ``None`` (the default) — read :func:`scope_locks_mode`. Mode ``off``
          returns immediately with ``status='disabled'`` and writes NOTHING, so a
          lane may wire this in before anyone has decided to turn it on.
        * ``True`` — refuse on conflict.
        * ``False`` — SHADOW: insert the rows anyway and report the conflict
          without refusing. Note that this is not "off"; passing ``False`` still
          writes, because a shadow that writes no rows measures a hypothetical.

        Returns an :class:`AcquireResult`; it does not raise on refusal, because
        the caller's next move (park a waiter) needs the blocker in hand.
        """
        mode: ScopeLockMode
        if enforce is None:
            mode = scope_locks_mode()
        else:
            mode = "enforce" if enforce else "shadow"
        if mode == "off":
            return AcquireResult(status="disabled", mode=mode)
        return self._acquire(
            claims,
            holder,
            generation=generation,
            ttl_s=ttl_s,
            mode=mode,
            owner_group=owner_group,
            now=now,
            replace=False,
        )

    def reacquire_scope(
        self,
        claims: Iterable[ScopeClaim],
        holder: LockHolder,
        *,
        generation: int = 0,
        ttl_s: float | None = None,
        enforce: bool | None = None,
        owner_group: str | None = None,
        now: str | datetime | None = None,
    ) -> AcquireResult:
        """Crash-resume: REPLACE this holder's scope with ``claims``, atomically.

        The difference from :meth:`try_acquire_scope` is that this holder's
        existing live locks are released FIRST, inside the same transaction, so a
        resumed run ends up holding exactly the set it re-declared rather than the
        union of that and whatever its dead incarnation had taken. A resume that
        narrows its scope must actually give the surrendered paths back.

        Still all-or-nothing: if the new set cannot be granted, the rollback
        restores the old locks untouched, so a failed resume never strands a run
        with a partially surrendered scope.

        Fencing still applies. A wedged worker cannot use this to reclaim what its
        adopter took: the generation check refuses anything older than the
        generation already recorded for this identity.
        """
        mode: ScopeLockMode
        if enforce is None:
            mode = scope_locks_mode()
        else:
            mode = "enforce" if enforce else "shadow"
        if mode == "off":
            return AcquireResult(status="disabled", mode=mode)
        return self._acquire(
            claims,
            holder,
            generation=generation,
            ttl_s=ttl_s,
            mode=mode,
            owner_group=owner_group,
            now=now,
            replace=True,
        )

    def _acquire(
        self,
        claims: Iterable[ScopeClaim],
        holder: LockHolder,
        *,
        generation: int,
        ttl_s: float | None,
        mode: ScopeLockMode,
        owner_group: str | None,
        now: str | datetime | None,
        replace: bool,
    ) -> AcquireResult:
        try:
            return self._acquire_locked(
                claims,
                holder,
                generation=generation,
                ttl_s=ttl_s,
                mode=mode,
                owner_group=owner_group,
                now=now,
                replace=replace,
            )
        except _ScopeBlocked as signal:
            # The transaction rolled back; report the conflict normally.
            return AcquireResult(status="blocked", mode=mode, conflict=signal.conflict)

    def _acquire_locked(
        self,
        claims: Iterable[ScopeClaim],
        holder: LockHolder,
        *,
        generation: int,
        ttl_s: float | None,
        mode: ScopeLockMode,
        owner_group: str | None,
        now: str | datetime | None,
        replace: bool,
    ) -> AcquireResult:
        candidates = self._prepare(claims, owner_group)
        now_iso = _now_iso(now)
        ttl = scope_ttl_s() if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            raise ScopeLockError(f"scope lock ttl must be positive: {ttl_s!r}")
        expires_at = _plus_seconds(now_iso, ttl)
        generation = int(generation)

        with self._write() as conn:
            self._reap_locked(conn, now_iso, REAP_LIMIT)

            mine = self._own_locks_locked(conn, holder, now_iso)
            # Fence BEFORE anything else: a holder identity that already carries a
            # HIGHER generation has been adopted away, and this caller is the
            # displaced one. Refuse rather than let it re-enter the fleet.
            # Fence against the HIGHEST generation this identity has EVER carried,
            # not just the one on its currently-live rows.
            #
            # `mine` filters `released_at IS NULL AND expires_at > now`, so reading
            # the fence from it made the fence evaporate the moment the adopter's
            # locks were released or lapsed: a displaced worker at generation 0
            # could then re-enter the fleet alongside its adopter. Reproduced
            # deterministically -- acquire at gen 5, release cleanly, re-acquire at
            # gen 0 -> granted rather than fenced.
            #
            # Rows are never DELETEd (release and reap both set released_at), so
            # the historical maximum is durable in the table itself and needs no
            # extra state. This is the whole point of migration 052's generation
            # bump: a wedged worker must be told to stop, not silently ignored.
            highest = conn.execute(
                "SELECT MAX(holder_generation) FROM resource_locks "
                "WHERE holder_kind = ? AND holder_id = ?",
                (holder.kind, holder.id),
            ).fetchone()[0]
            if highest is not None and highest > generation:
                return AcquireResult(status="fenced", mode=mode)

            if replace and mine:
                conn.execute(
                    "UPDATE resource_locks SET released_at = ? "
                    "WHERE holder_kind = ? AND holder_id = ? AND released_at IS NULL",
                    (now_iso, holder.kind, holder.id),
                )
                mine = []

            own_by_key = {lock.key: lock for lock in mine}
            foreign_by_realm = self._live_by_realm_locked(
                conn, {claim.realm for claim in candidates}, holder, now_iso
            )
            claims_by_realm = {
                realm: [lock.as_claim() for lock in locks]
                for realm, locks in foreign_by_realm.items()
            }
            foreign_keys = {lock.key for locks in foreign_by_realm.values() for lock in locks}

            # The decision, over the WHOLE set, before a single insert. Doing it
            # this way (rather than inserting as we go and unwinding) is what makes
            # all-or-nothing structural; the rollback in _write is the second line
            # of defense, not the first.
            conflict: ScopeConflict | None = None
            for claim in candidates:
                found = first_conflict(claim, claims_by_realm.get(claim.realm, ()))
                if found is not None:
                    conflict = found
                    break
            if conflict is not None and mode == "enforce":
                # ROLLBACK EXPLICITLY -- do not `return` from inside `_write`.
                #
                # `return` is a NORMAL exit from a @contextmanager, so `_write`
                # would fall through to store._commit() and PERMANENTLY COMMIT the
                # replace-release above. A blocked reacquire would then strand the
                # holder having surrendered its entire previous scope: exactly the
                # opposite of the all-or-nothing guarantee this method documents,
                # and worst on the crash-resume path where the holder has real
                # in-progress work behind those locks.
                #
                # Raising _ScopeBlocked unwinds `_write`'s except-path (a genuine
                # rollback), and the caller below converts it back into the
                # ordinary blocked result. Both review lenses found this
                # independently; the reproducer is a single-process,
                # timing-independent three-step sequence.
                raise _ScopeBlocked(conflict)

            lock_ids: list[str] = []
            skipped: list[str] = []
            for claim in candidates:
                key = _claim_key(claim)
                own = own_by_key.get(key)
                if own is not None:
                    # Already ours: extend it and adopt the current generation
                    # rather than inserting a second row (which the partial unique
                    # index would reject). This is what makes a retried acquire
                    # idempotent — a crash between COMMIT and the caller recording
                    # the result is recoverable by simply calling again.
                    conn.execute(
                        "UPDATE resource_locks SET expires_at = ?, holder_generation = ? "
                        "WHERE id = ? AND released_at IS NULL",
                        (expires_at, generation, own.id),
                    )
                    lock_ids.append(own.id)
                    continue
                if key in foreign_keys:
                    # Shadow mode only (enforce would have refused above): somebody
                    # else holds this EXACT path. The row cannot be written without
                    # violating the partial unique index, and weakening that index
                    # to allow it would destroy the invariant it exists to express.
                    # The contention is still measured — it is in `conflict`.
                    skipped.append(claim.path_text)
                    continue
                lock_ids.append(
                    self._insert_lock(
                        conn,
                        claim,
                        holder,
                        generation=generation,
                        acquired_at=now_iso,
                        expires_at=expires_at,
                    )
                )

            return AcquireResult(
                status="granted",
                mode=mode,
                lock_ids=tuple(lock_ids),
                conflict=conflict,
                shadowed=conflict is not None,
                skipped_duplicates=tuple(skipped),
            )

    def _prepare(self, claims: Iterable[ScopeClaim], owner_group: str | None) -> list[ScopeClaim]:
        """Normalize, de-duplicate and totally order a claim set.

        De-duplication is by the unique-index key, so a caller that declares both
        ``src/a`` and ``./src/a`` does not try to insert the same row twice and
        trip its own backstop. Overlapping-but-distinct claims from ONE holder
        (``src/**`` plus ``src/a.py``) are left alone: they are the same owner, so
        they do not exclude each other and both rows are meaningful.
        """
        prepared: dict[tuple[str, str, str, str], ScopeClaim] = {}
        for claim in claims:
            if not isinstance(claim, ScopeClaim):
                raise ScopeLockError(f"not a ScopeClaim: {claim!r}")
            resolved = claim if owner_group is None else _with_owner(claim, owner_group)
            prepared.setdefault(_claim_key(resolved), resolved)
        return sorted(prepared.values(), key=_order_key)

    def _own_locks_locked(
        self, conn: sqlite3.Connection, holder: LockHolder, now_iso: str
    ) -> list[HeldLock]:
        rows = conn.execute(
            f"SELECT {', '.join(_LOCK_COLUMNS)} FROM resource_locks "
            "WHERE holder_kind = ? AND holder_id = ? "
            "AND released_at IS NULL AND expires_at > ?",
            (holder.kind, holder.id, now_iso),
        ).fetchall()
        return [_held_from_row(row) for row in rows]

    def _live_by_realm_locked(
        self,
        conn: sqlite3.Connection,
        realms: Iterable[str],
        holder: LockHolder,
        now_iso: str,
    ) -> dict[str, list[HeldLock]]:
        """Live FOREIGN locks, per realm, broadest-first.

        The holder's OWN locks are excluded: a holder cannot conflict with itself,
        and including them would make every renew-shaped acquire refuse itself.
        Ownership is the identity pair, NOT ``owner_group`` — ``owner_group`` is
        the (near-dead, see ``conflict._same_owner_relaxed``) co-editing
        relaxation and deliberately does not confer self-exclusion.
        """
        by_realm: dict[str, list[HeldLock]] = {}
        for realm in realms:
            rows = conn.execute(
                f"SELECT {', '.join(_LOCK_COLUMNS)} FROM resource_locks "
                "WHERE realm = ? AND released_at IS NULL AND expires_at > ? "
                "ORDER BY depth ASC, id ASC",
                (realm, now_iso),
            ).fetchall()
            by_realm[realm] = [
                lock
                for lock in (_held_from_row(row) for row in rows)
                if lock.holder != (holder.kind, holder.id)
            ]
        return by_realm

    @staticmethod
    def _insert_lock(
        conn: sqlite3.Connection,
        claim: ScopeClaim,
        holder: LockHolder,
        *,
        generation: int,
        acquired_at: str,
        expires_at: str,
    ) -> str:
        """Insert ONE lock row and return its id.

        Isolated as its own method so ``test_no_partial_acquire`` has a stable
        seam to fail at part-way through a multi-claim acquire. That test is the
        proof of the all-or-nothing property; it needs somewhere honest to break.
        """
        lock_id = new_id("lck")
        conn.execute(
            "INSERT INTO resource_locks "
            "(id, realm, rel_path, depth, kind, holder_kind, holder_id, "
            " holder_generation, lane, owner_group, purpose, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lock_id,
                claim.realm,
                claim.path_text,
                claim.depth,
                claim.kind,
                holder.kind,
                holder.id,
                generation,
                holder.lane,
                claim.owner_group,
                _stored_purpose(claim.purpose),
                acquired_at,
                expires_at,
            ),
        )
        return lock_id

    # --- renew / release ----------------------------------------------------

    def renew_scope(
        self,
        holder_kind: str,
        holder_id: str,
        generation: int,
        ttl_s: float | None = None,
        *,
        now: str | datetime | None = None,
    ) -> bool:
        """Extend every live lock of one holder. Generation-FENCED.

        Returns True only when at least one lock was extended. False means one of
        two things and the caller must treat both the same way — stop working:
        the holder was adopted away (generation mismatch), or its lease already
        lapsed.

        ALREADY-EXPIRED locks are deliberately NOT renewable. Renewing one would
        resurrect a claim that had logically lapsed and that another holder may
        already have legitimately acquired, which is the one way a lock table can
        hand the same path to two writers. A lapsed holder must go back through
        :meth:`try_acquire_scope` and find out.
        """
        now_iso = _now_iso(now)
        ttl = scope_ttl_s() if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            raise ScopeLockError(f"scope lock ttl must be positive: {ttl_s!r}")
        expires_at = _plus_seconds(now_iso, ttl)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE resource_locks SET expires_at = ? "
                "WHERE holder_kind = ? AND holder_id = ? AND holder_generation = ? "
                "AND released_at IS NULL AND expires_at > ?",
                (expires_at, holder_kind, holder_id, int(generation), now_iso),
            )
            return int(cursor.rowcount or 0) > 0

    def release_scope(
        self,
        holder_kind: str,
        holder_id: str,
        generation: int,
        *,
        lock_ids: Sequence[str] | None = None,
        now: str | datetime | None = None,
    ) -> int:
        """Release this holder's locks; returns how many rows were released.

        GENERATION-FENCED, and that is the whole point of the argument: after an
        adoption bumps the generation, the displaced worker's cleanup path runs
        with its OLD generation and must not free the locks its adopter is now
        working under. A stale release matches zero rows and returns 0 — which is
        also the signal to the caller that it has been displaced.

        ``lock_ids`` narrows the release to specific rows, which is what
        :meth:`commit_lock` uses so that dropping a commit lock does not also drop
        the work locks the same holder took earlier.
        """
        now_iso = _now_iso(now)
        sql = (
            "UPDATE resource_locks SET released_at = ? "
            "WHERE holder_kind = ? AND holder_id = ? AND holder_generation = ? "
            "AND released_at IS NULL"
        )
        params: list[Any] = [now_iso, holder_kind, holder_id, int(generation)]
        if lock_ids is not None:
            ids = list(lock_ids)
            if not ids:
                return 0
            sql += f" AND id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)
        with self._write() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.rowcount or 0)

    # --- commit lock --------------------------------------------------------

    @contextmanager
    def commit_lock(
        self,
        realm: str,
        holder: LockHolder,
        *,
        generation: int = 0,
        ttl_s: float | None = None,
        enforce: bool | None = None,
        owner_group: str = "",
        now: str | datetime | None = None,
    ) -> Iterator[AcquireResult]:
        """Hold the WHOLE realm for the duration of a commit.

        A commit rewrites the index and moves HEAD; it is realm-wide in effect
        whatever files it names, so the claim is the whole-realm root
        (``components == ()``) with ``purpose='commit'`` — the one purpose the
        co-ownership relaxation never relaxes.

        Raises :class:`ScopeLockBusy` when refused (enforce mode) so a caller
        cannot accidentally commit through a ``with`` block that did not actually
        get the lock; in shadow mode it yields a result whose ``conflict`` records
        what enforcement WOULD have refused.

        Releases by lock id in a ``finally``, so a commit lock taken by a holder
        that also holds work locks drops only the commit lock.
        """
        claim = ScopeClaim(
            realm=realm,
            components=(),
            kind="root",
            owner_group=owner_group,
            purpose=COMMIT_PURPOSE,
        )
        result = self.try_acquire_scope(
            [claim],
            holder,
            generation=generation,
            ttl_s=ttl_s,
            enforce=enforce,
            now=now,
        )
        if result.status == "fenced":
            raise ScopeLockFenced(
                f"{holder.kind}:{holder.id} generation {generation} has been adopted away"
            )
        if not result.granted:
            raise ScopeLockBusy(result)
        try:
            yield result
        finally:
            if result.lock_ids:
                self.release_scope(holder.kind, holder.id, generation, lock_ids=result.lock_ids)

    # --- waiters ------------------------------------------------------------

    def park_waiter(
        self,
        realm: str,
        holder: LockHolder,
        *,
        blocked_on: str = "",
        blocked_path: str = "",
        now: str | datetime | None = None,
    ) -> int:
        """Park this holder at the BACK of the realm's FIFO queue; returns its id.

        Idempotent, and the idempotence is load-bearing. A holder that retries
        every few seconds calls this every time; if each call inserted a new row
        it would keep re-queueing itself at the back and starve forever. Re-parking
        an already-parked holder UPDATES the diagnostic pointers and KEEPS the
        original id and ``created_at``, so its queue position is preserved. The
        partial unique index on ``(holder_kind, holder_id) WHERE cleared_at IS
        NULL`` makes the alternative impossible rather than merely discouraged.
        """
        now_iso = _now_iso(now)
        with self._write() as conn:
            existing = conn.execute(
                "SELECT id FROM scope_waiters "
                "WHERE holder_kind = ? AND holder_id = ? AND cleared_at IS NULL",
                (holder.kind, holder.id),
            ).fetchone()
            if existing is not None:
                waiter_id = int(existing["id"])
                conn.execute(
                    "UPDATE scope_waiters SET realm = ?, lane = ?, blocked_on = ?, "
                    "blocked_path = ? WHERE id = ?",
                    (realm, holder.lane, blocked_on, blocked_path, waiter_id),
                )
                return waiter_id
            cursor = conn.execute(
                "INSERT INTO scope_waiters "
                "(realm, lane, holder_kind, holder_id, blocked_on, blocked_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    realm,
                    holder.lane,
                    holder.kind,
                    holder.id,
                    blocked_on,
                    blocked_path,
                    now_iso,
                ),
            )
            return int(cursor.lastrowid or 0)

    def next_waiter(self, realm: str) -> Waiter | None:
        """The oldest live waiter in ``realm``, or ``None``. A PEEK, not a claim.

        Mirrors ``LonghaulStore.next_waiting_in_category``: the caller wakes the
        holder, the holder re-runs the whole all-or-nothing acquire, and only a
        holder that actually gets its scope calls :meth:`clear_waiter`. Nothing is
        handed over here — see the module docstring on why there is no handoff.

        Ordered by the autoincrement id, i.e. strict insertion order.
        ``created_at`` is second-granularity so simultaneous parks tie under it,
        and ordering by a timestamp first would also let a wall-clock step
        backwards reorder the queue. Arrival order is the definition of FIFO;
        the id is the only thing that records it exactly.
        """
        with self._read() as conn:
            row = conn.execute(
                "SELECT id, realm, lane, holder_kind, holder_id, blocked_on, "
                "blocked_path, created_at FROM scope_waiters "
                "WHERE realm = ? AND cleared_at IS NULL ORDER BY id ASC LIMIT 1",
                (realm,),
            ).fetchone()
        return None if row is None else _waiter_from_row(row)

    def waiting_in_realm(self, realm: str) -> list[Waiter]:
        """Every live waiter in ``realm``, in FIFO order (queue depth / drills)."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id, realm, lane, holder_kind, holder_id, blocked_on, "
                "blocked_path, created_at FROM scope_waiters "
                "WHERE realm = ? AND cleared_at IS NULL ORDER BY id ASC",
                (realm,),
            ).fetchall()
        return [_waiter_from_row(row) for row in rows]

    def clear_waiter(
        self,
        holder_kind: str,
        holder_id: str,
        *,
        now: str | datetime | None = None,
    ) -> int:
        """Clear this holder's park row; returns how many rows were cleared (0 or 1).

        Not generation-fenced, deliberately: a waiter row grants NOTHING, so
        clearing someone's park by mistake costs at most one extra wake, whereas a
        fence here would leave orphan rows blocking the holder's own future parks
        through the unique index.
        """
        now_iso = _now_iso(now)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE scope_waiters SET cleared_at = ? "
                "WHERE holder_kind = ? AND holder_id = ? AND cleared_at IS NULL",
                (now_iso, holder_kind, holder_id),
            )
            return int(cursor.rowcount or 0)


def _with_owner(claim: ScopeClaim, owner_group: str) -> ScopeClaim:
    """``claim`` with ``owner_group`` applied (claims are frozen)."""
    if claim.owner_group == owner_group:
        return claim
    return ScopeClaim(
        realm=claim.realm,
        components=claim.components,
        kind=claim.kind,
        owner_group=owner_group,
        purpose=claim.purpose,
        lock_id=claim.lock_id,
    )
