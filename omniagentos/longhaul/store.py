"""LonghaulStore: DAL for longhaul task categories, sessions, and state.

All writes use BEGIN IMMEDIATE for atomicity (repo idiom).

SCOPE LOCKS (T3.3)
==================
:meth:`LonghaulStore.open_attempt` is this lane's durable dispatch boundary — it
already takes ONE ``BEGIN IMMEDIATE`` and already refuses a second live attempt
through ``idx_task_sessions_live`` — so it is also where the lane declares what
it is about to write to the rest of the fleet. See
:meth:`LonghaulStore._acquire_attempt_scope` for the two decisions that shape the
wiring: WHY the acquire sits just outside open_attempt's transaction rather than
inside it, and WHY longhaul's v1 claim is the whole realm.

PER-BOARD-TASK WORKTREES (T3.8)
===============================
:meth:`LonghaulStore.working_dir_for` is where this lane stops serializing a
project. It activates ``var/longhaul/worktrees/<board_task_id>/main`` on branch
``longhaul/<board_task_id>/main`` and hands back the directory the executor
should run in; because that directory is its own realm (see
``worktrees.lanes``), the whole-realm claim below stops colliding with every
other lane in the project and becomes a claim nobody else can name.

THE UNIT IS THE BOARD TASK, NOT THE ATTEMPT, and that is the one thing about
this lane that must not be "simplified". Longhaul is a CHAIN: an attempt ends,
``close_attempt`` fires, the engine opens a successor with a continuation prompt
and the relayed workbook, and the successor is meant to pick the work up exactly
where the last one put it down. A per-ATTEMPT worktree would hand attempt N+1 a
pristine checkout and silently discard everything attempt N had not committed.
``SubprocessWorktrees.create`` already has exactly the right semantics — it
reuses a still-registered worktree AS-IS, and re-attaches an existing branch AT
ITS TIP — so the chain keeps its tree and ``close_attempt`` deliberately does NOT
remove anything. The worktree is merged and removed once, when the TASK is done.

Everything here ships dark: ``omniagentos.scope.config.scope_locks_mode()``
defaults to ``off`` and ``OMNIAGENTOS_WORKTREES_LONGHAUL`` defaults to 0, and
with both off this module does not resolve a realm, does not construct a lock
store, does not shell out to git and writes no lock rows — byte-for-byte what it
did before the wiring landed (``tests/longhaul/test_scope_wiring.py``,
``tests/worktrees/test_lane_wiring.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import weakref
from collections.abc import Callable
from functools import wraps
from threading import RLock, local
from typing import Any, TypedDict, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import _connect
from omniagentos.scope.config import scope_locks_enabled
from omniagentos.scope.locks import AcquireResult, LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.scope.paths import realm_of
from omniagentos.worktrees.lanes import (
    WORKTREE_RECORD_KEY,
    Integration,
    LaneWorktree,
    LaneWorktrees,
    lane_worktrees,
    lane_worktrees_enabled,
    worktree_from_record,
)

LOG = logging.getLogger(__name__)

#: The ``worktrees.lanes`` binding this lane uses. The unit is the BOARD TASK —
#: see the module docstring on why an attempt-keyed worktree would throw away
#: every attempt's uncommitted work at the next link in the chain.
WORKTREE_LANE = "longhaul"

#: The ``board_tasks.park_state`` value a scope-blocked longhaul task parks in.
#:
#: IT IS NOT ``'waiting_scope'``, AND THAT IS NOT A PREFERENCE. Migration 043
#: pinned the column to ``CHECK (park_state IN ('waiting_category',
#: 'waiting_capacity', 'waiting_review'))``; SQLite cannot widen a CHECK without
#: rebuilding the table, this work package is forbidden from adding a migration,
#: and writing ``'waiting_scope'`` therefore raises IntegrityError out of
#: ``LonghaulEngine._transition`` — i.e. it would convert "this lane parks
#: politely" into "this lane crashes dispatch", in enforce mode only, which is
#: the worst possible place to find out.
#:
#: ``'waiting_capacity'`` is the honest existing spelling: it already means "this
#: task owns its category slot and is in_progress, but cannot execute right now",
#: which is exactly the state a scope-blocked attempt is in. The *reason* is
#: carried in ``longhaul_json.park_reason`` (:data:`SCOPE_PARK_REASON`) and in
#: the ``longhaul.waiting_scope`` event, so nothing about the diagnosis is lost.
#:
#: FOLLOW-UP: a later migration should rebuild ``board_tasks`` with
#: ``'waiting_scope'`` in the CHECK; this constant is the single place to
#: re-point when it does.
SCOPE_PARK_STATE = "waiting_capacity"

#: ``longhaul_json.park_reason`` marking a park as scope contention rather than
#: worker-capacity exhaustion. The discriminator :meth:`next_waiting_scope`
#: reads, and the reason the shared park_state value above is not ambiguous.
SCOPE_PARK_REASON = "scope"

#: Longhaul has no lease-adoption mechanism (nothing bumps a generation for an
#: attempt the way ``runs.lease_generation`` does for the runner), and every
#: attempt gets a fresh ``new_id('tks')`` identity, so this lane always claims at
#: generation 0. It is spelled out rather than defaulted so the fence still has
#: a name here: if a generation ever DOES appear above 0 for an attempt id, the
#: acquire is refused (``status='fenced'``) instead of silently re-entering.
SCOPE_GENERATION = 0


def slugify(name: str) -> str:
    """Convert a category name to its stable URL-safe slug."""
    slug = name.lower().strip().replace("_", "-").replace(" ", "-")
    slug = "".join(char if char.isalnum() or char == "-" else "" for char in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


class Category(TypedDict):
    """A task work category."""

    id: str
    name: str
    slug: str
    color: str
    wip_limit: int
    created_at: str
    updated_at: str


class TaskSession(TypedDict):
    """An executor attempt for a board task."""

    id: str
    board_task_id: str
    seq: int
    session_id: str | None  # ses_* for Claude bridge; None for direct CLIs
    harness: str  # persisted subscription CLI harness
    model: str
    account_id: str | None
    started_at: str
    ended_at: str | None
    end_reason: str | None
    detail: str


class ScopeUnavailable(RuntimeError):
    """The realm this attempt needs is not claimable right now.

    Typed on purpose: ``LonghaulEngine.dispatch`` must be able to tell "somebody
    else owns this project" apart from every other reason ``open_attempt`` can
    fail (a terminal task, an already-live attempt), because this one is the only
    one whose correct answer is *park and retry* rather than *stop*.

    Carries the :class:`~omniagentos.scope.locks.AcquireResult` so the park can
    record the blocker the store actually observed (``blocked_on``) instead of
    re-deriving one and naming a different, equally-valid lock.

    ``status`` is ``'blocked'`` or ``'fenced'``. Both park here, and for longhaul
    that is right for both: a fenced attempt id is refused forever, but the next
    dispatch mints a FRESH ``new_id('tks')`` and is therefore a different holder
    identity, so the retry is not the wedged-worker retry the fence exists to
    stop.
    """

    def __init__(self, result: AcquireResult, *, realm: str, attempt_id: str) -> None:
        self.result = result
        self.status = result.status
        self.realm = realm
        self.attempt_id = attempt_id
        self.blocked_on = result.blocked_on
        self.blocked_path = result.blocked_path
        conflict = result.conflict
        if conflict is not None:
            detail = conflict.describe()
        elif result.status == "fenced":
            detail = f"attempt {attempt_id} was fenced (adopted away) in realm {realm}"
        else:
            detail = f"scope unavailable in realm {realm}"
        super().__init__(detail)


class AttemptLimitReached(RuntimeError):
    """The durable attempt sequence reached the task's configured maximum."""


def _serialized[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Decorator: acquire the store lock, release on exception or return."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self: Any = args[0]
        with self._lock:
            return func(*args, **kwargs)

    return wrapper


class _ConnectionHolder:
    """Weak-referenceable owner of one thread's SQLite connection."""

    __slots__ = ("connection", "__weakref__")

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class LonghaulStore:
    """DAL for longhaul categories, task sessions, and related state.

    File-backed stores hand every thread its own connection (same discipline as
    :class:`~omniagentos.db.store.SqliteStore`). Write methods still serialize
    behind ``_lock`` so CAS transactions stay correct; the per-thread handle is
    what stops one thread's ``commit()`` from landing another thread's still-
    open, about-to-be-rolled-back work (H-27). ``:memory:`` keeps one shared
    connection because each connect would otherwise be a blank database.
    """

    def __init__(self, db_path: str) -> None:
        import logging
        import warnings

        warnings.warn(
            "DEPRECATION WARNING: The longhaul engine/store is frozen and deprecated. It will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        logging.getLogger("omniagentos").warning(
            "DEPRECATION WARNING: The longhaul engine/store is frozen and deprecated. It will be removed in a future release."
        )
        self._db_path = db_path
        self._lock = RLock()
        # Readers here have no separate guard to take: writes already serialize
        # behind ``_lock`` and per-thread connections carry the reads. The alias
        # exists so this store structurally satisfies
        # ``scope.locks._WriterStore`` (see ``_lock_store``) instead of relying
        # on that module's getattr fallback — which resolves to this very same
        # lock, so locking behavior is unchanged. Same idiom as SwarmDal.
        self._read_lock = self._lock
        self._local = local()
        self._closed = False
        self._holders: weakref.WeakSet[_ConnectionHolder] = weakref.WeakSet()
        self._holders_lock = RLock()
        # Built on first use, never in __init__: with scope locks off (the
        # shipped default) this stays None for the store's whole life, so the
        # dark path constructs nothing at all.
        self._path_locks: PathLockStore | None = None
        # T3.8, same discipline: the lane binding is built on first use, so a
        # store constructed with both flags off touches no worktree machinery
        # for its whole life.
        self._lane: LaneWorktrees | None = None

        connection = _connect(db_path)
        # ":memory:" cannot be reopened, so it pins one handle for every thread.
        # A file-backed store adopts the first handle on the constructing thread
        # and opens a fresh connection per additional thread.
        self._shared_connection: sqlite3.Connection | None = (
            connection if db_path == ":memory:" else None
        )
        self._adopt(connection)

    def _adopt(self, connection: sqlite3.Connection) -> _ConnectionHolder:
        holder = _ConnectionHolder(connection)
        self._local.holder = holder
        with self._holders_lock:
            self._holders.add(holder)
        return holder

    @property
    def _connection(self) -> sqlite3.Connection:
        """This thread's connection. Resolve it, never cache it on the instance."""
        if self._closed:
            raise RuntimeError("LonghaulStore is closed")
        shared = self._shared_connection
        if shared is not None:
            return shared
        holder: _ConnectionHolder | None = getattr(self._local, "holder", None)
        if holder is None:
            # Schema was migrated by the caller (tests) or the process bootstrap;
            # new threads only re-apply per-connection pragmas via _connect.
            holder = self._adopt(_connect(self._db_path))
        return holder.connection

    def close(self) -> None:
        """Close every connection this store handed out. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._holders_lock:
                holders = list(self._holders)
                self._holders.clear()
            self._shared_connection = None
            self._local = local()
        for holder in holders:
            try:
                holder.connection.close()
            except sqlite3.Error:  # pragma: no cover -- best-effort teardown
                pass

    def _holds_writer_lock(self) -> bool:
        """Whether THIS thread currently owns ``_lock``."""
        is_owned = getattr(self._lock, "_is_owned", None)
        return True if is_owned is None else bool(is_owned())

    def _begin(self) -> None:
        # Guardrail: multi-statement transactions must not open without the
        # writer lock. Without it two threads can interleave statements on a
        # shared :memory: handle, and unlocked commit paths historically landed
        # another thread's rolled-back work.
        assert self._holds_writer_lock(), (
            "LonghaulStore._begin requires the writer lock; wrap the caller in "
            "`with store._lock:` or decorate it with @_serialized"
        )
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        assert self._holds_writer_lock(), "LonghaulStore._commit requires the writer lock"
        self._connection.commit()

    def _rollback(self) -> None:
        assert self._holds_writer_lock(), "LonghaulStore._rollback requires the writer lock"
        self._connection.rollback()

    # --- Categories (CRUD + slug deduplication) --------------------------------

    @_serialized
    def create_category(
        self,
        name: str,
        color: str = "",
        wip_limit: int = 1,
    ) -> Category:
        """Create a category with slug deduplication: return existing on same slug."""
        slug = self._slugify(name)
        now = utc_now_iso()

        # Check if exists
        existing = self._connection.execute(
            "SELECT * FROM task_categories WHERE slug = ?",
            (slug,),
        ).fetchone()
        if existing:
            return dict(existing)  # type: ignore[return-value]

        # Create new
        cat_id = new_id("cat")
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO task_categories (id, name, slug, color, wip_limit, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cat_id, name, slug, color, wip_limit, now, now),
            )
            self._commit()
        except sqlite3.IntegrityError:
            # Race-safe create-or-get: a concurrent writer (another store/process)
            # landed the same slug between our SELECT and INSERT. The UNIQUE(slug)
            # loser re-selects and returns the row that won.
            self._rollback()
            winner = self._connection.execute(
                "SELECT * FROM task_categories WHERE slug = ?",
                (slug,),
            ).fetchone()
            if winner is None:
                raise  # some other integrity failure — not the slug race
            return dict(winner)  # type: ignore[return-value]
        except BaseException:
            self._rollback()
            raise

        return Category(
            id=cat_id,
            name=name,
            slug=slug,
            color=color,
            wip_limit=wip_limit,
            created_at=now,
            updated_at=now,
        )

    @_serialized
    def list_categories(self) -> list[Category]:
        """List all categories."""
        rows = self._connection.execute(
            "SELECT * FROM task_categories ORDER BY created_at ASC"
        ).fetchall()
        return cast(list[Category], [dict(row) for row in rows])

    @_serialized
    def get_category(self, id_or_slug: str) -> Category | None:
        """Get a category by id or slug."""
        row = self._connection.execute(
            "SELECT * FROM task_categories WHERE id = ? OR slug = ?",
            (id_or_slug, id_or_slug),
        ).fetchone()
        return dict(row) if row else None  # type: ignore[return-value]

    @_serialized
    def update_category(
        self,
        category_id: str,
        name: str | None = None,
        color: str | None = None,
        wip_limit: int | None = None,
    ) -> Category | None:
        """Update a category's fields."""
        self._begin()
        try:
            updates: dict[str, Any] = {}
            if name is not None:
                updates["name"] = name
            if color is not None:
                updates["color"] = color
            if wip_limit is not None:
                updates["wip_limit"] = wip_limit

            if not updates:
                # No changes requested
                row = self._connection.execute(
                    "SELECT * FROM task_categories WHERE id = ?",
                    (category_id,),
                ).fetchone()
                self._commit()
                return dict(row) if row else None  # type: ignore[return-value]

            updates["updated_at"] = utc_now_iso()
            assignments = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [category_id]

            self._connection.execute(
                f"UPDATE task_categories SET {assignments} WHERE id = ?",
                values,
            )
            self._commit()

            # Fetch and return updated row
            row = self._connection.execute(
                "SELECT * FROM task_categories WHERE id = ?",
                (category_id,),
            ).fetchone()
            return dict(row) if row else None  # type: ignore[return-value]
        except BaseException:
            self._rollback()
            raise

    @staticmethod
    def _slugify(name: str) -> str:
        """Compatibility wrapper for the canonical module-level category slugifier."""
        return slugify(name)

    # --- Category slot claiming (with parked-holds-slot semantics) --------------------------------

    @_serialized
    def claim_category_slot(self, category_id: str, board_task_id: str) -> bool:
        """Attempt to claim a slot in a category for a board task.

        Slot semantics:
        - If the task is already in_progress, it already owns a slot: return True (success)
        - If the category has available capacity:
          - Set status='in_progress' (from 'pending'/'open' only) and return True
        - Otherwise:
          - Set park_state='waiting_category' (status remains 'pending') and return False
        - A parked waiting_capacity task HOLDS its slot (status='in_progress') to prevent conflicts

        WIP counting is LANE-SCOPED: only ``lane='longhaul'`` cards consume
        slots. ``category_id`` on any other card (swarm members, fast lane) is
        pure metadata and never blocks or parks a longhaul task.

        This is a single BEGIN IMMEDIATE transaction.
        """
        self._begin()
        try:
            # BLOCKER 2(a): Check if task is already in_progress (already owns slot)
            task_status = self._connection.execute(
                "SELECT status FROM board_tasks WHERE id = ?",
                (board_task_id,),
            ).fetchone()
            if task_status and str(task_status["status"]) == "in_progress":
                # Task already owns a slot — return success
                self._commit()
                return True

            # Get category wip_limit
            cat = self._connection.execute(
                "SELECT wip_limit FROM task_categories WHERE id = ?",
                (category_id,),
            ).fetchone()
            if not cat:
                self._rollback()
                return False

            wip_limit = cat["wip_limit"]

            # Count active and parked_waiting_capacity tasks (both hold slots).
            # Lane-scoped: categorized swarm/fast cards are metadata, not slot
            # holders — without this filter a categorized swarm card would
            # consume longhaul WIP (the one seam; every engine entry is
            # already lane-gated).
            count_row = self._connection.execute(
                "SELECT COUNT(*) as cnt FROM board_tasks "
                "WHERE category_id = ? AND id != ? "
                "AND lane = 'longhaul' "
                "AND status IN ('claimed', 'in_progress') "
                "AND archived_at IS NULL",
                (category_id, board_task_id),
            ).fetchone()
            active_count = count_row["cnt"] if count_row else 0

            if active_count < wip_limit:
                # Slot available: claim it
                cursor = self._connection.execute(
                    "UPDATE board_tasks SET category_id = ?, status = 'in_progress', park_state = NULL "
                    "WHERE id = ? AND status IN ('pending', 'open')",
                    (category_id, board_task_id),
                )
                self._commit()
                return cursor.rowcount == 1
            else:
                # No slot: park in waiting_category
                cursor = self._connection.execute(
                    "UPDATE board_tasks SET category_id = ?, park_state = 'waiting_category' "
                    "WHERE id = ? AND status IN ('pending', 'open')",
                    (category_id, board_task_id),
                )
                self._commit()
                return False

        except BaseException:
            self._rollback()
            raise

    @_serialized
    def next_waiting_in_category(self, category_id: str) -> str | None:
        """Get the next FIFO waiting task in a category (oldest by created_at).

        Returns the board_task_id of the first task parked in waiting_category,
        or None if none are waiting. Lane-scoped (defense in depth): only
        ``lane='longhaul'`` cards are ever handed back for dispatch — a
        categorized swarm card must never shadow the real waiting longhaul
        task in the FIFO wake.
        """
        row = self._connection.execute(
            "SELECT id FROM board_tasks "
            "WHERE category_id = ? AND park_state = 'waiting_category' "
            "AND lane = 'longhaul' "
            "ORDER BY created_at ASC LIMIT 1",
            (category_id,),
        ).fetchone()
        return row["id"] if row else None

    @_serialized
    def next_waiting_scope(self) -> str | None:
        """The next FIFO longhaul task parked on SCOPE contention, or None.

        The mirror of :meth:`next_waiting_in_category` one level up, and the same
        contract: this is a PEEK, not a claim. The caller re-dispatches the task
        and the task re-runs the whole all-or-nothing acquire from scratch —
        nothing is handed over, because a handoff is precisely what reintroduces
        hold-and-wait (see the ``omniagentos.scope.locks`` module docstring on
        why this design has no cycle detector and does not need one).

        Lane-scoped for the same defense-in-depth reason as the category FIFO,
        and discriminated on ``longhaul_json.park_reason`` because
        :data:`SCOPE_PARK_STATE` is a value shared with worker-capacity parks.
        The discriminator is not cleared when a task later runs successfully, so
        a task that was scope-parked once and is now capacity-parked can match;
        the cost of that false positive is exactly one extra idempotent dispatch,
        which re-checks every gate and re-parks, so it is not worth a write on
        the success path (which would change the dark path's bytes).

        Unlike the category FIFO this also skips archived cards: dispatch would
        refuse one anyway, and the wake gets a single peek — spending it on a
        card that can never run would strand the queue behind it.
        """
        row = self._connection.execute(
            "SELECT id FROM board_tasks "
            "WHERE lane = 'longhaul' AND park_state = ? AND archived_at IS NULL "
            "AND json_extract(longhaul_json, '$.park_reason') = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (SCOPE_PARK_STATE, SCOPE_PARK_REASON),
        ).fetchone()
        return row["id"] if row else None

    # --- Scope locks (T3.3) --------------------------------

    def _lock_store(self) -> PathLockStore:
        """The lane's :class:`PathLockStore`, built on first use.

        Deliberately wrapped around THIS store rather than opening its own
        connection: the lock rows and the attempt rows must be serialized behind
        one writer lock, and a second connection would opt straight back out of
        that.
        """
        if self._path_locks is None:
            self._path_locks = PathLockStore(self)
        return self._path_locks

    def _assert_outside_transaction(self, what: str) -> None:
        """Refuse to touch the lock store from inside one of OUR transactions.

        The whole hazard of this wiring in one check. ``PathLockStore._write``
        opens its own ``BEGIN IMMEDIATE`` and ``_lock`` is an RLock, so a future
        caller that moves an acquire or a release inside a ``_begin()`` block
        gets past the mutex and then dies on sqlite's "cannot start a transaction
        within a transaction" — under load, in the one code path that is hardest
        to reproduce. This turns that into a named, immediate failure at the seam
        that caused it.

        Cheap (one C attribute read) and reached only when locks are enabled, so
        the dark path never executes it.
        """
        if self._connection.in_transaction:
            raise RuntimeError(
                f"longhaul scope {what} was called inside an open LonghaulStore "
                "transaction; PathLockStore opens its own BEGIN IMMEDIATE and "
                "cannot nest. Move the call outside the _begin()/_commit() block."
            )

    # --- Per-board-task worktrees (T3.8) --------------------------------

    def _lane_worktrees(self) -> LaneWorktrees:
        if self._lane is None:
            self._lane = lane_worktrees(WORKTREE_LANE)
        return self._lane

    def _worktree_record(self, board_task_id: str) -> dict[str, Any] | None:
        record = (self.get_longhaul_json(board_task_id) or {}).get(WORKTREE_RECORD_KEY)
        return dict(record) if isinstance(record, dict) else None

    def worktree_for(self, board_task_id: str) -> LaneWorktree | None:
        """The recorded worktree for a board task, or ``None``.

        Reads the record; touches neither git nor the filesystem. This is what a
        RESTARTED daemon uses to find the tree a previous process activated —
        the record is the durable half of the mechanism, and it is why a chain of
        attempts survives the process that started it.
        """
        return worktree_from_record(self._worktree_record(board_task_id))

    def working_dir_for(self, board_task_id: str, project_dir: str) -> str:
        """Where this task's executor should RUN — its worktree, or ``project_dir``.

        Call it once per dispatch, BEFORE :meth:`open_attempt`, and pass the
        result to ``open_attempt(working_dir=...)`` as well as to the executor:
        the realm this lane claims is derived from that same string, and a lane
        that claims a worktree while executing in the project has traded a
        serialized-but-correct dispatch for a parallel-and-wrong one.

        Idempotent across the attempt chain. The first call for a task resolves
        the mode, creates the worktree and records both; every later call in the
        chain replays the record and re-attaches the SAME tree — reusing it as-is
        when it is still registered (so attempt N+1 inherits attempt N's
        uncommitted work) and re-attaching the branch at its tip when a previous
        removal salvage-committed it.

        Never raises and never blocks a dispatch: a flag that is off, a project
        git cannot make a worktree in, or an unusable task id all return
        ``project_dir`` unchanged.

        Call with NO transaction open — it writes ``longhaul_json``.
        """
        task_id = str(board_task_id or "")
        if not task_id:
            return project_dir
        record = self._worktree_record(task_id)
        if record is None and not lane_worktrees_enabled(WORKTREE_LANE):
            # Nothing recorded and the flag is off: one env read, no git, no
            # write, no realm resolution. The dark path.
            return project_dir
        # Taken (and released) around the CHECK only. Reading
        # ``connection.in_transaction`` without the store's RLock can observe
        # another thread's open transaction and raise spuriously; holding the
        # lock across the git subprocess below would block every other caller
        # for the duration of a ``worktree add``, which is the opposite trade.
        with self._lock:
            self._assert_outside_transaction("worktree activation")
        lane = self._lane_worktrees()
        try:
            activation = lane.activate(task_id, project_dir, record=record)
        except Exception:  # noqa: BLE001 -- isolation is never a precondition
            LOG.warning(
                "longhaul worktree activation failed for %s; using %s",
                task_id,
                project_dir,
                exc_info=True,
            )
            return project_dir
        if activation.record is not None:
            self._record_worktree(task_id, activation.record)
        return activation.working_dir

    def _record_worktree(self, board_task_id: str, record: dict[str, str]) -> None:
        """Merge the registration into ``longhaul_json``. Best-effort.

        Read-modify-write through the existing accessors, so the park_reason /
        acceptance / workbook_path keys the engine owns are preserved — this
        column is shared state and a blind overwrite here would silently unpark
        a task.
        """
        try:
            state = dict(self.get_longhaul_json(board_task_id) or {})
            if state.get(WORKTREE_RECORD_KEY) == record:
                return
            state[WORKTREE_RECORD_KEY] = dict(record)
            self.set_longhaul_json(board_task_id, state)
        except Exception:  # noqa: BLE001 -- a record is not a precondition
            LOG.warning(
                "longhaul could not record the worktree for %s", board_task_id, exc_info=True
            )

    def integrate_worktree(self, board_task_id: str) -> Integration | None:
        """Merge a finished task's branch back into the project, then remove it.

        THE TERMINAL BOUNDARY IS THE TASK, NOT THE ATTEMPT. ``close_attempt``
        must never call this: every non-final attempt is followed by a successor
        that needs the tree exactly as the last one left it. Call it when the
        board task reaches a terminal state (done / cancelled / archived).

        Merges the sha captured INSIDE the project realm's commit lock — see
        ``LaneWorktrees.integrate``. A conflict leaves the branch alive and named
        so the work can be integrated by hand; the merge itself never wedges the
        project workspace (``merge --abort`` has already run).

        Idempotent: the record is cleared once the tree is gone, so a duplicate
        terminal delivery merges nothing the second time. Call with NO
        transaction open.
        """
        worktree = self.worktree_for(board_task_id)
        if worktree is None:
            return None
        with self._lock:  # coherent check only; see working_dir_for
            self._assert_outside_transaction("worktree integration")
        lane = self._lane_worktrees()
        result = lane.integrate(
            worktree,
            # Only take the durable commit lock when locking is on; with it off
            # the merge holds the in-process lock alone and constructs nothing.
            locks=self._lock_store() if scope_locks_enabled() else None,
            holder=LockHolder(kind="attempt", id=str(board_task_id), lane="longhaul"),
            generation=SCOPE_GENERATION,
        )
        if result.status == "skipped":
            # Nothing merged — keep the tree, or the work is gone.
            LOG.warning(
                "longhaul %s: worktree %s kept — merge skipped (%s)",
                board_task_id,
                worktree.path,
                result.detail,
            )
            return result
        outcome = lane.finish(worktree)
        if outcome is not None and outcome.status == "salvage_failed":
            LOG.error(
                "longhaul %s: worktree %s left in place — salvage failed on a dirty tree",
                board_task_id,
                worktree.path,
            )
            return result
        self._clear_worktree_record(board_task_id)
        return result

    def _clear_worktree_record(self, board_task_id: str) -> None:
        """Drop the registration once the tree is gone (makes integrate idempotent)."""
        try:
            state = dict(self.get_longhaul_json(board_task_id) or {})
            if WORKTREE_RECORD_KEY not in state:
                return
            state.pop(WORKTREE_RECORD_KEY, None)
            self.set_longhaul_json(board_task_id, state)
        except Exception:  # noqa: BLE001 -- bookkeeping, never control flow
            LOG.warning(
                "longhaul could not clear the worktree record for %s",
                board_task_id,
                exc_info=True,
            )

    def _acquire_attempt_scope(
        self, attempt_id: str, working_dir: str | None
    ) -> AcquireResult | None:
        """Claim the realm for one attempt, or raise :class:`ScopeUnavailable`.

        WHY THIS RUNS *BEFORE* open_attempt's TRANSACTION, NOT INSIDE IT
        ===============================================================
        ``PathLockStore._write`` opens its OWN ``BEGIN IMMEDIATE`` (it calls
        ``store._begin()``). ``LonghaulStore._lock`` is an RLock, so calling any
        lock-store method from inside our own ``_begin()`` block re-enters the
        lock quite happily and then raises "cannot start a transaction within a
        transaction" — a bug that appears only once the lane is contended, i.e.
        never in the test that would have caught it cheaply. Two honest options:

        (a) inline the lock SQL into open_attempt's existing transaction, or
        (b) acquire in a transaction of its own immediately before, and release
            on every path where the attempt insert then fails.

        (a) is rejected. The conflict decision is not one INSERT: it is the
        bounded lazy reap, plus the realm's live read, plus
        ``scope.conflict.first_conflict`` over the whole claim set, plus the
        generation fence. Inlining it here would create a sixth divergent copy of
        exactly the algebra ``omniagentos.scope`` exists to have exactly one of,
        and the copy that drifts is the one that hands two writers the same path.

        (b) is what this does, and its cost is bounded and stated: the two
        transactions are not atomic with respect to each other, so a crash in the
        window between them leaves a lock with no attempt behind it. That lock
        carries a TTL (``scope.config.scope_ttl_s``, 90s default) and the lazy
        reaper at the head of the next acquire frees it, so the failure mode is
        "one longhaul dispatch waits up to one TTL", not "the realm is wedged".
        Every non-crash failure path releases explicitly — see open_attempt.

        WHAT LONGHAUL CAN HONESTLY CLAIM
        ================================
        Longhaul is workbook-driven and exploratory: the executor decides what to
        touch as it goes, so this lane CANNOT declare a file-level scope up front
        without lying. v1 therefore claims ``kind='root'`` on ``'.'`` — the WHOLE
        ``working_dir`` realm. That means exactly ONE longhaul task per project
        at a time, and for the attempt's duration it also excludes every other
        lane from that project.

        That is a real parallelism cost and it is taken deliberately, because a
        claim narrower than the truth is worse than no claim at all: it grants
        permission the lane cannot honour, and the resulting collision is exactly
        the one Phase 3 exists to remove. T3.8's per-board-task worktree is what
        restores parallelism, and it needs NOTHING from the algebra here: when
        :meth:`working_dir_for` has handed the engine a worktree, that path is
        what arrives as ``working_dir`` and ``realm_of`` keys it to the
        worktree's own PRIVATE realm, so N longhaul tasks hold N disjoint roots
        instead of queueing on one shared project root. The only requirement is
        that the lane's worktrees base has been REGISTERED as a private base in
        this process before ``realm_of`` sees the path — see below, and see
        ``worktrees.lanes`` for what happens if it has not been.
        """
        if working_dir is None or not scope_locks_enabled():
            # Gate BEFORE realm_of, which costs a realpath and a (memoized)
            # `git rev-parse` SUBPROCESS. With locks off this lane must do
            # byte-for-byte what it did before, and that includes not shelling
            # out. The MODE is still never hardcoded: the acquire below passes
            # enforce=None so scope_locks_mode() makes the actual decision — this
            # is only the do-no-work gate in front of it.
            return None
        self._assert_outside_transaction("acquire")
        # Idempotent, and reached only past the dark gate above. Deliberately NOT
        # conditioned on the worktree flag: ``var/longhaul/worktrees`` is private
        # as a matter of fact about the filesystem, not as a matter of
        # configuration, and the case that needs it most is a process where the
        # flag is OFF but the task carries a RECORDED worktree (the recorded mode
        # wins). An unregistered worktree path folds into the enclosing repo's
        # realm, and the claim would then collide with the whole project instead
        # of naming a directory nobody else can.
        self._lane_worktrees().register_realm_base()
        realm = realm_of(working_dir)
        if realm is None:
            # Fail OPEN, deliberately. realm_of returns None only for a path that
            # cannot be realpath'd at all, and LonghaulEngine._project_dir hands
            # us a directory that exists, so this is unreachable in practice. If
            # it ever fires there is nothing to protect (no other lane can name
            # that realm either), while failing closed would turn one bad path
            # into a permanently undispatchable lane.
            return None
        holder = LockHolder(kind="attempt", id=attempt_id, lane="longhaul")
        result = self._lock_store().try_acquire_scope(
            [ScopeClaim.for_path(realm, ".", kind="root")],
            holder,
            generation=SCOPE_GENERATION,
            enforce=None,  # -> scope_locks_mode(): off | shadow | enforce
        )
        if not result.granted:
            raise ScopeUnavailable(result, realm=realm, attempt_id=attempt_id)
        return result

    def _release_attempt_scope(self, attempt_id: str) -> int:
        """Drop every lock held by one attempt. Idempotent; returns rows released.

        MUST be called outside any transaction this store opened — release goes
        through ``PathLockStore._write``, which begins its own (see
        :meth:`_acquire_attempt_scope`).

        If the mode was turned OFF while an attempt was mid-flight, this returns
        0 and the attempt's locks lapse on their TTL instead. That is the correct
        behaviour for a kill switch: flipping the feature off must stop it from
        acting, not make it act one last time.
        """
        if not scope_locks_enabled():
            return 0
        self._assert_outside_transaction("release")
        return self._lock_store().release_scope("attempt", attempt_id, SCOPE_GENERATION)

    def renew_attempt_scope(self, attempt_id: str) -> bool:
        """Extend a live attempt's lease. True only if something was extended.

        WITHOUT THIS THE LANE'S CLAIM IS DECORATIVE. The default lease is 90s
        (``scope.config.scope_ttl_s``) and a longhaul attempt runs for hours, so
        an unrenewed claim lapses while its executor is still writing and the
        next claimant then takes the realm LEGITIMATELY — two writers, which is
        the exact failure Phase 3 exists to remove, arrived at through the lock
        table rather than in spite of it.

        False means the lease already lapsed or the holder was fenced. Both say
        "you no longer own this", and neither is recoverable by renewing again:
        ``PathLockStore.renew_scope`` deliberately refuses to resurrect an
        expired lock, because another holder may already have taken it. Deciding
        what to DO with a live executor that lost its claim (kill it, let it
        finish, re-acquire if the realm happens to be free) is a policy question
        that belongs with the scheduler work package; this lane logs it.
        """
        if not scope_locks_enabled():
            return False
        self._assert_outside_transaction("renew")
        return self._lock_store().renew_scope("attempt", attempt_id, SCOPE_GENERATION)

    def _release_attempt_scope_quietly(self, attempt_id: str) -> None:
        """Best-effort release: never own the outcome of the call it cleans up after.

        Same rule as ``LonghaulEngine._notify`` ("notifications never own the
        transition"), and here it is load-bearing twice over:

        * in ``close_attempt`` the CAS has ALREADY COMMITTED, so raising would
          make an idempotent boundary look like it failed and invite a duplicate
          terminal delivery — the exact thing that CAS exists to prevent;
        * in ``open_attempt``'s failure path it would MASK the real error (a
          terminal task, a live attempt) behind a lock-store error.

        Swallowing is safe because the lock carries a TTL: the worst case is one
        project waiting a single lease instead of being freed immediately, and
        that beats either of the failures above.
        """
        try:
            self._release_attempt_scope(attempt_id)
        except Exception:  # noqa: BLE001 - the TTL is the backstop; see above.
            LOG.warning(
                "longhaul scope release failed for attempt %s; the lease will lapse",
                attempt_id,
                exc_info=True,
            )

    # --- Task sessions (ordered executor-attempt chain) --------------------------------

    @_serialized
    def open_attempt(
        self,
        board_task_id: str,
        harness: str,
        model: str,
        account_id: str | None = None,
        session_id: str | None = None,
        working_dir: str | None = None,
        max_sessions: int | None = None,
    ) -> TaskSession:
        """Open a new executor attempt for a board task.

        Returns the new TaskSession.
        Raises if there is already an open (ended_at IS NULL) attempt for this task.

        ``working_dir`` is the directory the executor will run in; passing it
        opts this attempt into cross-lane scope locking (see
        :meth:`_acquire_attempt_scope`). With ``scope_locks_mode()`` off — the
        shipped default — it is inert: nothing is resolved, nothing is written,
        and the attempt row is byte-for-byte the one this method has always
        produced. When enforcing, a realm somebody else already owns raises
        :class:`ScopeUnavailable` and NO attempt row is created.
        """
        # Minted before the transaction because the scope claim is keyed on this
        # attempt's identity and must be taken before the row exists (the
        # transaction cannot call the lock store — see _acquire_attempt_scope).
        # new_id is a pure uuid4 derivative, so pulling it forward consumes
        # nothing and changes no row.
        attempt_id = new_id("tks")
        scope = self._acquire_attempt_scope(attempt_id, working_dir)
        self._begin()
        try:
            # A terminal or archived task must never gain a new attempt — this
            # guard runs inside the same transaction as the insert so a racing
            # done-transition cannot slip an attempt in behind it.
            board_row = self._connection.execute(
                "SELECT status, archived_at FROM board_tasks WHERE id = ?",
                (board_task_id,),
            ).fetchone()
            if (
                board_row is None
                or str(board_row["status"])
                in (
                    "done",
                    "blocked",
                    "cancelled",
                )
                or board_row["archived_at"] is not None
            ):
                raise ValueError(f"cannot open attempt for terminal/archived task {board_task_id}")
            # Check for existing open attempt
            open_attempt = self._connection.execute(
                "SELECT id FROM task_sessions WHERE board_task_id = ? AND ended_at IS NULL",
                (board_task_id,),
            ).fetchone()
            if open_attempt:
                self._rollback()
                raise RuntimeError(
                    f"Cannot open a new attempt: task {board_task_id} already has an open attempt"
                )

            # Get next seq
            seq_row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 as next_seq FROM task_sessions WHERE board_task_id = ?",
                (board_task_id,),
            ).fetchone()
            seq = seq_row["next_seq"]

            # Defense in depth for the terminal/reconcile fence: enforce the
            # maximum in the same BEGIN IMMEDIATE transaction that allocates the
            # sequence. Even legacy or corrupt state without
            # max_sessions_reached cannot create attempt max+1.
            if max_sessions is not None and seq >= max(1, max_sessions):
                raise AttemptLimitReached(f"maximum sessions reached before attempt #{seq + 1}")

            # Create new attempt
            now = utc_now_iso()

            self._connection.execute(
                "INSERT INTO task_sessions "
                "(id, board_task_id, seq, session_id, harness, model, account_id, started_at, ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    board_task_id,
                    seq,
                    session_id,
                    harness,
                    model,
                    account_id,
                    now,
                    None,
                    None,
                    "",
                ),
            )
            self._commit()

            return TaskSession(
                id=attempt_id,
                board_task_id=board_task_id,
                seq=seq,
                session_id=session_id,
                harness=harness,
                model=model,
                account_id=account_id,
                started_at=now,
                ended_at=None,
                end_reason=None,
                detail="",
            )
        except BaseException:
            self._rollback()
            # The attempt row did not land, so nothing will ever close it and
            # release its scope. Give the realm back HERE — outside the (now
            # rolled-back) transaction, since release opens its own — or a
            # refused insert would hold the whole project hostage for a full TTL.
            if scope is not None:
                self._release_attempt_scope_quietly(attempt_id)
            raise

    @_serialized
    def close_attempt(self, attempt_id: str, end_reason: str, detail: str = "") -> bool:
        """Close an open attempt (idempotent: idempotent CAS on ended_at).

        Returns True if the attempt was closed, False if already closed.

        This is also where the attempt's scope locks are released. Every terminal
        path in ``LonghaulEngine`` funnels through this CAS — completion, review
        denial, usage limit, auth failure, crash reconciliation, kill, archive —
        which is exactly why the release belongs here and not at each of them.
        """
        self._begin()
        try:
            now = utc_now_iso()
            cursor = self._connection.execute(
                "UPDATE task_sessions SET ended_at = ?, end_reason = ?, detail = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (now, end_reason, detail, attempt_id),
            )
            closed = cursor.rowcount == 1
            self._commit()
        except BaseException:
            self._rollback()
            raise
        # AFTER the commit and OUTSIDE the transaction: release opens its own
        # BEGIN IMMEDIATE, and calling it above would re-enter this store's
        # RLock inside our transaction and raise "cannot start a transaction
        # within a transaction".
        #
        # Unconditional, not gated on `closed`. Release is itself idempotent (a
        # holder with nothing live matches zero rows), and doing it on the
        # already-closed path too closes the crash window between the CAS commit
        # and this call: a process that dies in between leaves locks that a
        # later duplicate terminal delivery now cleans up immediately instead of
        # leaving them to lapse on their TTL.
        #
        # Best-effort: the CAS above has already committed, so a lock-store
        # failure must not be able to turn a WON close into a raised exception
        # that the caller reads as "not closed".
        self._release_attempt_scope_quietly(attempt_id)
        return closed

    @_serialized
    def close_attempt_with_task_state(
        self,
        attempt_id: str,
        end_reason: str,
        detail: str,
        *,
        board_task_id: str,
        phase: str,
        updates: dict[str, Any],
        action: str,
        account_effect: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any] | None, bool]:
        """Atomically close an attempt, account effect, and pacing state.

        A terminal callback used to commit ``task_sessions.ended_at`` and only
        later persist ``longhaul_json.next_dispatch_at``. A concurrent tick (or
        another daemon process) could observe the gap as "no live attempt, no
        backoff" and open the replacement that the pacing guard was meant to
        prevent. This transaction makes the close CAS and successor horizon one
        visibility boundary.

        Usage-limit cooldown and authentication disable are part of the same
        boundary. Applying them after the close loses the effect on a crash and
        skips it entirely when ``max_sessions`` blocks the task; applying them
        before the close lets duplicate callbacks refresh or repeat the effect.
        The ended-at CAS owns all three writes exactly once.

        The scope release deliberately remains after commit, matching
        :meth:`close_attempt`: the lock store opens its own transaction and the
        durable TTL is the recovery backstop.
        """

        if account_effect is not None:
            effect_kind = str(account_effect.get("kind") or "")
            if effect_kind != end_reason or effect_kind not in {
                "usage_limited",
                "auth_failed",
            }:
                raise ValueError(
                    "terminal account effect must match usage_limited/auth_failed end_reason"
                )
            if effect_kind == "usage_limited" and not str(
                account_effect.get("cooldown_until") or ""
            ):
                raise ValueError("usage_limited account effect requires cooldown_until")

        now = utc_now_iso()
        state: dict[str, Any] | None = None
        closed = False
        account_effect_applied = False
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE task_sessions SET ended_at = ?, end_reason = ?, detail = ? "
                "WHERE id = ? AND board_task_id = ? AND ended_at IS NULL",
                (now, end_reason, detail, attempt_id, board_task_id),
            )
            closed = cursor.rowcount == 1
            if closed:
                if account_effect is not None:
                    attempt_row = self._connection.execute(
                        "SELECT account_id FROM task_sessions WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                    account_id = (
                        str(attempt_row["account_id"])
                        if attempt_row is not None and attempt_row["account_id"] is not None
                        else None
                    )
                    if account_id is not None:
                        effect_detail = str(account_effect.get("detail") or "")
                        if end_reason == "usage_limited":
                            effect_cursor = self._connection.execute(
                                "UPDATE claude_accounts SET status = 'rate_limited', "
                                "status_detail = ?, cooldown_until = ?, updated_at = ? "
                                "WHERE id = ?",
                                (
                                    effect_detail,
                                    str(account_effect["cooldown_until"]),
                                    now,
                                    account_id,
                                ),
                            )
                        else:
                            effect_cursor = self._connection.execute(
                                "UPDATE claude_accounts SET enabled = 0, status = 'error', "
                                "status_detail = ?, updated_at = ? WHERE id = ?",
                                (effect_detail, now, account_id),
                            )
                        account_effect_applied = effect_cursor.rowcount == 1
                row = self._connection.execute(
                    "SELECT longhaul_json FROM board_tasks "
                    "WHERE id = ? AND archived_at IS NULL "
                    "AND status NOT IN ('done', 'blocked', 'cancelled')",
                    (board_task_id,),
                ).fetchone()
                if row is not None:
                    raw = row["longhaul_json"]
                    try:
                        decoded = json.loads(raw or "{}")
                    except (TypeError, ValueError):
                        decoded = {}
                    state = decoded if isinstance(decoded, dict) else {}
                    state.update(updates)
                    state["phase"] = phase
                    self._connection.execute(
                        "UPDATE board_tasks SET longhaul_json = ?, status = 'in_progress', "
                        "park_state = NULL, updated_at = ? WHERE id = ?",
                        (
                            json.dumps(
                                state,
                                separators=(",", ":"),
                                sort_keys=True,
                                default=str,
                            ),
                            now,
                            board_task_id,
                        ),
                    )
                    self._connection.execute(
                        "INSERT INTO events "
                        "(ts, type, actor, action, target_type, target_id, "
                        "payload_json, trace_id) "
                        "VALUES (?, 'task.longhaul', 'longhaul', ?, "
                        "'board_task', ?, ?, '')",
                        (
                            now,
                            action,
                            board_task_id,
                            json.dumps(
                                {
                                    "phase": phase,
                                    "status": "in_progress",
                                    "park_state": None,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
            self._commit()
        except BaseException:
            self._rollback()
            raise

        # Unconditional and best-effort for the same crash-window reason as
        # close_attempt(): a duplicate delivery may be the process that finally
        # releases a scope whose close commit already won.
        self._release_attempt_scope_quietly(attempt_id)
        return closed, state, account_effect_applied

    @_serialized
    def list_attempts(self, board_task_id: str) -> list[TaskSession]:
        """List all attempts for a board task, ordered by seq ASC."""
        rows = self._connection.execute(
            "SELECT * FROM task_sessions WHERE board_task_id = ? ORDER BY seq ASC",
            (board_task_id,),
        ).fetchall()
        return cast(list[TaskSession], [dict(row) for row in rows])

    @_serialized
    def current_attempt(self, board_task_id: str) -> TaskSession | None:
        """Get the open (ended_at IS NULL) attempt for a board task, if any."""
        row = self._connection.execute(
            "SELECT * FROM task_sessions WHERE board_task_id = ? AND ended_at IS NULL",
            (board_task_id,),
        ).fetchone()
        return dict(row) if row else None  # type: ignore[return-value]

    # --- Longhaul task fields --------------------------------

    @_serialized
    def set_lane(self, board_task_id: str, lane: str | None) -> bool:
        """Set the lane for a task (NULL|'fast'|'longhaul')."""
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE board_tasks SET lane = ? WHERE id = ?",
                (lane, board_task_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_park_state(self, board_task_id: str, park_state: str | None) -> bool:
        """Set the park_state for a task (NULL|'waiting_category'|'waiting_capacity'|'waiting_review')."""
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE board_tasks SET park_state = ? WHERE id = ?",
                (park_state, board_task_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_longhaul_json(self, board_task_id: str, longhaul_json: dict[str, Any]) -> bool:
        """Set the longhaul_json field for a task."""
        self._begin()
        try:
            json_str = json.dumps(longhaul_json, separators=(",", ":"), sort_keys=True)
            cursor = self._connection.execute(
                "UPDATE board_tasks SET longhaul_json = ? WHERE id = ?",
                (json_str, board_task_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def get_longhaul_json(self, board_task_id: str) -> dict[str, Any] | None:
        """Get the longhaul_json field for a task."""
        row = self._connection.execute(
            "SELECT longhaul_json FROM board_tasks WHERE id = ?",
            (board_task_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["longhaul_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @_serialized
    def list_parked(self, park_state: str) -> list[str]:
        """List board_task_ids parked in a given state."""
        rows = self._connection.execute(
            "SELECT id FROM board_tasks WHERE park_state = ? ORDER BY created_at ASC",
            (park_state,),
        ).fetchall()
        return [row["id"] for row in rows]

    @_serialized
    def set_task_category(self, board_task_id: str, category_id: str | None) -> bool:
        """Set the category for a board task.

        Any card may carry a category — it is cross-lane metadata (the board
        taxonomy). Longhaul WIP semantics stay safe because
        :meth:`claim_category_slot` and :meth:`next_waiting_in_category` are
        lane-scoped: a categorized swarm card never consumes a slot.
        """
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE board_tasks SET category_id = ? WHERE id = ?",
                (category_id, board_task_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    # --- Account cooldown --------------------------------

    @_serialized
    def set_account_cooldown(self, account_id: str, until_iso: str, detail: str = "") -> bool:
        """Set an account to rate_limited with a cooldown_until timestamp.

        Returns True if the account was updated, False if not found.

        Thin delegate: the single cooldown writer lives in
        ``omniagentos.accounts.service`` (WP2 -- one cooldown/limit authority;
        this method exists only so longhaul callers keep their store-shaped
        seam)."""
        from omniagentos.accounts.service import set_account_cooldown as _set_account_cooldown

        return _set_account_cooldown(account_id, until_iso, detail, self._db_path)

    @_serialized
    def clear_expired_cooldowns(self, now_iso: str) -> list[str]:
        """Clear expired cooldowns (cooldown_until <= now), restoring status='ok'.

        Returns list of account_ids that were cleared.

        Thin delegate over the single implementation in
        ``omniagentos.accounts.service`` (WP2)."""
        from omniagentos.accounts.service import (
            clear_expired_cooldowns as _clear_expired_cooldowns,
        )

        return _clear_expired_cooldowns(now_iso, self._db_path)

    # --- Conversation helpers (thin wrappers over ConversationStore) --------------------------------

    @_serialized
    def append_task_turn(
        self,
        board_task_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Append a message to a task's conversation thread."""
        meta_json = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True, default=str)
        now = utc_now_iso()
        turn_id = new_id("cnv")

        self._begin()
        try:
            # Get next seq
            seq_row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM conversations "
                "WHERE scope_type = 'task' AND scope_id = ?",
                (board_task_id,),
            ).fetchone()
            seq = seq_row["next_seq"] if seq_row else 0

            # Insert turn
            self._connection.execute(
                "INSERT INTO conversations "
                "(id, scope_type, scope_id, seq, role, content, created_at, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, "task", board_task_id, seq, role, content, now, meta_json),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def task_turns(
        self,
        board_task_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get recent turns in a task's conversation, newest first."""
        rows = self._connection.execute(
            "SELECT id, seq, role, content, model, created_at, meta_json "
            "FROM conversations WHERE scope_type = 'task' AND scope_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (board_task_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def pending_steering(self, board_task_id: str) -> list[dict[str, Any]]:
        """Get turns with delivery.pending=true in a task's conversation."""
        rows = self._connection.execute(
            "SELECT id, seq, role, content, created_at, meta_json "
            "FROM conversations WHERE scope_type = 'task' AND scope_id = ? "
            "AND json_extract(meta_json, '$.delivery.pending') = 1 "
            "ORDER BY seq ASC",
            (board_task_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def task_id_for_session(self, session_id: str) -> str | None:
        """Resolve the board task whose attempt ran (or runs) this session."""
        row = self._connection.execute(
            "SELECT board_task_id FROM task_sessions WHERE session_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return str(row["board_task_id"]) if row else None

    @_serialized
    def mark_turn_delivered(
        self,
        turn_id_or_scope_seq: str | tuple[str, int],
        session_id: str,
    ) -> bool:
        """Mark a turn delivered by session.

        Args:
            turn_id_or_scope_seq: either turn id or (scope_id, seq) tuple
            session_id: the session that applied the turn

        Returns True if updated, False otherwise.
        """
        self._begin()
        try:
            now = utc_now_iso()
            delivery = {"session_id": session_id, "delivered_at": now}
            delivery_json = json.dumps(delivery, separators=(",", ":"), sort_keys=True)

            if isinstance(turn_id_or_scope_seq, str):
                # By turn ID
                cursor = self._connection.execute(
                    "UPDATE conversations SET meta_json = json_set(meta_json, '$.delivery', json(?)) "
                    "WHERE id = ?",
                    (delivery_json, turn_id_or_scope_seq),
                )
            else:
                # By (scope_id, seq)
                scope_id, seq = turn_id_or_scope_seq
                cursor = self._connection.execute(
                    "UPDATE conversations SET meta_json = json_set(meta_json, '$.delivery', json(?)) "
                    "WHERE scope_type = 'task' AND scope_id = ? AND seq = ?",
                    (delivery_json, scope_id, seq),
                )

            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise
