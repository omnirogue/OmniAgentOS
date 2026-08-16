"""The session lane's half of the Phase-3 scope-lock wiring.

:mod:`omniagentos.scope` is the mechanism — path algebra, the conflict matrix and
a durable, deadlock-free lock store. This module is the session lane's ADAPTER
onto it: it decides WHAT a bridge session claims, WHEN it claims it, and what the
supervisor does with the four answers it can get back. The supervisor keeps its
lifecycle; this keeps every scope decision in one place where it can be read as a
whole.

SHIPS DARK, AND THAT IS THE LOAD-BEARING PROPERTY
=================================================
:func:`~omniagentos.scope.config.scope_locks_mode` defaults to ``off``, and with
it off EVERY entry point here returns before touching the database, the
filesystem or ``git``. No lock row, no waiter row, no ``scope_json`` write, no
``realm_of`` shell-out, no extra ``SELECT``. ``test_scope_wiring.py`` asserts
that by making the lock store itself explode if it is ever constructed while the
mode is off — a much stronger statement than counting rows, because it also
catches the reads.

That is why the mode gate is a CACHED read (see :class:`SessionScopeGate`).
``scope_locks_mode()`` parses ``configs/parallelism.yaml`` whenever
``OMNIAGENTOS_SCOPE_LOCKS`` is unset, and the supervisor calls into this module
once per session per poll — at 2 Hz over a few thousand rows that is a YAML parse
storm entirely in the OFF path. The cache bounds the kill switch's latency to
``mode_cache_s`` (1 second by default) and costs one monotonic clock read per
call otherwise.

WHAT A SESSION CLAIMS, AND WHY IT IS DERIVED RATHER THAN PASSED
==============================================================
A bridge session's declared scope is its project dir plus its frozen
``granted_roots`` — the exact two values the OS sandbox and the PreToolUse hook
already use to decide where that session may write (see
``supervisor._launch`` and ``dal.decode_granted_roots``). Deriving the claim set
from those values rather than accepting it as an independent argument is what
makes "grants and scope cannot desync" STRUCTURAL: there is only one input, so
there is nothing to disagree with. ``SessionSupervisor.spawn``'s ``declared_scope``
keyword exists for a planner that eventually knows better (Phase 4's
declared-vs-actual work), and until something passes it the derivation is the
only path.

Each root is claimed as a ``root`` (subtree) claim, which is COARSE on purpose:
today the session lane's honest statement about a bridge session is "it may write
anywhere the sandbox lets it", and a claim narrower than the sandbox would be a
lie the locks then enforce. The consequence is worth stating plainly: under
``enforce``, two ordinary sessions pointed at the same project cannot run at
once. That is the point of enforcement, it is why the mode ships off, and it is
why the shadow ramp exists — the soak measures exactly this before anybody turns
it on.

THE LANE-OWNED EXEMPTION
========================
``[longhaul:<attempt>]`` / ``[swarm:<attempt>]`` sessions take NO locks of their
own. Their owning lane already holds the scope for the work the session performs,
and the session's holder identity is a different one — so a swarm worker taking
its own whole-project root claim would collide with its own coordinator's, be
refused, and park forever waiting for a lock that will not be released until the
worker it is blocking finishes. Self-deadlock, and by construction rather than by
accident. The marker on ``sessions.title`` is the same signal
``supervisor._scheduler_owns_respawn`` reads for respawn ownership, and
``test_scope_wiring.py`` asserts the two marker sets stay identical.

RELEASE IS NOT DONE BY THE REAPER
=================================
Every terminal transition funnels through ``SessionSupervisor._finish``, and that
is where the release lives. The idle reaper deliberately does NOT release:
:meth:`~SessionSupervisor._reap` only sets ``kill_requested`` (and in its default
dry-run mode does not even do that), so the session's process is still ALIVE and
still writing when the reaper runs. Releasing there would hand its paths to a
second writer while the first is mid-write — precisely the failure these locks
exist to prevent. The reaper's kill reaches ``_kill_session`` → ``_finish`` on the
next poll, after the process is dead, and the release happens there.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from omniagentos.scope.config import ScopeLockMode, scope_locks_mode, scope_ttl_s
from omniagentos.scope.locks import (
    AcquireResult,
    HolderKind,
    Lane,
    LockHolder,
    PathLockStore,
    ScopeLockError,
)
from omniagentos.scope.model import DEFAULT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import ScopePathError, realm_of, resolve_into_realm
from omniagentos.sessions.dal import decode_granted_roots
from omniagentos.worktrees.lanes import (
    WORKTREE_RECORD_KEY,
    Activation,
    Integration,
    LaneWorktree,
    LaneWorktrees,
    lane_worktrees,
    lane_worktrees_enabled,
)

LOG = logging.getLogger(__name__)

__all__ = [
    "HOLDER_KIND",
    "LANE",
    "LANE_OWNED_TITLE_MARKERS",
    "SCOPE_ENVELOPE_VERSION",
    "SESSION_GENERATION",
    "WORKTREE_LANE",
    "ScopeDecision",
    "ScopeOutcome",
    "SessionScopeGate",
    "claims_for_roots",
    "claims_for_session",
    "decode_scope",
    "decode_worktree",
    "encode_scope",
    "is_lane_owned",
    "session_holder",
]

#: This lane's ``resource_locks.lane`` value (migration 059's CHECK list).
#: Annotated with the store's Literal so a typo is a type error here rather than
#: an opaque CHECK-constraint IntegrityError at the first acquire.
LANE: Lane = "session"

#: This lane's ``resource_locks.holder_kind``. The holder id is the ``ses_`` id,
#: which exists before the session row is inserted, so a claim can be taken at
#: the moment the row is created rather than after the process is already live.
HOLDER_KIND: HolderKind = "session"

#: Title markers for sessions a DURABLE LANE ENGINE owns. Kept here rather than
#: imported from ``supervisor`` because this module sits underneath it; a test
#: asserts these are exactly ``supervisor``'s ``_LONGHAUL_TITLE_MARKER`` /
#: ``_SWARM_TITLE_MARKER`` so the two cannot drift.
LANE_OWNED_TITLE_MARKERS: tuple[str, ...] = ("[longhaul:", "[swarm:")

#: Sessions carry NO lease generation: migration 059 added ``lease_generation``
#: to ``runs`` (and 052 to ``swarm_runs``), not to ``sessions``, because nothing
#: adopts a bridge session away from its supervisor — the supervisor that owns
#: the pid is the only writer.
#:
#: The fence is still live, and this constant is why: ``try_acquire_scope``
#: refuses any generation LOWER than the highest ever recorded for the same
#: holder identity, so if some future lane ever does adopt a session and takes
#: its locks at a higher generation, this supervisor's next acquire comes back
#: ``fenced`` and it stops instead of writing alongside the adopter.
SESSION_GENERATION = 0

#: The ``worktrees.lanes`` binding this lane uses
#: (``var/sessions/worktrees/<session_id>/main`` on ``session/<session_id>/main``).
WORKTREE_LANE = "session"

#: ``sessions.scope_json`` version for the ENVELOPE spelling. The column has
#: always held a bare JSON LIST of claims and still does whenever there is
#: nothing but claims to record — the envelope appears only when a T3.8 worktree
#: registration has to ride along, so the shipped bytes are unchanged and
#: :func:`decode_scope` reads both.
SCOPE_ENVELOPE_VERSION = 2

#: How much of the TTL may elapse before a renew is actually written. A third of
#: the lease gives two chances to renew before it lapses (the standard
#: heartbeat margin) while keeping the write rate off the 2 Hz poll loop: without
#: it, every live session would cost one write transaction per poll.
_RENEW_FRACTION = 3.0

#: How long a session's merge waits out ANOTHER process's commit lock on the
#: project realm. Deliberately far below ``worktrees.lanes``' default: the
#: session lane's terminal path runs on the supervisor's 2 Hz POLL THREAD, and a
#: thread parked there for a lock TTL is a stalled fleet. A refused merge leaves
#: the worktree and its record in place, which is the recoverable outcome.
MERGE_WAIT_S = 5.0

ScopeOutcome = Literal[
    "off",
    "exempt",
    "undeclared",
    "granted",
    "shadowed",
    "parked",
    "fenced",
]


# ---------------------------------------------------------------------------
# Identity, exemption, claim derivation
# ---------------------------------------------------------------------------


def is_lane_owned(title: Any) -> bool:
    """True when a durable lane engine — not the supervisor — owns this session.

    Such a session must NOT take its own locks; see the module docstring on
    self-deadlock. Mirrors ``supervisor._scheduler_owns_respawn``'s predicate
    exactly (substring, not prefix: the marker is prepended to a human title, so
    it is at the front today, but matching on containment is what the respawn
    check does and the two must agree).
    """
    text = str(title or "")
    return any(marker in text for marker in LANE_OWNED_TITLE_MARKERS)


def session_holder(session_id: str) -> LockHolder:
    """The :class:`LockHolder` identity for one bridge session."""
    return LockHolder(kind=HOLDER_KIND, id=str(session_id), lane=LANE)


def claims_for_roots(roots: Iterable[str]) -> tuple[ScopeClaim, ...]:
    """Subtree claims for a set of absolute write roots, de-duplicated.

    A root that will not resolve — an unreadable path, a path on another volume,
    anything :func:`~omniagentos.scope.paths.realm_of` cannot key — is DROPPED
    with a debug line rather than raising. Two reasons: the OS sandbox is the
    actual write boundary and is unaffected, and a spawn that died because one of
    a project's five grant dirs had been renamed would be a far worse failure
    than a claim set that is one entry short. The dropped entry is still visible
    in ``granted_roots`` on the row.

    Order is preserved (project dir first, then grants in their frozen order) so
    the persisted ``scope_json`` is stable across processes.
    """
    claims: dict[tuple[str, str], ScopeClaim] = {}
    for raw in roots:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            realm = realm_of(text)
            components = None if realm is None else resolve_into_realm(text, realm)
        except (OSError, ScopePathError, ValueError):  # pragma: no cover - defensive
            LOG.debug("scope: unusable session write root %r", text, exc_info=True)
            continue
        if realm is None or components is None:
            LOG.debug("scope: session write root %r has no resolvable realm", text)
            continue
        claim = ScopeClaim(
            realm=realm,
            components=components,
            kind="root",
            purpose=DEFAULT_PURPOSE,
        )
        claims.setdefault((claim.realm, claim.path_text), claim)
    return tuple(claims.values())


def claims_for_session(session: Mapping[str, Any]) -> tuple[ScopeClaim, ...]:
    """The claim set a session row declares: its project dir plus its grants.

    Derived from the row so that the queued-spawn path (which never saw the
    ``spawn`` arguments) and the direct path compute the same set from the same
    persisted values.
    """
    return claims_for_roots([str(session.get("project_dir") or ""), *decode_granted_roots(session)])


def encode_scope(
    claims: Sequence[ScopeClaim], *, worktree: Mapping[str, Any] | None = None
) -> str | None:
    """Serialize a claim set for ``sessions.scope_json``; ``()`` -> ``None``.

    A RECORD, not the mechanism. ``resource_locks`` is what actually excludes a
    second writer; this column is what Phase 4 diffs declared-against-actual
    with, and what a crash-resume replays. Keys are sorted and separators are
    tight so the stored text is byte-stable.

    ``worktree`` (T3.8's recorded registration) switches the spelling from the
    historical bare LIST to a versioned envelope. Without it — the shipped
    default, because the flag is off — the emitted bytes are unchanged, which is
    what makes "sessions are byte-for-byte what they were" a literal claim.
    A claimless session with a worktree still records: ``()`` collapses to
    ``None`` only when there is nothing at all to say.
    """
    if not claims and not worktree:
        return None
    entries = [
        {
            "realm": claim.realm,
            "path": claim.path_text,
            "kind": claim.kind,
            "purpose": claim.purpose,
        }
        for claim in claims
    ]
    if not worktree:
        return json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return json.dumps(
        {
            "v": SCOPE_ENVELOPE_VERSION,
            "claims": entries,
            WORKTREE_RECORD_KEY: dict(worktree),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _scope_entries(raw: Any) -> list[Any]:
    """The claim entries in either spelling of the column; ``[]`` if unreadable."""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        entries = value.get("claims")
        return list(entries) if isinstance(entries, list) else []
    return []


def decode_worktree(raw: Any) -> dict[str, Any] | None:
    """The recorded T3.8 registration in ``sessions.scope_json``, or ``None``.

    Total, like :func:`decode_scope`: an unreadable record means "nothing
    recorded", the caller re-resolves from the ambient flag, and a session
    without a record has no worktree to be wrong about.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    record = value.get(WORKTREE_RECORD_KEY)
    return dict(record) if isinstance(record, Mapping) else None


def decode_scope(raw: Any) -> tuple[ScopeClaim, ...]:
    """Parse ``sessions.scope_json`` back into claims; anything malformed -> ``()``.

    Fails to the empty set rather than raising, matching
    ``dal.decode_granted_roots``: a column this module wrote is not a trust
    boundary, but a reader that dies on one bad row starves every other session.

    Reads BOTH spellings — the historical bare list and the envelope
    :func:`encode_scope` writes when a worktree registration rides along.
    """
    claims: list[ScopeClaim] = []
    value = _scope_entries(raw)
    for entry in value:
        if not isinstance(entry, dict):
            continue
        realm = str(entry.get("realm") or "")
        if not realm:
            continue
        kind = entry.get("kind")
        try:
            claims.append(
                ScopeClaim.for_path(
                    realm,
                    str(entry.get("path") or "."),
                    kind=kind if kind in ("file", "root") else "root",
                    purpose=str(entry.get("purpose") or DEFAULT_PURPOSE),
                )
            )
        except ScopePathError:
            continue
    return tuple(claims)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """What the gate decided, and what the caller is allowed to do next.

    ``may_launch`` is the only thing the supervisor's control flow reads. It is
    False for exactly two outcomes and they are NOT the same failure:

    ``parked``
        A live conflict under ``enforce``. A durable waiter row exists; the
        session stays in ``starting`` and the spawn ingress retries it every
        pass. This is a delay, not an error.
    ``fenced``
        This holder identity was adopted away. Retrying is wrong at any
        generation (see :data:`SESSION_GENERATION`), so the caller terminalizes.
    """

    outcome: ScopeOutcome
    mode: ScopeLockMode = "off"
    claims: tuple[ScopeClaim, ...] = ()
    result: AcquireResult | None = None
    waiter_id: int = 0
    detail: str = ""

    @property
    def may_launch(self) -> bool:
        """May the caller start (or resume) the session process?"""
        return self.outcome not in ("parked", "fenced")

    @property
    def parked(self) -> bool:
        """Refused and queued. Leave the row in ``starting`` and retry later."""
        return self.outcome == "parked"

    @property
    def fenced(self) -> bool:
        """Adopted away. Stop; do not retry."""
        return self.outcome == "fenced"

    @property
    def holds_locks(self) -> bool:
        """True when lock rows are now held for this session."""
        return self.outcome in ("granted", "shadowed")


_OFF = ScopeDecision(outcome="off")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class SessionScopeGate:
    """Scope claims for one supervisor's sessions, over one ``SessionsDal``.

    Constructed around the DAL the supervisor already owns, never around a
    private connection: Phase 2 serialized every writer behind that DAL's lock,
    and a second connection into the same database would opt straight back out of
    it.

    THE RE-ENTRANCY RULE. :class:`~omniagentos.scope.locks.PathLockStore` opens
    its own ``BEGIN IMMEDIATE`` through ``dal._begin()``. ``dal._lock`` is an
    ``RLock``, so calling any method here from INSIDE an open DAL transaction
    re-enters the lock, reaches ``BEGIN IMMEDIATE`` with one already open, and
    raises "cannot start a transaction within a transaction" — under load only,
    which is the worst way to find out. Every method here is therefore documented
    as "call me with no transaction open", and :meth:`_persist_scope` opens and
    closes its own before any lock call is made.
    """

    def __init__(
        self,
        dal: Any,
        *,
        locks: PathLockStore | None = None,
        mode_cache_s: float = 1.0,
    ) -> None:
        self._dal = dal
        self._locks = locks
        self._mode_cache_s = max(0.0, float(mode_cache_s))
        self._mode_cached: tuple[ScopeLockMode, float] | None = None
        self._mode_expires = 0.0
        # Sessions this process believes hold lock rows. Purely an OPTIMIZATION
        # gate, never a correctness one: the DB is the truth, and the TTL reclaims
        # anything this set forgets (a supervisor restart empties it; reconcile's
        # adopt path refills it for the sessions that are still alive).
        #
        # It exists because both renew and release are called for every session on
        # every poll. Without it, a fleet of terminal rows would cost two write
        # transactions each per pass forever.
        self._holding: set[str] = set()
        self._last_renew: dict[str, float] = {}
        # T3.8: session_id -> the private worktree this supervisor activated.
        # Empty while the worktree flag is off, which keeps ``release``'s
        # single-lookup early return exactly as cheap as it was.
        self._worktrees: dict[str, LaneWorktree] = {}
        self._lane: LaneWorktrees | None = None

    # --- config -------------------------------------------------------------

    def _resolved(self) -> tuple[ScopeLockMode, float]:
        """``(mode, ttl)``, cached for ``mode_cache_s``.

        ``scope_locks_mode()`` re-reads (and YAML-parses) ``parallelism.yaml`` on
        every call whenever the env override is unset, and this is on the 2 Hz
        per-session path. The cache bounds how long the ``OMNIAGENTOS_SCOPE_LOCKS``
        kill switch takes to bite; ``mode_cache_s=0`` disables it entirely, which
        is what the tests use.
        """
        now = time.monotonic()
        if self._mode_cached is not None and now < self._mode_expires:
            return self._mode_cached
        resolved = (scope_locks_mode(), scope_ttl_s())
        self._mode_cached = resolved
        self._mode_expires = now + self._mode_cache_s
        return resolved

    @property
    def mode(self) -> ScopeLockMode:
        """The effective mode, through the cache."""
        return self._resolved()[0]

    @property
    def armed(self) -> bool:
        """True when this gate may write anything at all (mode is not ``off``)."""
        return self.mode != "off"

    def _store(self) -> PathLockStore:
        """The lock store, built lazily so ``off`` never constructs one."""
        if self._locks is None:
            self._locks = PathLockStore(self._dal)
        return self._locks

    # --- per-session worktree (T3.8) ----------------------------------------

    def worktrees_enabled(self) -> bool:
        """Is the per-session worktree flag on? One env read; nothing else."""
        return lane_worktrees_enabled(WORKTREE_LANE)

    def _lane_worktrees(self) -> LaneWorktrees:
        if self._lane is None:
            self._lane = lane_worktrees(WORKTREE_LANE)
        return self._lane

    def worktree_for(self, session_id: str) -> LaneWorktree | None:
        """The worktree this supervisor activated for a session (diagnostics)."""
        return self._worktrees.get(str(session_id))

    def working_dir_for(
        self,
        session_id: str,
        project_dir: str,
        *,
        title: Any = None,
        recorded_scope_json: Any = None,
    ) -> str:
        """Where this session should RUN — its private worktree, or ``project_dir``.

        Independent of :func:`scope_locks_mode` on purpose: isolation and
        arbitration are two mechanisms with two flags, and a host may reasonably
        want worktrees while the lock soak is still off. What it is NOT
        independent of is the derivation: the caller must pass the returned
        directory back in as ``project_dir`` (and grant it), because
        :func:`claims_for_roots` keys the claim on the roots the row carries and
        a session that claims a worktree while running in the project is the one
        outcome worse than no isolation at all.

        LANE-OWNED SESSIONS GET NOTHING. A ``[longhaul:...]``/``[swarm:...]``
        session's owning lane already made a worktree for the work — longhaul's
        is per BOARD TASK and outlives this attempt — and nesting a session
        worktree inside it would split the attempt chain's tree in two. Same
        exemption, and the same predicate, as the lock claim.
        """
        sid = str(session_id or "")
        if not sid or is_lane_owned(title):
            return project_dir
        live = self._worktrees.get(sid)
        if live is not None:
            return live.path
        record = decode_worktree(recorded_scope_json)
        if record is None and not self.worktrees_enabled():
            return project_dir
        lane = self._lane_worktrees()
        try:
            activation: Activation = lane.activate(sid, project_dir, record=record)
        except Exception:  # noqa: BLE001 -- isolation is never a precondition
            LOG.warning("session worktree activation failed for %s", sid, exc_info=True)
            return project_dir
        if activation.record is not None:
            self._persist_scope(
                sid,
                encode_scope(decode_scope(recorded_scope_json), worktree=activation.record),
            )
        if activation.worktree is None:
            return project_dir
        self._worktrees[sid] = activation.worktree
        return activation.worktree.path

    def integrate(self, session_id: str) -> Integration | None:
        """Merge this session's worktree branch back, then remove the worktree.

        Exactly-once per process: the handle is popped before any git call, so
        the terminal transition and a later idempotent release cannot both merge.
        A skipped merge (busy commit lock, unreadable HEAD) KEEPS the worktree —
        removing it would destroy the only copy of the work.
        """
        sid = str(session_id)
        worktree = self._worktrees.pop(sid, None)
        if worktree is None:
            return None
        lane = self._lane_worktrees()
        result = lane.integrate(
            worktree,
            # Only an ARMED gate hands over a lock store: with locking off the
            # merge takes the in-process lock alone (what this lane had before
            # Phase 3), and constructing a store here would break the "off
            # builds nothing" property the dark-path test asserts.
            locks=self._store() if self.armed else None,
            holder=session_holder(sid),
            generation=SESSION_GENERATION,
            wait_s=MERGE_WAIT_S,
        )
        if result.status == "skipped":
            # The tree is KEPT (removing an unmerged worktree destroys the only
            # copy of the work) but the handle is NOT put back: ``release`` runs
            # for every terminal row on every poll, so a retry here would put a
            # git merge on the 2 Hz loop. The record on ``scope_json`` is what
            # survives, and this line is what an operator acts on.
            LOG.error(
                "session %s: worktree %s kept — merge skipped (%s)",
                sid,
                worktree.path,
                result.detail,
            )
            return result
        outcome = lane.finish(worktree)
        if outcome is not None and outcome.status == "salvage_failed":
            LOG.error(
                "session %s: worktree %s left in place — salvage failed on a dirty tree",
                sid,
                worktree.path,
            )
        return result

    # --- acquire ------------------------------------------------------------

    def open_for_launch(
        self,
        session_id: str,
        *,
        title: Any = None,
        project_dir: str = "",
        granted_roots: Sequence[str] | None = None,
        declared_scope: Sequence[ScopeClaim] | None = None,
        recorded_scope_json: Any = None,
    ) -> ScopeDecision:
        """Declare and claim one session's scope. Call with NO transaction open.

        ``declared_scope`` overrides the derivation; when it is ``None`` the claim
        set comes from ``project_dir`` + ``granted_roots``, which are the same two
        values frozen onto the row, so the grant and the claim cannot disagree.

        Persists ``sessions.scope_json`` BEFORE acquiring: the declaration is
        what the session said it would touch and is worth recording whether or
        not the claim was granted. ``recorded_scope_json`` is what the row already
        holds, when the caller happens to know — a blocked session comes back
        through here on every ingress pass, and re-writing an identical column is
        a write transaction bought for nothing.
        """
        mode = self._resolved()[0]
        if mode == "off" or not session_id:
            return _OFF
        if is_lane_owned(title):
            return ScopeDecision(outcome="exempt", mode=mode)
        # Past the dark gate, so this costs nothing that matters — and it is not
        # conditioned on the worktree flag on purpose: ``var/sessions/worktrees``
        # is private as a matter of fact, and the case that needs the
        # registration most is a supervisor whose flag is OFF adopting a session
        # with a RECORDED worktree. Unregistered, that path folds into the
        # enclosing repo's realm and the session would claim the whole checkout.
        self._lane_worktrees().register_realm_base()
        claims = (
            tuple(declared_scope)
            if declared_scope is not None
            else claims_for_roots([project_dir, *(granted_roots or ())])
        )
        if not claims:
            return ScopeDecision(outcome="undeclared", mode=mode)
        # Carry the T3.8 registration through: this column holds the declaration
        # AND the recorded worktree mode, so re-serializing without the record
        # would erase it and let the next process re-resolve the mode from the
        # ambient flag — the mid-flight flip m6 exists to prevent.
        payload = encode_scope(claims, worktree=decode_worktree(recorded_scope_json))
        if payload != recorded_scope_json:
            self._persist_scope(session_id, payload)
        return self._acquire(session_id, claims, replace=False)

    def open_for_row(
        self,
        session: Mapping[str, Any],
        *,
        declared_scope: Sequence[ScopeClaim] | None = None,
    ) -> ScopeDecision:
        """:meth:`open_for_launch` for a session row already on disk.

        The durable spawn ingress' entry point: it has a row and no spawn
        arguments, so the claim set must come from the row's own persisted
        ``project_dir`` + ``granted_roots``. Requires a FULL row (``get_session``,
        which is ``SELECT *``); ``list_sessions``' narrower projection has no
        ``scope_json``, which would only cost a redundant re-write here but
        matters in :meth:`adopt`.
        """
        if not self.armed:
            return _OFF
        return self.open_for_launch(
            str(session.get("id") or ""),
            title=session.get("title"),
            project_dir=str(session.get("project_dir") or ""),
            granted_roots=decode_granted_roots(session),
            declared_scope=declared_scope,
            recorded_scope_json=session.get("scope_json"),
        )

    def adopt(self, session: Mapping[str, Any]) -> ScopeDecision:
        """Re-take a live session's scope after a SUPERVISOR restart.

        Uses ``reacquire_scope``, not ``try_acquire_scope``: the previous
        incarnation's rows may still be live, and the resumed supervisor must end
        up holding exactly the set it re-declares rather than the union of that
        and whatever the dead process had taken.

        Best-effort by contract. A refusal here means somebody else legitimately
        took the paths while this supervisor was down; the adopted process is
        already running and killing it would be a larger harm than the race, so
        the outcome is logged and reconcile continues.

        The claim set prefers the row's RECORDED ``scope_json`` — replaying what
        the session actually declared, rather than re-deriving it, is the point of
        persisting it. ``list_sessions`` (which is what ``reconcile`` iterates)
        projects a fixed column list that does not include ``scope_json``, so in
        practice the derivation from ``project_dir`` + ``granted_roots`` is what
        runs; both produce the same set for a session that never had an explicit
        ``declared_scope``, and the recorded value takes over for free the day
        that projection widens.
        """
        mode = self._resolved()[0]
        if mode == "off":
            return _OFF
        session_id = str(session.get("id") or "")
        if not session_id or is_lane_owned(session.get("title")):
            return ScopeDecision(outcome="exempt", mode=mode)
        claims = decode_scope(session.get("scope_json")) or claims_for_session(session)
        if not claims:
            return ScopeDecision(outcome="undeclared", mode=mode)
        decision = self._acquire(session_id, claims, replace=True)
        if not decision.may_launch:
            LOG.warning(
                "scope adopt refused %s",
                json.dumps(
                    {
                        "event": "scope.adopt_refused",
                        "session_id": session_id,
                        "outcome": decision.outcome,
                        "detail": decision.detail,
                    },
                    sort_keys=True,
                ),
            )
        return decision

    def _acquire(
        self, session_id: str, claims: tuple[ScopeClaim, ...], *, replace: bool
    ) -> ScopeDecision:
        """One all-or-nothing acquire plus the park/clear bookkeeping.

        ``enforce`` is deliberately NOT passed: ``try_acquire_scope(enforce=None)``
        resolves :func:`scope_locks_mode` itself, and the ``mode`` on the result
        is the authoritative one. If the operator flipped the kill switch between
        this gate's cached read and the call, the store returns ``disabled`` and
        this reports ``off`` — the switch wins, which is the entire reason it
        exists.
        """
        holder = session_holder(session_id)
        store = self._store()
        try:
            acquire = store.reacquire_scope if replace else store.try_acquire_scope
            result = acquire(claims, holder, generation=SESSION_GENERATION)
        except (ScopeLockError, sqlite3.Error) as exc:
            # FAIL CLOSED UNDER ENFORCE, OPEN UNDER SHADOW. Enforcement that
            # silently degrades to "no locks" the moment the database hiccups is
            # not enforcement; a shadow soak that blocks launches is not a shadow.
            mode = self._resolved()[0]
            LOG.warning(
                "scope acquire failed %s",
                json.dumps(
                    {
                        "event": "scope.acquire_error",
                        "session_id": session_id,
                        "mode": mode,
                        "error": str(exc) or type(exc).__name__,
                    },
                    sort_keys=True,
                ),
            )
            if mode == "enforce":
                return ScopeDecision(outcome="parked", mode=mode, claims=claims, detail=str(exc))
            return ScopeDecision(outcome="undeclared", mode=mode, claims=claims)

        if result.status == "disabled":
            return _OFF
        if result.status == "fenced":
            return ScopeDecision(
                outcome="fenced",
                mode=result.mode,
                claims=claims,
                result=result,
                detail=f"{HOLDER_KIND}:{session_id} was adopted away",
            )
        if result.status == "blocked":
            waiter_id = self._park(session_id, claims, result)
            self._forget(session_id)
            LOG.info(
                "scope blocked %s",
                json.dumps(
                    {
                        "event": "scope.blocked",
                        "session_id": session_id,
                        "blocked_on": result.blocked_on,
                        "blocked_path": result.blocked_path,
                        "waiter_id": waiter_id,
                    },
                    sort_keys=True,
                ),
            )
            return ScopeDecision(
                outcome="parked",
                mode=result.mode,
                claims=claims,
                result=result,
                waiter_id=waiter_id,
                detail=result.conflict.describe() if result.conflict else "",
            )

        # Granted. Clear any park this session left behind on an earlier pass,
        # or it sits in the realm's FIFO forever pretending to still be waiting.
        self._clear_waiter(session_id)
        self._holding.add(session_id)
        self._last_renew[session_id] = time.monotonic()
        if result.conflict is not None:
            # Shadow mode: the row set was written anyway and the collision that
            # enforcement WOULD have refused is the entire product of the soak.
            LOG.info(
                "scope shadow conflict %s",
                json.dumps(
                    {
                        "event": "scope.shadow_conflict",
                        "session_id": session_id,
                        "blocked_on": result.blocked_on,
                        "blocked_path": result.blocked_path,
                        "skipped": list(result.skipped_duplicates),
                    },
                    sort_keys=True,
                ),
            )
            return ScopeDecision(
                outcome="shadowed",
                mode=result.mode,
                claims=claims,
                result=result,
                detail=result.conflict.describe(),
            )
        return ScopeDecision(outcome="granted", mode=result.mode, claims=claims, result=result)

    # --- renew --------------------------------------------------------------

    def renew(self, session: Mapping[str, Any]) -> bool:
        """Extend this session's lease. Call every poll; writes at most once per
        third of the TTL.

        Returns True when the lease is (still) held. A session this process has
        no record of holding locks for is skipped before any store call — that is
        the common case on the poll loop and it must not cost a write.

        A renew that matches nothing means the lease LAPSED: the supervisor was
        stalled for longer than the whole TTL, which is why this takes the ROW and
        not just an id — the recovery is one :meth:`adopt`, and adopt needs the
        declaration. If that re-acquire is refused (somebody legitimately took the
        paths during the stall) the session keeps running and the loss is logged
        rather than escalated. Killing a live session because its own supervisor
        was slow trades a rare race for a certain, self-inflicted failure; the
        logged event is what an operator escalates on.

        The recovery cannot loop: a refused adopt leaves the session out of the
        held-set, so the next poll skips it at the first line.
        """
        session_id = str(session.get("id") or "")
        if session_id not in self._holding:
            # The held-set is empty while the mode is off, so this is also the
            # off-path short circuit — and it is a set lookup, not a config read.
            return False
        mode, ttl = self._resolved()
        if mode == "off":
            return False
        now = time.monotonic()
        interval = max(1.0, ttl / _RENEW_FRACTION)
        last = self._last_renew.get(session_id)
        if last is not None and now - last < interval:
            return True
        try:
            renewed = self._store().renew_scope(HOLDER_KIND, session_id, SESSION_GENERATION)
        except (ScopeLockError, sqlite3.Error) as exc:
            LOG.debug("scope renew failed for %s: %s", session_id, exc)
            return False
        if renewed:
            self._last_renew[session_id] = now
            return True
        LOG.warning(
            "scope lease lapsed %s",
            json.dumps(
                {"event": "scope.lease_lapsed", "session_id": session_id, "mode": mode},
                sort_keys=True,
            ),
        )
        self._forget(session_id)
        return self.adopt(session).holds_locks

    # --- release ------------------------------------------------------------

    def release(self, session_id: str) -> int:
        """Drop this session's locks and its park row. Idempotent.

        Called from every terminal path, including the poll loop's terminal
        branch, which runs for every terminal row on every pass — hence the
        ``_holding`` guard: a session this process never claimed for, or already
        released, costs nothing, and while the mode is off that set is empty so
        this is also the off-path short circuit.

        Note what is deliberately NOT here: a mode check. If an operator throws
        the kill switch while sessions are holding locks, those locks must still
        be given back at the end. "Turn it off" cannot mean "and strand every lock
        the fleet is currently holding until its TTL expires".

        It is also where a per-session WORKTREE is merged back and removed, and
        the order is load-bearing: the merge happens while the session still owns
        its private realm, and only then are the locks given back.
        """
        if session_id in self._worktrees:
            self.integrate(session_id)
        if session_id not in self._holding:
            return 0
        self._forget(session_id)
        released = 0
        try:
            released = self._store().release_scope(HOLDER_KIND, session_id, SESSION_GENERATION)
        except (ScopeLockError, sqlite3.Error) as exc:
            # The TTL is the backstop: an unreleased lock lapses on its own, so a
            # failed release costs a delay, never a permanently wedged path.
            LOG.warning("scope release failed for %s: %s", session_id, exc)
        self._clear_waiter(session_id)
        return released

    # --- internals ----------------------------------------------------------

    def _forget(self, session_id: str) -> None:
        self._holding.discard(session_id)
        self._last_renew.pop(session_id, None)

    def _park(
        self,
        session_id: str,
        claims: tuple[ScopeClaim, ...],
        result: AcquireResult,
    ) -> int:
        """Park a durable waiter for the realm the refusal happened in.

        The realm comes from the OBSERVED conflict, not from the first claim: a
        session that claims two realms and is refused in the second must queue in
        the second, or it waits behind a queue it was never blocked by.
        """
        conflict = result.conflict
        realm = conflict.candidate.realm if conflict is not None else claims[0].realm
        try:
            return self._store().park_waiter(
                realm,
                session_holder(session_id),
                blocked_on=result.blocked_on,
                blocked_path=result.blocked_path,
            )
        except (ScopeLockError, sqlite3.Error) as exc:
            # The park row is the FIFO's bookkeeping, not the refusal itself. The
            # refusal already stands; losing the row costs fairness, not safety.
            LOG.warning("scope park failed for %s: %s", session_id, exc)
            return 0

    def _clear_waiter(self, session_id: str) -> None:
        try:
            self._store().clear_waiter(HOLDER_KIND, session_id)
        except (ScopeLockError, sqlite3.Error) as exc:
            LOG.debug("scope waiter clear failed for %s: %s", session_id, exc)

    def _persist_scope(self, session_id: str, payload: str | None) -> None:
        """Write ``sessions.scope_json``. Call with NO transaction open.

        A direct ``UPDATE`` rather than a column on ``create_session`` because
        ``dal._SESSION_COLUMNS`` is that method's insert allowlist and this work
        package does not own ``sessions/dal.py``; see the follow-up note in the
        landing summary. The write is deliberately SEPARATE from the acquire —
        the lock store opens its own ``BEGIN IMMEDIATE``, so folding this into it
        would be the re-entrancy bug this class's docstring warns about.

        Best-effort: ``scope_json`` is a record for Phase 4's declared-vs-actual
        diff and for crash-resume, while ``resource_locks`` is the mechanism. A
        failure to record must not refuse a launch.
        """
        dal = self._dal
        try:
            with dal._lock:
                dal._begin()
                try:
                    dal._connection.execute(
                        "UPDATE sessions SET scope_json = ? WHERE id = ?",
                        (payload, session_id),
                    )
                    dal._commit()
                except BaseException:
                    dal._rollback()
                    raise
        except sqlite3.Error as exc:
            LOG.warning("could not persist scope_json for %s: %s", session_id, exc)
