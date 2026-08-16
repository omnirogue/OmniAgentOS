"""Runner-lane wiring for the Phase-3 scope kernel.

The kernel (``omniagentos/scope/*``, migration 059) decides *who owns which
paths*. This module is the runner's half of the contract — the five verbs a lane
has to implement, and nothing else:

DECLARE
    ``runs.scope_json``, written the moment this worker takes a run (the runner's
    "run creation": a run row is authored by the control plane, but it acquires a
    runner-lane identity only when a worker claims or adopts it). A resume reads
    the declaration back rather than re-deriving it, so a run that comes back
    from a restart re-claims exactly what it claimed before.

CLAIM
    ``Runner._select`` — see :meth:`RunnerScope.take`.

RENEW
    ``Runner._heartbeat_during_step`` already pulses at ``stale_s/3``; the lock
    lease renews on the same pulse and ``runs.lease_expires_at`` moves with it.

RELEASE
    ``Runner._transition`` on every terminal state, plus a belt-and-braces sweep
    in ``Runner.execute_run``'s ``finally`` so a fault that never reaches
    ``_transition`` still gives the paths back.

FENCE
    ``Runner._assert_fence`` gains the generation predicate, so a
    displaced-but-alive worker fails its next fence check exactly like a
    displaced swarm coordinator does (migration 052's shape, applied to ``runs``).

WHAT THE RUNNER ACTUALLY CLAIMS (v1)
====================================
**A realm-ROOT lock on the run's working dir. This is per-PROJECT serialization,
not per-file.** The runner has no declared file scope today — a plan step names a
``working_dir`` and an adapter then writes wherever it likes inside it — so there
is no honest way to name individual files up front. Claiming the realm root is
the only claim that is *true*: it says "this run may touch anything under here",
which is exactly what a coding agent pointed at a repo may do.

The consequence is deliberate and must not be mistaken for a bug: two runs whose
working dirs resolve to the SAME realm (the same git checkout) will serialize
against each other in enforce mode. Intra-project parallelism comes back with
T3.8's per-run git worktrees — a worktree is its own realm (see
``scope.paths.private_workspace_bases``), so two runs in two worktrees of one
repo stop colliding without the claim algebra above changing at all.

PER-RUN WORKTREES (T3.8)
========================
:meth:`RunnerScope.working_dir_for` is the seam that delivers that. It activates
``var/runs/worktrees/<run_id>/main`` on branch ``run/<run_id>/main``, records the
resolved mode on ``runs.scope_json`` (the RECORDED value then wins over the
ambient flag forever after — see ``worktrees.lanes``), and hands back the
directory the run should execute in. ``derive_claims`` is unchanged and does not
need to change: it claims ``realm_of(working_dir)``, and once the lane's
worktrees base is registered as a private base that realm is the WORKTREE's, not
the project's — a claim nobody else can name.

The two halves must move together. A run that CLAIMS its worktree realm while
still EXECUTING in the shared project has swapped a serialized-but-correct lane
for a parallel-and-wrong one, so ``working_dir_for`` returns the value used for
both, and the flag (``OMNIAGENTOS_WORKTREES_RUNNER``, default 0) is what turns
both on at once.

Runs that were never pointed at a project are unaffected either way: their
working dir is ``<workspace_base>/<run_id>``, which ``realm_of`` resolves to a
PRIVATE realm per run, so their claims can never collide with anything.

SHIPS DARK
==========
``scope_locks_mode()`` defaults to ``off`` and every entry point below returns
before touching the store in that mode. With the feature off this module performs
exactly one env/config read per runner pass and makes zero database writes —
``tests/runner/test_scope_wiring.py`` asserts that by recording every store call.

THE RE-ENTRANCY HAZARD
======================
``PathLockStore._write`` opens its own ``BEGIN IMMEDIATE`` under the store's
(re-entrant) writer lock. Calling any method here from INSIDE a transaction the
caller opened would re-enter that lock and raise "cannot start a transaction
within a transaction" — a bug that only shows up under load. The runner never
opens a raw transaction (it goes through ``Store`` methods, each of which commits
before returning), so every call below is outside one. Keep it that way.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from omniagentos.contracts import TERMINAL_RUN_STATES, RunState, Store
from omniagentos.scope.config import (
    scope_locks_mode,
    scope_starvation_s,
    scope_ttl_s,
)
from omniagentos.scope.locks import (
    AcquireResult,
    HolderKind,
    Lane,
    LockHolder,
    PathLockStore,
)
from omniagentos.scope.model import DEFAULT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import realm_of
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
    "SCOPE_JSON_VERSION",
    "WORKTREE_LANE",
    "RunnerScope",
    "ScopeDecision",
    "ScopeStatus",
    "claims_to_json",
    "derive_claims",
    "json_to_claims",
    "json_to_worktree",
]

#: ``resource_locks.holder_kind`` / ``lane`` for this lane. A runner lock is held
#: by the RUN, not by the worker: adoption changes the worker but not the run, so
#: keying on the run is what makes an adopted run keep its own scope (and what
#: makes ``reacquire_scope`` the right verb on every resume path).
HOLDER_KIND: HolderKind = "run"
LANE: Lane = "runner"

#: Envelope version for ``runs.scope_json``. Anything else is ignored and the
#: scope is re-derived, so a future format change degrades instead of exploding.
SCOPE_JSON_VERSION = 1

#: The ``worktrees.lanes`` binding this lane uses (``var/runs/worktrees/...``).
WORKTREE_LANE = "runner"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

ScopeStatus = Literal["disabled", "granted", "blocked", "fenced", "error"]


# ---------------------------------------------------------------------------
# Declaration: claims <-> runs.scope_json
# ---------------------------------------------------------------------------


def claims_to_json(
    claims: tuple[ScopeClaim, ...], *, worktree: Mapping[str, Any] | None = None
) -> str:
    """Serialize a declared claim set for ``runs.scope_json``.

    ``worktree`` is T3.8's recorded registration (``worktrees.lanes``). With it
    absent — the shipped default, because the flag is off — the emitted bytes are
    IDENTICAL to what this function produced before per-run worktrees existed,
    which is what makes the dark path a byte-for-byte claim rather than a
    behavioural one. The key sits beside ``claims`` rather than inside them
    because it is not a claim: it is the record of WHERE the run runs.
    """
    payload: dict[str, Any] = {
        "v": SCOPE_JSON_VERSION,
        "claims": [
            {
                "realm": claim.realm,
                "path": claim.path_text,
                "kind": claim.kind,
                "purpose": claim.purpose,
            }
            for claim in claims
        ],
    }
    if worktree:
        payload[WORKTREE_RECORD_KEY] = dict(worktree)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def json_to_worktree(raw: Any) -> dict[str, Any] | None:
    """The recorded worktree registration inside ``runs.scope_json``, or ``None``.

    Total for the same reason :func:`json_to_claims` is: a run must never be
    undispatchable because its own bookkeeping is unreadable. An unreadable
    record simply means "nothing recorded", and the caller re-resolves from the
    ambient flag — which is the correct degradation, because a run with no
    record has no worktree either.
    """
    if not raw:
        return None
    try:
        payload = raw if isinstance(raw, dict) else json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    record = payload.get(WORKTREE_RECORD_KEY)
    return dict(record) if isinstance(record, dict) else None


def json_to_claims(raw: Any) -> tuple[ScopeClaim, ...]:
    """Parse ``runs.scope_json`` back into claims; ``()`` for anything unusable.

    Deliberately total: a malformed, truncated or future-versioned declaration
    returns empty rather than raising, and the caller then re-derives. A resume
    must never be blocked by its own bookkeeping.
    """
    if not raw:
        return ()
    try:
        payload = raw if isinstance(raw, dict) else json.loads(str(raw))
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict) or int(payload.get("v") or 0) != SCOPE_JSON_VERSION:
        return ()
    entries = payload.get("claims")
    if not isinstance(entries, list):
        return ()
    claims: list[ScopeClaim] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return ()
        try:
            claims.append(
                ScopeClaim.for_path(
                    str(entry["realm"]),
                    str(entry.get("path", ".")),
                    kind=str(entry.get("kind", "root")),  # type: ignore[arg-type]
                    purpose=str(entry.get("purpose", DEFAULT_PURPOSE)),
                )
            )
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(claims)


def working_dirs_for_run(
    run: dict[str, Any], task_input: dict[str, Any], fallback: str
) -> list[str]:
    """Every directory this run has declared it will work in, best-effort.

    Sources, in the order the runner itself consults them (see
    ``Runner._execute_agent``): each plan step's ``params.working_dir``, then the
    task's ``input_json.working_dir``, then the per-run workspace. A plan whose
    steps name two different dirs yields two — one realm-root claim each, taken
    all-or-nothing, which is the honest reading of "this run will touch both".
    """
    found: list[str] = []
    plan_raw = run.get("plan_json")
    try:
        plan = plan_raw if isinstance(plan_raw, list) else json.loads(str(plan_raw or "[]"))
    except (TypeError, ValueError):
        plan = []
    if isinstance(plan, list):
        for step in plan:
            if not isinstance(step, dict):
                continue
            params = step.get("params")
            if not isinstance(params, dict):
                continue
            value = params.get("working_dir")
            if value and str(value).strip():
                found.append(str(value))
    declared = task_input.get("working_dir")
    if declared and str(declared).strip():
        found.append(str(declared))
    found.append(fallback)
    ordered: list[str] = []
    for value in found:
        if value not in ordered:
            ordered.append(value)
    return ordered


def derive_claims(
    run: dict[str, Any], task_input: dict[str, Any], workspace_path: str
) -> tuple[ScopeClaim, ...]:
    """The v1 runner claim set: one realm-ROOT claim per distinct working dir.

    ``components=()`` with ``kind='root'`` is the whole realm — the broadest claim
    the algebra has, and the only one that does not lie about what a coding agent
    will touch. See the module docstring for why per-file claims are not
    available to this lane yet and what restores intra-project parallelism.
    """
    claims: dict[str, ScopeClaim] = {}
    # ``working_dirs_for_run`` always ends with the per-run workspace, and
    # ``realm_of`` only fails on an empty path, so the result is never empty in
    # practice — a run always arbitrates against at least its own workspace realm.
    for directory in working_dirs_for_run(run, task_input, workspace_path):
        realm = realm_of(directory)
        if realm is None or realm in claims:
            continue
        claims[realm] = ScopeClaim(
            realm=realm,
            components=(),
            kind="root",
            purpose=DEFAULT_PURPOSE,
        )
    return tuple(claims.values())


# ---------------------------------------------------------------------------
# The decision handed back to Runner._select
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """What the lane must do with one run.

    ``proceed`` is the only thing the happy path reads. ``starved`` is the
    anti-starvation break: skipping a blocked run turns strict FIFO into
    skip-the-blocked-head, and a head that has been parked longer than
    ``OMNIAGENTOS_SCOPE_STARVATION_S`` sets this so the lane STOPS taking other
    work instead of walking past it forever.
    """

    status: ScopeStatus
    realm: str = ""
    generation: int = 0
    detail: str = ""
    blocked_on: str = ""
    blocked_path: str = ""
    waited_s: float = 0.0
    starved: bool = False
    shadowed: bool = False
    report: bool = False

    @property
    def proceed(self) -> bool:
        """May the caller execute this run? True when off, granted, or shadowed."""
        return self.status in ("disabled", "granted")


_DISABLED = ScopeDecision(status="disabled")


# ---------------------------------------------------------------------------
# The lane wiring
# ---------------------------------------------------------------------------


class RunnerScope:
    """One per :class:`~omniagentos.runner.core.Runner`; holds the lease state.

    Construction touches nothing: no config read, no database access, no lock
    store. A worker running with the feature off must be indistinguishable from a
    worker built before this module existed.
    """

    def __init__(self, store: Store, *, worker_id: str, workspace_base: str) -> None:
        self._store = store
        self._worker_id = worker_id
        self._workspace_base = workspace_base
        self._locks: PathLockStore | None = None
        self._locks_unavailable = False
        # The generation this worker believes it holds, per run. EMPTY is the dark
        # path: every hot-path entry point (fence, renew, release) checks this
        # dict first, so with the feature off they cost one dict lookup and never
        # read config or touch the store.
        self._generations: dict[str, int] = {}
        # Runs whose renew came back False in ENFORCE mode: they were adopted away
        # or their lease lapsed, and the next fence check must fail.
        self._displaced: set[str] = set()
        self._parked: set[str] = set()
        # Last reported (status, blocker) per run, so a run that stays blocked for
        # a hundred ticks produces one event, not a hundred.
        self._reported: dict[str, str] = {}
        # T3.8: run_id -> the private worktree this worker activated for it.
        # EMPTY on the dark path (and while the worktree flag is off), which is
        # what lets ``release_if_terminal`` keep its single-lookup early return.
        self._worktrees: dict[str, LaneWorktree] = {}
        self._lane: LaneWorktrees | None = None
        # Selection runs on the scheduler thread, renew on the per-step heartbeat
        # threads, and release on whichever pool slot finished the run — so every
        # one of the dicts above is cross-thread and all four live under this lock.
        self._state_lock = threading.Lock()

    # --- mode -------------------------------------------------------------

    def enabled(self) -> bool:
        """Is the mechanism on at all? Read ONCE per selection pass by the caller.

        Read fresh rather than cached, because ``OMNIAGENTOS_SCOPE_LOCKS=off`` is
        the emergency kill switch and must take effect on a LIVE worker without a
        restart.
        """
        return scope_locks_mode() != "off"

    def generation_for(self, run_id: str) -> int | None:
        """The lease generation this worker holds for ``run_id`` (tests/diagnostics)."""
        with self._state_lock:
            return self._generations.get(run_id)

    # --- per-run worktree (T3.8) -------------------------------------------

    def worktrees_enabled(self) -> bool:
        """Is the per-run worktree flag on? One env read; nothing else."""
        return lane_worktrees_enabled(WORKTREE_LANE)

    def _lane_worktrees(self) -> LaneWorktrees:
        if self._lane is None:
            self._lane = lane_worktrees(WORKTREE_LANE)
        return self._lane

    def worktree_for(self, run_id: str) -> LaneWorktree | None:
        """The worktree this worker activated for ``run_id`` (tests/diagnostics)."""
        with self._state_lock:
            return self._worktrees.get(run_id)

    def working_dir_for(self, run: dict[str, Any], default_dir: str) -> str:
        """Where this run should EXECUTE — its private worktree, or ``default_dir``.

        Call this before the run's working dir is used for anything: it is both
        the directory the adapter runs in and the directory
        :func:`derive_claims` keys the run's claim on, and those two must be the
        same directory or the claim is a lie (see the module docstring).

        Never raises and never blocks a run: a flag that is off, a workspace git
        cannot make a worktree in, an unusable run id — all return ``default_dir``
        unchanged, which is exactly what this lane did before T3.8.

        The RECORDED mode wins over the ambient flag. A run that was activated in
        worktree mode keeps running in its worktree even in a process whose flag
        was since turned off (its branch and its uncommitted work are there), and
        a run recorded as same-directory never flips into a worktree mid-flight
        (its locks were sized for the shared realm).
        """
        run_id = str(run.get("id") or "")
        if not run_id:
            return default_dir
        with self._state_lock:
            live = self._worktrees.get(run_id)
        if live is not None:
            return live.path
        record = json_to_worktree(run.get("scope_json"))
        if record is None and not self.worktrees_enabled():
            # Nothing recorded and the flag is off: the dark path, and it costs
            # one env read. No git, no realpath, no database write.
            return default_dir
        lane = self._lane_worktrees()
        try:
            activation: Activation = lane.activate(run_id, default_dir, record=record)
        except Exception:  # noqa: BLE001 -- isolation is never a precondition
            LOG.exception("runner worktree activation failed for run %s", run_id)
            return default_dir
        if activation.record is not None:
            self._persist_worktree(run_id, activation.record)
        if activation.worktree is None:
            return default_dir
        with self._state_lock:
            self._worktrees[run_id] = activation.worktree
        return activation.worktree.path

    def _persist_worktree(self, run_id: str, record: Mapping[str, Any]) -> None:
        """Record the registration on ``runs.scope_json``, preserving the claims.

        Read-modify-write, and the read is FRESH: ``take`` writes the claim set
        into the same column earlier in the pass, so re-serializing the caller's
        (older) copy of the row would silently drop the declaration this run is
        currently holding locks under.

        Best-effort. ``resource_locks`` is the mechanism and this column is the
        record; a failed write costs the m6 guarantee for one run — it will
        re-resolve from the ambient flag next time — and that beats refusing to
        run it.
        """
        try:
            row = self._store.get_run(run_id) or {}
            claims = json_to_claims(row.get("scope_json"))
            self._store.update_run(
                run_id,
                {"scope_json": claims_to_json(claims, worktree=record)},
                expect_worker=self._worker_id,
            )
        except Exception:  # noqa: BLE001 -- a record is not a precondition
            LOG.exception("runner could not record the worktree for run %s", run_id)

    def integrate(self, run_id: str) -> Integration | None:
        """Merge this run's worktree branch back, then remove the worktree.

        Exactly-once per process: the worktree is POPPED under the state lock
        before anything git-side happens, so a terminal transition and the
        ``finally`` crash net cannot both merge the same branch (a second
        ``--no-ff`` merge of an already-merged sha is a noop, but a second
        *removal* would race the first one's salvage).

        A conflict is not a failure of this method: the branch is left alive and
        named, ``merge --abort`` has already restored the shared workspace, and
        the worktree is still removed WITH SALVAGE so nothing uncommitted is lost.
        """
        with self._state_lock:
            worktree = self._worktrees.pop(run_id, None)
        if worktree is None:
            return None
        lane = self._lane_worktrees()
        with self._state_lock:
            generation = self._generations.get(run_id) or 0
        result = lane.integrate(
            worktree,
            # Only build a lock store when locking is ON: with it off the merge
            # takes the in-process lock alone (what this lane had before Phase 3)
            # and the "off constructs nothing" property survives.
            locks=self._lock_store() if scope_locks_mode() != "off" else None,
            holder=LockHolder(kind=HOLDER_KIND, id=run_id, lane=LANE),
            generation=generation,
        )
        if result.status == "skipped":
            # The merge never happened (busy commit lock, unreadable HEAD).
            # Removing now would destroy the only copy of the work, and the
            # handle goes BACK so the next terminal path — ``_transition`` and
            # the ``finally`` crash net both reach here — gets another attempt.
            with self._state_lock:
                self._worktrees[run_id] = worktree
            LOG.warning(
                "run %s: worktree %s kept — merge skipped (%s)",
                run_id,
                worktree.path,
                result.detail,
            )
            return result
        outcome = lane.finish(worktree)
        if outcome is not None and outcome.status == "salvage_failed":
            LOG.error(
                "run %s: worktree %s left in place — salvage failed on a dirty tree",
                run_id,
                worktree.path,
            )
        return result

    # --- claim ------------------------------------------------------------

    def take(self, run: dict[str, Any], *, resume: bool) -> ScopeDecision:
        """Declare, fence and acquire this run's scope. Never raises.

        ``resume=True`` uses :meth:`PathLockStore.reacquire_scope`: the holder
        identity is the RUN, so a worker that restarted (or adopted the run from a
        dead worker) is the same holder coming back, and REPLACE semantics are
        what make it end up holding exactly the set it just re-declared rather
        than the union with whatever its previous incarnation had. ``resume=False``
        is the fresh claim out of ``claim_next_run``.

        Every failure mode is mapped, never raised:

        * ``disabled`` — the feature is off (or the store has no lock surface).
        * ``granted``  — proceed. In shadow mode a conflict may still be attached.
        * ``blocked``  — refused; a durable waiter is parked and the caller skips.
        * ``fenced``   — this identity has been adopted away; the caller skips.
        * ``error``    — the lock layer itself failed. In ENFORCE that refuses the
          run (executing unarbitrated is the thing enforcement exists to prevent);
          in shadow it degrades to ``disabled``, because shadow must never change
          a decision.
        """
        run_id = str(run.get("id") or "")
        if not run_id:
            return _DISABLED
        if scope_locks_mode() == "off":
            self._forget(run_id)
            return _DISABLED
        locks = self._lock_store()
        if locks is None:
            return _DISABLED
        try:
            return self._take(locks, run, run_id, resume=resume)
        except Exception as exc:  # noqa: BLE001 -- mapped to a decision, never raised
            LOG.exception("runner scope acquire failed for run %s", run_id)
            if scope_locks_mode() == "enforce":
                return self._decide(
                    run_id, ScopeDecision(status="error", detail=f"{type(exc).__name__}: {exc}")
                )
            return _DISABLED

    def _take(
        self, locks: PathLockStore, run: dict[str, Any], run_id: str, *, resume: bool
    ) -> ScopeDecision:
        worktree_record = json_to_worktree(run.get("scope_json"))
        if worktree_record is not None or self.worktrees_enabled():
            # WITHOUT THIS THE WHOLE FEATURE INVERTS. ``var/runs`` is already a
            # depth-1 private base, so an unregistered worktree path keys to the
            # single realm ``var/runs/worktrees`` and every run in the fleet
            # collides there — strictly worse than the per-project serialization
            # T3.8 exists to remove. Registering is idempotent and only ever
            # reached once a worktree is recorded or the flag is on.
            self._lane_worktrees().register_realm_base()
        claims = self._declaration(run, run_id)
        generation = int(run.get("lease_generation") or 0) + 1
        ttl = scope_ttl_s()
        if not self._persist_declaration(run_id, claims, generation, ttl, worktree=worktree_record):
            # Somebody else owns the row now. Not our run to arbitrate.
            return self._decide(run_id, ScopeDecision(status="fenced", detail="not_owner"))
        holder = LockHolder(kind=HOLDER_KIND, id=run_id, lane=LANE)
        acquire = locks.reacquire_scope if resume else locks.try_acquire_scope
        result: AcquireResult = acquire(
            claims,
            holder,
            generation=generation,
            ttl_s=ttl,
            # enforce=None resolves scope_locks_mode() inside the store; the mode
            # is never hardcoded here.
        )
        realm = claims[0].realm if claims else ""
        if result.status == "fenced":
            return self._decide(
                run_id,
                ScopeDecision(status="fenced", realm=realm, generation=generation),
            )
        if result.granted:
            with self._state_lock:
                self._generations[run_id] = generation
                self._displaced.discard(run_id)
                was_parked = run_id in self._parked
                self._parked.discard(run_id)
            if was_parked or resume:
                # Only a holder that actually got its scope clears its park row.
                # ``resume`` clears unconditionally because a park can outlive the
                # process that made it and this worker's memory of it cannot.
                locks.clear_waiter(HOLDER_KIND, run_id)
            return self._decide(
                run_id,
                ScopeDecision(
                    status="granted",
                    realm=realm,
                    generation=generation,
                    shadowed=result.shadowed,
                    detail=result.conflict.describe() if result.conflict is not None else "",
                ),
            )
        park_realm = result.conflict.candidate.realm if result.conflict is not None else realm
        if park_realm:
            locks.park_waiter(
                park_realm,
                holder,
                blocked_on=result.blocked_on,
                blocked_path=result.blocked_path,
            )
            with self._state_lock:
                self._parked.add(run_id)
        waited = self._waited_s(locks, park_realm, run_id)
        return self._decide(
            run_id,
            ScopeDecision(
                status="blocked",
                realm=park_realm,
                generation=generation,
                detail=result.conflict.describe() if result.conflict is not None else "",
                blocked_on=result.blocked_on,
                blocked_path=result.blocked_path,
                waited_s=waited,
                starved=waited >= scope_starvation_s(),
            ),
        )

    def _decide(self, run_id: str, decision: ScopeDecision) -> ScopeDecision:
        """Attach ``report`` — True only when this is NEW information.

        A blocked run is re-evaluated every selection pass; without this a single
        stuck run would emit an audit event twice a second forever.
        """
        signature = f"{decision.status}|{decision.blocked_on}|{int(decision.starved)}"
        with self._state_lock:
            if decision.status == "granted" and not decision.shadowed:
                self._reported.pop(run_id, None)
                return decision
            if self._reported.get(run_id) == signature:
                return decision
            self._reported[run_id] = signature
        return ScopeDecision(
            status=decision.status,
            realm=decision.realm,
            generation=decision.generation,
            detail=decision.detail,
            blocked_on=decision.blocked_on,
            blocked_path=decision.blocked_path,
            waited_s=decision.waited_s,
            starved=decision.starved,
            shadowed=decision.shadowed,
            report=True,
        )

    def _declaration(self, run: dict[str, Any], run_id: str) -> tuple[ScopeClaim, ...]:
        """The claim set for this run: replay ``scope_json``, else derive it.

        Replaying matters on the resume path — a run that declared a scope before
        the worker died must come back asking for the SAME scope, not for whatever
        its plan happens to derive to now (a plan is mutable state; a declaration
        is a commitment).
        """
        stored = json_to_claims(run.get("scope_json"))
        if stored:
            return stored
        return derive_claims(run, self._task_input(run), self._workspace_path(run_id))

    def _task_input(self, run: dict[str, Any]) -> dict[str, Any]:
        try:
            task = self._store.get_task(str(run.get("task_id") or "")) or {}
            raw = task.get("input_json")
            parsed = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
        except Exception:  # noqa: BLE001 -- an unreadable task degrades to the workspace
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _workspace_path(self, run_id: str) -> str:
        return str(Path(self._workspace_base) / run_id)

    def _persist_declaration(
        self,
        run_id: str,
        claims: tuple[ScopeClaim, ...],
        generation: int,
        ttl: float,
        *,
        worktree: Mapping[str, Any] | None = None,
    ) -> bool:
        """Write ``scope_json`` + the fenced lease. False when we no longer own it.

        The generation bump is a read-modify-write over ``runs.lease_generation``,
        guarded by ``expect_worker``: only the current owner can bump. That is not
        by itself an atomic compare-and-swap, and it does not have to be — the
        lock store's ``BEGIN IMMEDIATE`` is the real arbiter, and it refuses any
        generation lower than the highest ever recorded for this identity, so a
        lost race can only ever fence a claimant OUT, never let two in.

        ``worktree`` is the record already on the row: this column carries both
        the declaration and the T3.8 registration, so re-serializing without it
        would erase the run's recorded mode and let the next process re-resolve
        it from the ambient flag — the mid-flight flip m6 exists to prevent.
        """
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl)).strftime(_ISO_FORMAT)
        return bool(
            self._store.update_run(
                run_id,
                {
                    "scope_json": claims_to_json(claims, worktree=worktree),
                    "lease_generation": generation,
                    "lease_expires_at": expires_at,
                },
                expect_worker=self._worker_id,
            )
        )

    def _waited_s(self, locks: PathLockStore, realm: str, run_id: str) -> float:
        """How long this run's DURABLE waiter row has existed, in seconds.

        Durable on purpose: an in-process timer would reset on every worker
        restart, which is precisely the condition under which a head starves.
        """
        if not realm:
            return 0.0
        try:
            for waiter in locks.waiting_in_realm(realm):
                if waiter.holder_kind == HOLDER_KIND and waiter.holder_id == run_id:
                    created = _parse_iso(waiter.created_at)
                    if created is None:
                        return 0.0
                    return max(0.0, (datetime.now(UTC) - created).total_seconds())
        except Exception:  # noqa: BLE001 -- diagnostics must not break the decision
            LOG.exception("runner scope could not measure waiter age for run %s", run_id)
        return 0.0

    # --- fence ------------------------------------------------------------

    def fence_ok(self, run_id: str) -> bool:
        """False when this worker is no longer the lease holder for ``run_id``.

        The dark path is a single dict lookup: with the feature off nothing is
        ever recorded, so this returns True without reading config or the store.

        When a generation IS held, the predicate is ``runs.lease_generation ==
        the generation we acquired under``. An adopter bumps that column, so a
        merely-WEDGED (not dead) worker that wakes up mid-step fails here and
        stops — the same fate migration 052 gave a displaced swarm coordinator.
        """
        with self._state_lock:
            generation = self._generations.get(run_id)
            displaced = run_id in self._displaced
        if generation is None:
            return True
        if displaced:
            return False
        try:
            row = self._store.get_run(run_id)
        except Exception:  # noqa: BLE001 -- an unreadable row is not a proof of ownership
            LOG.exception("runner scope could not re-read run %s for its fence", run_id)
            return False
        if row is None:
            return False
        return int(row.get("lease_generation") or 0) == generation

    # --- renew ------------------------------------------------------------

    def renew(self, run_id: str) -> bool:
        """Extend this run's locks and ``lease_expires_at``. Never raises.

        Called from the per-step heartbeat pulse (``stale_s/3``), which is well
        inside the 90s default lease. A False return in ENFORCE mode marks the run
        displaced so the NEXT fence check fails: the heartbeat thread cannot abort
        a step in flight, but it can make sure the step's own fence boundary does.
        Shadow mode records nothing — a shadow that changes a decision is not a
        shadow.
        """
        with self._state_lock:
            generation = self._generations.get(run_id)
        if generation is None:
            return True
        locks = self._lock_store()
        if locks is None:
            return True
        ttl = scope_ttl_s()
        try:
            renewed = locks.renew_scope(HOLDER_KIND, run_id, generation, ttl_s=ttl)
            if renewed:
                # Only a holder that actually still holds the locks moves the row's
                # advertised expiry. A refused renew means we were adopted away, and
                # writing the column then would be this worker editing its adopter's
                # lease bookkeeping.
                self._store.update_run(
                    run_id,
                    {
                        "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).strftime(
                            _ISO_FORMAT
                        )
                    },
                    expect_worker=self._worker_id,
                )
        except Exception:  # noqa: BLE001 -- a renew fault must not kill the pulse thread
            LOG.exception("runner scope renew failed for run %s", run_id)
            return False
        if not renewed and scope_locks_mode() == "enforce":
            LOG.warning("runner scope lease lost for run %s at generation %s", run_id, generation)
            with self._state_lock:
                self._displaced.add(run_id)
        return renewed

    # --- release ----------------------------------------------------------

    def release(self, run_id: str) -> int:
        """Give this run's paths back. Generation-FENCED, and never raises.

        Fenced because a displaced worker's cleanup path runs with its OLD
        generation and must not free the locks its ADOPTER is now working under: a
        stale release matches zero rows, which is both the correct outcome and the
        signal that this worker has been displaced.
        """
        with self._state_lock:
            generation = self._generations.pop(run_id, None)
            self._displaced.discard(run_id)
            self._reported.pop(run_id, None)
            parked = run_id in self._parked
            self._parked.discard(run_id)
        if generation is None and not parked:
            return 0
        locks = self._lock_store()
        if locks is None:
            return 0
        released = 0
        try:
            if generation is not None:
                released = locks.release_scope(HOLDER_KIND, run_id, generation)
                self._store.update_run(
                    run_id, {"lease_expires_at": None}, expect_worker=self._worker_id
                )
            locks.clear_waiter(HOLDER_KIND, run_id)
        except Exception:  # noqa: BLE001 -- a terminal run must terminate regardless
            LOG.exception("runner scope release failed for run %s", run_id)
        return released

    def release_if_terminal(self, run_id: str) -> None:
        """The crash net: release when the run ended, whatever path it ended by.

        ``Runner._transition`` releases on the ordinary terminal path. This runs in
        ``execute_run``'s ``finally`` and catches everything else — an isolated
        fault, a quarantined finalization, a lost fence, a raise out of
        ``_transition`` itself.

        A run that is merely PARKED (awaiting approval) or PAUSED deliberately
        keeps nothing: it is not terminal, so no release happens here, and nobody
        renews it while it waits, so its lease simply LAPSES. The resume path
        re-acquires. That is the whole reason the locks carry a TTL.

        It is also where a per-run WORKTREE is merged back and removed, and the
        two are deliberately on the same trigger: the worktree is this run's
        private realm, so integrating it is the last thing that must happen while
        the run still owns that realm. The merge runs BEFORE the release for the
        same reason. A run that is parked/paused keeps its worktree exactly as it
        keeps its declaration — a successor activation reuses it AS-IS, including
        anything uncommitted.
        """
        with self._state_lock:
            held = run_id in self._generations
            has_worktree = run_id in self._worktrees
        if not held and not has_worktree:
            return
        try:
            row = self._store.get_run(run_id)
        except Exception:  # noqa: BLE001
            LOG.exception("runner scope could not read run %s while releasing", run_id)
            return
        if row is None:
            self.integrate(run_id)
            self.release(run_id)
            return
        try:
            terminal = RunState(str(row["state"])) in TERMINAL_RUN_STATES
        except ValueError:
            terminal = False
        displaced = str(row.get("worker_id") or "") != self._worker_id
        if terminal or displaced:
            if terminal and not displaced:
                # A DISPLACED worker must not merge: the adopter owns this run's
                # workspace now, and a merge is a mutation of the shared realm.
                # Dropping the handle without integrating leaves the branch and
                # the worktree intact for the adopter to inherit.
                self.integrate(run_id)
            else:
                with self._state_lock:
                    self._worktrees.pop(run_id, None)
            self.release(run_id)

    def _forget(self, run_id: str) -> None:
        """Drop in-process lease state without writing anything (mode flipped off)."""
        with self._state_lock:
            self._generations.pop(run_id, None)
            self._displaced.discard(run_id)
            self._parked.discard(run_id)
            self._reported.pop(run_id, None)

    # --- plumbing ---------------------------------------------------------

    def _lock_store(self) -> PathLockStore | None:
        """The lock store over the runner's own store, or None if it has no surface.

        ``PathLockStore`` deliberately shares the caller's connection and writer
        lock (Phase 2 serialized every writer for a reason). A Store implementation
        that is not the SQLite one — a fake in a unit test, a future remote store —
        has no such surface, and the honest answer there is "this lane cannot take
        locks", not a crash.
        """
        if self._locks is not None:
            return self._locks
        if self._locks_unavailable:
            return None
        store = self._store
        if not all(hasattr(store, attr) for attr in ("_lock", "_connection", "_begin")):
            self._locks_unavailable = True
            LOG.warning(
                "runner scope locks unavailable: %s has no transaction surface",
                type(store).__name__,
            )
            return None
        self._locks = PathLockStore(store)  # type: ignore[arg-type]
        return self._locks


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        return None
