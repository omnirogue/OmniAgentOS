"""Scope telemetry: the two measurements that decide whether ENFORCE is ever safe.

This module writes no locks, refuses nothing, and changes no decision. It exists
to answer one question with a query instead of an opinion:

    **Is it safe to move this lane from OBSERVE to ENFORCE?**

Two streams, emitted through the audit seam into the existing ``events`` table.
No new table: a rollout gate that needs a migration to be readable is a gate
nobody runs.

Stream (a) — ``scope_conflict_shadow``
    One event per collision OBSERVED while scope locks are in shadow mode.
    Payload: ``realm``, ``candidate_path``, ``held_path``, ``reason``,
    ``candidate_lane``, ``held_lane``, ``held_holder``.

    This measures REAL contention, not a hypothesis, and the reason is structural:
    :meth:`~omniagentos.scope.locks.PathLockStore.try_acquire_scope` in shadow
    mode *inserts the lock rows anyway* (see that module's docstring). A shadow
    that only evaluated the predicate without writing would measure a
    counterfactual, because the locks it declined to write are exactly the locks
    the next claimant would have collided with. Since the rows are real, every
    conflict this stream records is a conflict enforcement would actually have
    refused.

Stream (b) — ``scope_declared_vs_actual``
    One event per unit at its TERMINAL transition. Payload: ``declared``,
    ``actual``, ``missing`` (= ``actual`` minus ``declared``), ``precision``.

    THIS IS THE GATE.

Why (b) is the gate
-------------------
Enforcement converts an UNDER-DECLARATION into a HANG.

Under OBSERVE, a unit that touches a path it never declared produces a note. Under
ENFORCE, that same unit must acquire the undeclared path mid-run; if any other
claimant holds it, the unit parks in the realm's FIFO queue instead of writing. It
does not crash. It does not log an error. Nothing is "wrong" anywhere — the
scheduler is behaving exactly as designed. The operator sees a fleet that looks
wedged with no error to grep for, which is the single worst failure mode this
phase can ship, because it is indistinguishable from a deadlock and it will get
the whole mechanism switched off.

So the precondition for enforcement is not "locking works". Locking already works
and :mod:`omniagentos.scope.locks` proves it. The precondition is that agents
*declare what they actually touch*, and that is a measurement, not a design
property.

THE ROLLOUT RULE
----------------
    **precision >= 0.99 per lane, over 72 hours of PRODUCTION shadow traffic,
    before that lane may leave OBSERVE.**

Every term is load-bearing:

``precision >= 0.99``
    ``precision`` is the fraction of ACTUALLY-touched paths that were covered by
    the declaration (see :func:`declared_vs_actual`). Its complement is therefore
    the per-path rate at which enforcement would have to arbitrate a path nobody
    reserved — the hang rate's upper bound. At 0.99, roughly one path in a hundred
    is unreserved; at 0.90, one in ten, and with tens of paths per unit that is
    effectively every unit. 0.99 is not a round number chosen for looking
    rigorous: it is the point at which the median unit gets through without
    needing arbitration it did not plan for.

``per lane``
    Because the declaration DISCIPLINE is per lane, not global. The swarm lane
    declares ``owned_paths`` up front and is planned against them; a session
    declares whatever the supervisor could infer. A fleet-wide average lets a
    well-behaved lane carry a badly-behaved one over the line, and the lane that
    gets enforced is the one that hangs. This mirrors
    :class:`~omniagentos.contracts.ScopeEnforcement`, which is deliberately
    per-lane with no global switch.

``72 hours``
    Long enough to contain the periodic work that produces the worst
    under-declarations — nightly maintenance, lockfile regeneration, migration
    rewrites, a weekly report job. A two-hour soak measures the happy path and
    passes; the paths that hang enforcement are precisely the ones a short window
    never sees. :attr:`SOAK_WINDOW_HOURS` is checked by
    :meth:`ScopeGateReport.verdict`, which returns ``insufficient_data`` — never
    ``pass`` — for a window that is too short or too thin.

``production``
    Test traffic declares what the test declared. It proves nothing about agents.

Reading the gate
----------------
:func:`scope_gate_report` returns per-lane and per-(lane, realm) counters with
conflict rate, blocked-seconds and declared-vs-actual precision, plus the most
frequently missed paths — so the answer to "may this lane be enforced" is a query
result and the follow-up "what would we have to declare first" is already in the
same object.

Ship-dark contract
------------------
Everything here is INERT by default. With the shipped configuration
(:func:`~omniagentos.scope.config.scope_locks_mode` == ``off`` and
``OMNIAGENTOS_SCOPE_OBSERVE`` unset) every recorder returns ``None`` having made
ZERO store calls — not one insert, not one read, not even a config-dependent
branch that reaches the database. ``tests/scope/test_observe.py`` proves it
against a store double that raises on any attribute access.

Once an operator has already opted into shadow or enforce, telemetry follows
automatically (see :func:`scope_observe_enabled`). That default is deliberate: the
alternative is running a 72-hour soak and discovering afterwards that the gate
data was never collected. ``OMNIAGENTOS_SCOPE_OBSERVE=off`` is the kill switch and
it overrides in both directions.

This module must never import from ``omniagentos.swarm`` / ``omniagentos.runner``
/ ``omniagentos.db`` — the derivation in :func:`derive_actual` is LIFTED from
``SwarmScheduler._settle_terminal`` and takes its two git seams as structural
protocols so that the scheduler can hand over the objects it already has without
this package sitting on top of it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast, runtime_checkable

from omniagentos.audit import audit
from omniagentos.contracts import Events, ObservedChange, Store
from omniagentos.scope.config import parallelism_config, scope_locks_enabled, scope_locks_mode
from omniagentos.scope.conflict import ScopeConflict
from omniagentos.scope.locks import AcquireResult, HeldLock
from omniagentos.scope.paths import ScopePathError, normalize_rel, rel_text, under

__all__ = [
    "ACTION_CONFLICT_SHADOW",
    "ACTION_DECLARED_VS_ACTUAL",
    "MIN_GATE_UNITS",
    "MISSING_SAMPLE_LIMIT",
    "OBSERVE_ACTIONS",
    "PATH_SAMPLE_LIMIT",
    "PRECISION_GATE",
    "SCOPE_OBSERVE_ENV",
    "SCOPE_TARGET_TYPE",
    "SOAK_WINDOW_HOURS",
    "DeclaredVsActual",
    "GateVerdict",
    "ScopeCounters",
    "ScopeGateReport",
    "WorkdirDiff",
    "WorktreeDiff",
    "declared_paths_from_scope",
    "declared_vs_actual",
    "derive_actual",
    "observe_acquire",
    "record_declared_vs_actual",
    "record_shadow_conflict",
    "record_terminal_observation",
    "resolve_held",
    "scope_gate_report",
    "scope_observe_enabled",
    "session_reported_paths",
]

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The gate constants
# ---------------------------------------------------------------------------

#: The rollout threshold. See the module docstring for why 0.99 and not 0.95.
PRECISION_GATE = 0.99

#: Hours of production shadow traffic a lane must accumulate before its precision
#: is allowed to mean anything. Shorter windows systematically miss the periodic
#: jobs that produce the worst under-declarations.
SOAK_WINDOW_HOURS = 72.0

#: Below this many observed units a lane's precision is statistical noise: three
#: clean units are a 1.00 that says nothing. ``insufficient_data``, not ``pass``.
MIN_GATE_UNITS = 50

#: How many distinct missing paths a bucket keeps, most-frequent-first. Bounded
#: because the report is meant to be read, and because the interesting answer is
#: always the head of that distribution (a lockfile, a generated migration, an
#: ``__init__`` export) rather than its tail.
MISSING_SAMPLE_LIMIT = 20

#: How many paths a single event payload carries per list. A unit that touched
#: 4000 files must not write a megabyte into ``events.payload_json``; the COUNTS
#: are what the gate arithmetic reads, and they are recorded exactly even when the
#: path lists are truncated.
PATH_SAMPLE_LIMIT = 200

ACTION_CONFLICT_SHADOW = "scope_conflict_shadow"
ACTION_DECLARED_VS_ACTUAL = "scope_declared_vs_actual"

#: The two actions :func:`scope_gate_report` reads.
OBSERVE_ACTIONS: tuple[str, ...] = (ACTION_CONFLICT_SHADOW, ACTION_DECLARED_VS_ACTUAL)

#: ``events.target_type`` for both streams. Deliberately NOT ``'run'``: the
#: pre-existing run-detail and project-feed queries (``get_events_for_run``,
#: ``list_events_for_project``) filter on ``target_type='run'``, and telemetry must
#: not change the shape of a UI feed as a side effect of being switched on. A
#: distinct target_type also makes the gate query cheap to scope.
SCOPE_TARGET_TYPE = "scope"

SCOPE_OBSERVE_ENV = "OMNIAGENTOS_SCOPE_OBSERVE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

GateVerdict = Literal["pass", "fail", "insufficient_data"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool | None:
    """Parse one boolean spelling; ``None`` when the value says nothing.

    Unparseable is ``None`` rather than ``False`` for the same reason
    :func:`~omniagentos.scope.config._coerce_mode` ignores a typo'd mode: a
    mistyped value must fall through to the documented resolution order, not
    silently pick a side.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def scope_observe_enabled() -> bool:
    """Is scope telemetry recording at all?

    Resolution order, and every step of it is deliberate:

    1. ``OMNIAGENTOS_SCOPE_OBSERVE`` — bidirectional. Truthy turns recording on
       even with locks off (useful for measuring declaration quality BEFORE
       committing to a lock soak); falsy force-disables it on a host whose config
       says otherwise. A one-directional override is not a kill switch.
    2. ``configs/parallelism.yaml``, key ``scope_observe``.
    3. Follow the locks: on in ``shadow``/``enforce``, off in ``off``.

    Step 3 is why the shipped default is OFF — ``scope_locks_mode()`` defaults to
    ``off`` — while an operator who has already turned shadow on gets the soak data
    without a second flag to remember. Running a 72-hour soak and finding out
    afterwards that nothing was recorded is the failure this default prevents.

    Read fresh on every call, never cached: the kill switch has to work on a live
    process without a restart, exactly as :func:`scope_locks_mode` does.
    """
    from_env = _coerce_bool(os.environ.get(SCOPE_OBSERVE_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_bool(parallelism_config().get("scope_observe"))
    if from_config is not None:
        return from_config
    return scope_locks_enabled()


# ---------------------------------------------------------------------------
# Structural seams
# ---------------------------------------------------------------------------


class _EventStore(Protocol):
    """The one write method this module needs, via the audit seam."""

    def insert_event(
        self,
        type: str,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> int: ...  # noqa: D102

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...  # noqa: D102


@runtime_checkable
class WorktreeDiff(Protocol):
    """The slice of :class:`omniagentos.worktrees.git.SubprocessWorktrees` used here."""

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]: ...  # noqa: D102


@runtime_checkable
class WorkdirDiff(Protocol):
    """The slice of the swarm scheduler's git seam used here."""

    def changed_paths(self, working_dir: str) -> list[str]: ...  # noqa: D102


class _HeldLookup(Protocol):
    """The slice of :class:`~omniagentos.scope.locks.PathLockStore` used here."""

    def held_in_realm(self, realm: str) -> list[HeldLock]: ...  # noqa: D102


# ---------------------------------------------------------------------------
# The lifted derivation
# ---------------------------------------------------------------------------


def session_reported_paths(session: Mapping[str, Any] | None) -> list[str] | None:
    """The agent's own file report, or ``None`` when it made none.

    Lifted verbatim from ``SwarmScheduler._session_files``. ``None`` and ``[]``
    are different answers and the difference is the whole point: ``None`` means
    UNKNOWN (fall through to the git delta), ``[]`` means the agent said it
    touched nothing. Collapsing them would turn "we have no idea" into a perfect
    precision score, which is precisely the false pass this gate exists to
    prevent.
    """
    if session is None:
        return None
    raw = session.get("files_json")
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(p) for p in raw]
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    return [str(p) for p in parsed] if isinstance(parsed, list) else None


def derive_actual(
    *,
    worktree_path: str = "",
    base_sha: str = "",
    worktrees: WorktreeDiff | None = None,
    working_dir: str = "",
    git: WorkdirDiff | None = None,
    session: Mapping[str, Any] | None = None,
) -> ObservedChange:
    """What the unit ACTUALLY touched. LIFTED from ``SwarmScheduler._settle_terminal``.

    The scheduler's post-terminal ownership diff already answers this question
    correctly, and it took several iterations to get right, so this is the same
    ladder rather than a second opinion:

    * **worktree mode** (``worktree_path`` and ``worktrees`` both given) —
      ``changed_paths_since(worktree_path, base_sha)``: the branch's CUMULATIVE
      delta against its base, which is committed + uncommitted + untracked.
      Workers commit freely inside their own worktree, so a HEAD-only delta or the
      agent's own file list would UNDER-report, and an under-report here inflates
      precision — the exact direction that would falsely open the gate.
    * **otherwise** — the agent's ``files_json`` report first, with the working
      directory's git delta as the fallback when it reported nothing.

    The one place this deliberately DIVERGES from the scheduler is the failure
    case. The scheduler substitutes ``[]`` on a git error because its next move is
    a revert and reverting nothing is the safe default. For telemetry, ``[]`` would
    be a silent perfect score, so a failed derivation returns
    ``source='unobserved'`` and :meth:`ScopeGateReport.verdict` refuses to count
    it. ``ObservedChange``'s own docstring makes the same point: 'unobserved' must
    not be read as 'nothing changed'.

    ``source`` uses the ``ObservedChange`` vocabulary: ``git-worktree`` |
    ``git-index`` | ``agent-report`` | ``unobserved``.
    """
    in_worktree = bool(worktree_path) and worktrees is not None
    if in_worktree:
        assert worktrees is not None  # narrowed by in_worktree
        try:
            paths = [str(p) for p in worktrees.changed_paths_since(worktree_path, base_sha)]
        except Exception:  # noqa: BLE001 -- inconclusive, never "clean".
            LOG.warning("scope observe: changed_paths_since failed", exc_info=True)
            return ObservedChange(source="unobserved", base_ref=base_sha or None)
        return ObservedChange(
            source="git-worktree",
            base_ref=base_sha or None,
            paths=_dedupe(paths),
        )

    reported = session_reported_paths(session)
    if reported is not None:
        return ObservedChange(
            source="agent-report",
            base_ref=base_sha or None,
            paths=_dedupe(reported),
        )
    if git is not None and working_dir:
        try:
            paths = [str(p) for p in git.changed_paths(working_dir)]
        except Exception:  # noqa: BLE001 -- inconclusive, never "clean".
            LOG.warning("scope observe: changed_paths failed", exc_info=True)
            return ObservedChange(source="unobserved", base_ref=base_sha or None)
        return ObservedChange(
            source="git-index",
            base_ref=base_sha or None,
            paths=_dedupe(paths),
        )
    return ObservedChange(source="unobserved", base_ref=base_sha or None)


def _dedupe(paths: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of raw path text."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# declared vs actual
# ---------------------------------------------------------------------------


def declared_paths_from_scope(scope: Any) -> list[str]:
    """Flatten a :class:`~omniagentos.contracts.DeclaredScope` into declared paths.

    All five path fields count as declarations, including ``files_to_delete``: a
    delete is a write, and enforcement arbitrates it like any other. Duck-typed on
    the attribute names rather than isinstance-checked so a lane may pass its own
    equivalent record.
    """
    out: list[str] = []
    for name in (
        "files_to_modify",
        "files_to_create",
        "files_to_delete",
        "create_roots",
        "must_modify",
    ):
        values = getattr(scope, name, None) or ()
        out.extend(str(v) for v in values)
    return _dedupe(out)


@dataclass(frozen=True, slots=True)
class DeclaredVsActual:
    """One unit's declaration measured against what it actually touched.

    ``precision`` is ``covered / actual``: of the paths the unit ACTUALLY wrote,
    the fraction the declaration already covered. Its complement is the share of
    writes enforcement would have had to arbitrate unreserved — i.e. the hang
    exposure. (In information-retrieval terms this is the declaration's *coverage*
    of observed change; ``precision`` is the field name the gate reads and the
    module docstring defines it, so the arithmetic is never ambiguous.)

    ``actual`` empty means nothing was touched. Nothing touched cannot be
    under-declared, so ``precision`` is ``1.0`` and ``counted`` is ``False`` — the
    unit is excluded from the gate denominator rather than credited with a perfect
    score. A fleet of no-op units must never be able to open the gate.

    ``observed`` empty with ``source == 'unobserved'`` means the derivation FAILED.
    Also ``counted=False``, for the stronger reason: an inconclusive observation is
    not a clean one.
    """

    declared: tuple[str, ...]
    actual: tuple[str, ...]
    missing: tuple[str, ...]
    covered: tuple[str, ...]
    precision: float
    source: str
    counted: bool

    @property
    def clean(self) -> bool:
        """True when every actually-touched path was declared."""
        return not self.missing


def _declared_prefixes(declared: Iterable[str]) -> list[tuple[str, ...]]:
    """Normalize declared paths to component tuples, dropping unusable entries.

    An entry that is not realm-relative (absolute, ``~``, climbing out) cannot be
    a scope declaration and is DROPPED rather than repaired: silently widening a
    malformed declaration to something plausible would credit coverage the lock
    store would never have granted.
    """
    prefixes: list[tuple[str, ...]] = []
    for entry in declared:
        try:
            prefixes.append(normalize_rel(entry))
        except ScopePathError:
            LOG.debug("scope observe: undeclarable path %r", entry)
    return prefixes


def declared_vs_actual(
    declared: Iterable[str],
    observed: ObservedChange | Iterable[str],
) -> DeclaredVsActual:
    """Compute the gate measurement for one unit.

    Coverage uses :func:`~omniagentos.scope.paths.under` — COMPONENT-wise
    containment, never string prefixes — because that is the identical algebra
    :mod:`omniagentos.scope.conflict` uses to decide what a granted claim actually
    covers. Any other rule would measure something enforcement does not do:

    * a declared subtree ``src/a`` covers ``src/a/b.py`` (that write needs no new
      arbitration, so it cannot hang);
    * ``.github`` does NOT cover ``github/x`` (``under`` compares components);
    * a declaration of ``"."`` normalizes to the whole realm and covers
      everything, which is correct — a whole-realm claim is what the lock store
      grants for it.

    Paths that cannot be normalized are dropped from BOTH sides, so a malformed
    actual path never counts as missing (it would be unattributable) and a
    malformed declaration never covers anything.

    De-duplication happens AFTER normalization, not before: ``src/a/../a/b.py``
    and ``src/a/b.py`` are one file written twice, and counting them as two would
    let a noisy diff move the gate's denominator.
    """
    if isinstance(observed, ObservedChange):
        source = observed.source
        actual_raw = list(observed.paths)
    else:
        source = "agent-report"
        actual_raw = [str(p) for p in observed]

    declared_list = _dedupe(declared)
    prefixes = _declared_prefixes(declared_list)

    covered: list[str] = []
    missing: list[str] = []
    actual_kept: list[str] = []
    seen: set[str] = set()
    for raw in _dedupe(actual_raw):
        try:
            components = normalize_rel(raw)
        except ScopePathError:
            LOG.debug("scope observe: unattributable actual path %r", raw)
            continue
        text = rel_text(components)
        if text in seen:
            continue
        seen.add(text)
        actual_kept.append(text)
        if any(under(components, prefix) for prefix in prefixes):
            covered.append(text)
        else:
            missing.append(text)

    total = len(actual_kept)
    precision = 1.0 if total == 0 else len(covered) / total
    counted = total > 0 and source != "unobserved"
    return DeclaredVsActual(
        declared=tuple(declared_list),
        actual=tuple(actual_kept),
        missing=tuple(missing),
        covered=tuple(covered),
        precision=precision,
        source=source,
        counted=counted,
    )


# ---------------------------------------------------------------------------
# Recorders
# ---------------------------------------------------------------------------


def _emit(
    store: _EventStore,
    action: str,
    *,
    lane: str,
    target_id: str,
    payload: dict[str, Any],
    trace_id: str,
) -> int | None:
    """One audit insert. NEVER raises into a lane.

    Telemetry that can fail a run is worse than no telemetry: the entire argument
    for shipping this is that it is free of consequence, and an exception escaping
    a terminal-transition hook would break a lane over a measurement.
    """
    # ``audit()`` is annotated against the whole frozen ``Store`` protocol but uses
    # only ``insert_event``. The cast keeps this module STRUCTURAL over the one
    # method it needs — the same discipline ``PathLockStore`` applies to the
    # store's writer seam — so a lane may hand in any event sink, including a
    # test double, without implementing forty unrelated methods.
    sink = cast("Store", store)
    try:
        return audit(
            sink,
            actor=f"scope:{lane}" if lane else "scope",
            action=action,
            target_type=SCOPE_TARGET_TYPE,
            target_id=target_id,
            payload=payload,
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001 -- see the docstring: never break a lane.
        LOG.warning("scope observe: emitting %s failed", action, exc_info=True)
        return None


def resolve_held(locks: _HeldLookup | None, conflict: ScopeConflict) -> HeldLock | None:
    """Find the live lock row behind a conflict, for its lane and holder identity.

    :meth:`~omniagentos.scope.locks.HeldLock.as_claim` carries the lock id but not
    the lane or holder — those are row columns, not claim algebra — so the (a)
    payload's ``held_lane``/``held_holder`` need this lookup. Best-effort: a
    ``None`` return means the payload records the empty string rather than losing
    the whole event, and the lock may legitimately have been released between the
    conflict and this call.
    """
    if locks is None:
        return None
    lock_id = conflict.held.lock_id
    if not lock_id:
        return None
    try:
        for lock in locks.held_in_realm(conflict.held.realm):
            if lock.id == lock_id:
                return lock
    except Exception:  # noqa: BLE001 -- telemetry lookup, never fatal.
        LOG.debug("scope observe: held-lock lookup failed", exc_info=True)
    return None


def record_shadow_conflict(
    store: _EventStore,
    conflict: ScopeConflict,
    *,
    candidate_lane: str,
    held_lock: HeldLock | None = None,
    held_lane: str = "",
    held_holder: str = "",
    locks: _HeldLookup | None = None,
    blocked_s: float = 0.0,
    candidate_holder: str = "",
    run_id: str = "",
    unit_id: str = "",
    mode: str = "",
    trace_id: str = "",
) -> int | None:
    """Stream (a): record one observed collision. Returns the event id, or ``None``.

    Call this on EVERY conflict the lock store reports — the shadow-mode ones that
    were granted anyway (``AcquireResult.conflict`` with ``status='granted'``) and
    the enforce-mode refusals alike. The action name says "shadow" because that is
    the mode the rollout ramp runs in and the mode whose data opens the gate; the
    ``mode`` field records which it actually was, so an enforce-mode host's numbers
    are never silently pooled with a shadow soak's.

    ``blocked_s`` is the time attributable to this conflict:

    * in ENFORCE, the seconds the candidate actually waited (the runner's
      ``waited_s``);
    * in SHADOW, the COUNTERFACTUAL — how long the candidate *would* have waited,
      best estimated by the blocker's remaining lease. Nothing waits in shadow, so
      a caller that passes nothing gets ``0.0`` and
      :attr:`ScopeCounters.blocked_seconds` reads as a floor rather than an
      estimate. That is honest; a fabricated number would not be.
    """
    if not scope_observe_enabled():
        return None
    if held_lock is None:
        held_lock = resolve_held(locks, conflict)
    if held_lock is not None:
        held_lane = held_lane or held_lock.lane
        held_holder = held_holder or f"{held_lock.holder_kind}:{held_lock.holder_id}"
    payload = {
        "realm": conflict.candidate.realm,
        "candidate_path": conflict.candidate.path_text,
        "candidate_kind": conflict.candidate.kind,
        "held_path": conflict.held.path_text,
        "held_kind": conflict.held.kind,
        "reason": conflict.reason,
        "candidate_lane": candidate_lane,
        "held_lane": held_lane,
        "held_holder": held_holder,
        "held_lock_id": conflict.held.lock_id,
        "candidate_holder": candidate_holder,
        "blocked_s": float(blocked_s),
        "mode": mode or scope_locks_mode(),
        "run_id": run_id,
        "unit_id": unit_id,
    }
    return _emit(
        store,
        ACTION_CONFLICT_SHADOW,
        lane=candidate_lane,
        target_id=unit_id or run_id,
        payload=payload,
        trace_id=trace_id,
    )


def observe_acquire(
    store: _EventStore,
    result: AcquireResult,
    *,
    candidate_lane: str,
    locks: _HeldLookup | None = None,
    blocked_s: float = 0.0,
    candidate_holder: str = "",
    run_id: str = "",
    unit_id: str = "",
    trace_id: str = "",
) -> int | None:
    """Convenience wrapper: record stream (a) straight from an :class:`AcquireResult`.

    A no-op when the acquire saw no collision, so a lane can call it
    unconditionally after every acquire without an ``if`` at the call site — the
    granted path stays one attribute read.
    """
    if result.conflict is None:
        return None
    return record_shadow_conflict(
        store,
        result.conflict,
        candidate_lane=candidate_lane,
        locks=locks,
        blocked_s=blocked_s,
        candidate_holder=candidate_holder,
        run_id=run_id,
        unit_id=unit_id,
        mode=result.mode,
        trace_id=trace_id,
    )


def record_declared_vs_actual(
    store: _EventStore,
    measurement: DeclaredVsActual,
    *,
    lane: str,
    realm: str = "",
    run_id: str = "",
    unit_id: str = "",
    unit_kind: str = "",
    terminal_state: str = "",
    trace_id: str = "",
) -> int | None:
    """Stream (b) — THE GATE: record one unit's declared-vs-actual measurement.

    Emit at the unit's TERMINAL transition, once, whatever the outcome. Failed and
    cancelled units are included on purpose: a unit that died after wandering
    outside its declaration is exactly the unit enforcement would have hung, and
    scoring only successes would bias the gate optimistic.

    Path lists are truncated to :data:`PATH_SAMPLE_LIMIT` with the ``*_count``
    fields carrying the exact totals, so the gate arithmetic stays correct on a
    unit that touched thousands of files while ``events.payload_json`` stays a row
    rather than a blob. ``missing`` is truncated LAST-resort only — it is the
    actionable half of the payload.
    """
    if not scope_observe_enabled():
        return None
    payload = {
        "declared": list(measurement.declared[:PATH_SAMPLE_LIMIT]),
        "actual": list(measurement.actual[:PATH_SAMPLE_LIMIT]),
        "missing": list(measurement.missing[:PATH_SAMPLE_LIMIT]),
        "declared_count": len(measurement.declared),
        "actual_count": len(measurement.actual),
        "covered_count": len(measurement.covered),
        "missing_count": len(measurement.missing),
        "truncated": max(
            len(measurement.declared),
            len(measurement.actual),
            len(measurement.missing),
        )
        > PATH_SAMPLE_LIMIT,
        "precision": round(measurement.precision, 6),
        "source": measurement.source,
        "counted": measurement.counted,
        "lane": lane,
        "realm": realm,
        "run_id": run_id,
        "unit_id": unit_id,
        "unit_kind": unit_kind,
        "terminal_state": terminal_state,
        "mode": scope_locks_mode(),
    }
    return _emit(
        store,
        ACTION_DECLARED_VS_ACTUAL,
        lane=lane,
        target_id=unit_id or run_id,
        payload=payload,
        trace_id=trace_id,
    )


def record_terminal_observation(
    store: _EventStore,
    *,
    lane: str,
    declared: Iterable[str],
    realm: str = "",
    run_id: str = "",
    unit_id: str = "",
    unit_kind: str = "",
    terminal_state: str = "",
    worktree_path: str = "",
    base_sha: str = "",
    worktrees: WorktreeDiff | None = None,
    working_dir: str = "",
    git: WorkdirDiff | None = None,
    session: Mapping[str, Any] | None = None,
    trace_id: str = "",
) -> DeclaredVsActual | None:
    """Derive + measure + emit stream (b) in one call. The lane-facing entry point.

    Returns the measurement (so a caller can log or assert on it) or ``None`` when
    telemetry is off. The flag is checked BEFORE the derivation, because
    ``changed_paths_since`` shells out to git and the dark path must not pay for a
    subprocess it will then discard.
    """
    if not scope_observe_enabled():
        return None
    observed = derive_actual(
        worktree_path=worktree_path,
        base_sha=base_sha,
        worktrees=worktrees,
        working_dir=working_dir,
        git=git,
        session=session,
    )
    measurement = declared_vs_actual(declared, observed)
    record_declared_vs_actual(
        store,
        measurement,
        lane=lane,
        realm=realm,
        run_id=run_id,
        unit_id=unit_id,
        unit_kind=unit_kind,
        terminal_state=terminal_state,
        trace_id=trace_id,
    )
    return measurement


# ---------------------------------------------------------------------------
# The queryable summary
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScopeCounters:
    """One (lane, realm) bucket — or one lane rollup when ``realm`` is ``""``.

    Raw counts are public so an operator can recompute any ratio; the derived
    properties are the ones the gate reads.
    """

    lane: str
    realm: str = ""
    conflicts: int = 0
    blocked_seconds: float = 0.0
    units: int = 0
    units_counted: int = 0
    units_clean: int = 0
    units_unobserved: int = 0
    declared_paths: int = 0
    actual_paths: int = 0
    covered_paths: int = 0
    missing_paths: int = 0
    first_ts: str = ""
    last_ts: str = ""
    #: Span of the DECLARED-VS-ACTUAL observations only. Separate from
    #: first_ts/last_ts because those also advance on scope_conflict_shadow rows,
    #: which let a lane satisfy the 72h soak on conflict chatter alone while the
    #: gate evidence -- the units -- was all recorded in one burst. The soak
    #: exists to prove the declarations hold up OVER TIME, so it must measure the
    #: units and nothing else.
    unit_first_ts: str = ""
    unit_last_ts: str = ""
    missing_counter: Counter[str] = field(default_factory=Counter)

    @property
    def precision(self) -> float:
        """Path-weighted precision: covered / actual over every counted unit.

        ``1.0`` when nothing was observed. Read that as "no evidence", never as
        "clean" — :meth:`ScopeGateReport.verdict` refuses to pass a bucket with
        too few counted units precisely so this cannot be mistaken for a result.
        """
        if self.actual_paths <= 0:
            return 1.0
        return self.covered_paths / self.actual_paths

    @property
    def unit_precision(self) -> float:
        """Unit-weighted: the fraction of counted units that were fully declared.

        Reported alongside the path-weighted number because they fail differently.
        One unit that touched 500 undeclared files tanks path precision while
        unit precision stays high; 200 units each missing one file does the
        reverse. Enforcement hangs UNITS, so a low ``unit_precision`` with a
        passing ``precision`` is still a reason not to enforce — which is why
        :meth:`ScopeGateReport.verdict` checks both.
        """
        if self.units_counted <= 0:
            return 1.0
        return self.units_clean / self.units_counted

    @property
    def conflict_rate(self) -> float:
        """Observed conflicts per counted unit.

        The denominator is UNITS, not acquire attempts, and that is a deliberate
        trade: an event per granted acquire would multiply the events table by the
        renew/poll rate for a number nobody reads. "Conflicts per unit of work" is
        the quantity that predicts contention anyway, and both raw counts are
        exposed for anyone who wants a different ratio.
        """
        if self.units_counted <= 0:
            return 0.0
        return self.conflicts / self.units_counted

    @property
    def window_hours(self) -> float:
        """Hours spanned by the GATE EVIDENCE (declared-vs-actual units).

        Deliberately NOT the span of every observation: conflict-shadow rows also
        move first_ts/last_ts, and a lane could otherwise clear the soak on
        conflict chatter with all of its unit evidence recorded in a single burst.
        """
        start = _parse_ts(self.unit_first_ts)
        end = _parse_ts(self.unit_last_ts)
        if start is None or end is None:
            return 0.0
        return max((end - start).total_seconds() / 3600.0, 0.0)

    @property
    def top_missing(self) -> tuple[tuple[str, int], ...]:
        """The most frequently undeclared paths — the actionable half of the gate.

        A gate that only says "no" wastes the soak. This says which declarations
        to fix first, and in practice the head of this distribution is a handful
        of paths (a lockfile, a generated migration, an ``__init__`` export) that
        one planner change covers.
        """
        return tuple(self.missing_counter.most_common(MISSING_SAMPLE_LIMIT))

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready snapshot, derived values included."""
        return {
            "lane": self.lane,
            "realm": self.realm,
            "conflicts": self.conflicts,
            "conflict_rate": round(self.conflict_rate, 6),
            "blocked_seconds": round(self.blocked_seconds, 3),
            "units": self.units,
            "units_counted": self.units_counted,
            "units_clean": self.units_clean,
            "units_unobserved": self.units_unobserved,
            "declared_paths": self.declared_paths,
            "actual_paths": self.actual_paths,
            "covered_paths": self.covered_paths,
            "missing_paths": self.missing_paths,
            "precision": round(self.precision, 6),
            "unit_precision": round(self.unit_precision, 6),
            "window_hours": round(self.window_hours, 3),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "unit_first_ts": self.unit_first_ts,
            "unit_last_ts": self.unit_last_ts,
            "top_missing": [{"path": p, "count": n} for p, n in self.top_missing],
        }


@dataclass(frozen=True, slots=True)
class ScopeGateReport:
    """The gate, as a query result.

    ``by_lane`` answers "may this lane be enforced" (the rollout rule is per-lane).
    ``by_realm`` answers "where is the contention", keyed ``(lane, realm)``.
    """

    by_lane: dict[str, ScopeCounters]
    by_realm: dict[tuple[str, str], ScopeCounters]
    since: str = ""
    until: str = ""
    events_scanned: int = 0
    truncated: bool = False

    def verdict(self, lane: str) -> GateVerdict:
        """May ``lane`` leave OBSERVE? ``pass`` / ``fail`` / ``insufficient_data``.

        Four conditions, and ``insufficient_data`` is returned in preference to
        ``pass`` whenever any of the evidence conditions is unmet — a gate that
        answers ``pass`` on thin data is worse than no gate, because it launders
        an absence of measurement into a green light.

        1. the lane has observations at all;
        2. at least :data:`MIN_GATE_UNITS` COUNTED units (units whose observation
           was conclusive and non-empty);
        3. a window of at least :data:`SOAK_WINDOW_HOURS`;
        4. both ``precision`` and ``unit_precision`` at or above
           :data:`PRECISION_GATE`.

        Only condition 4 can produce ``fail``. 1-3 are about whether the question
        has been answered yet.
        """
        if self.truncated:
            # A scan-capped report has SEEN only part of the evidence, so its
            # precision is an optimistic sample rather than a measurement: the
            # rows it did not read are exactly as likely to be the failures. A
            # capped report greenlighting a lane whose full data fails is the
            # worst outcome this gate can produce.
            return "insufficient_data"
        bucket = self.by_lane.get(lane)
        if bucket is None or bucket.units_counted <= 0:
            return "insufficient_data"
        if bucket.units_counted < MIN_GATE_UNITS:
            return "insufficient_data"
        if bucket.window_hours < SOAK_WINDOW_HOURS:
            return "insufficient_data"
        if bucket.precision < PRECISION_GATE or bucket.unit_precision < PRECISION_GATE:
            return "fail"
        return "pass"

    def blockers(self, lane: str) -> list[str]:
        """Why ``lane`` is not passing yet, in plain sentences. Empty when it passes."""
        out: list[str] = []
        if self.truncated:
            out.append(
                f"report is TRUNCATED at {self.events_scanned} events -- the "
                "precision below is a sample, not a measurement; re-run with a "
                "higher max_events before treating any verdict as final"
            )
        bucket = self.by_lane.get(lane)
        if bucket is None or bucket.units_counted <= 0:
            out.append(f"no counted scope observations for lane {lane!r}")
            return out
        if bucket.units_counted < MIN_GATE_UNITS:
            out.append(f"only {bucket.units_counted} counted units (need >= {MIN_GATE_UNITS})")
        if bucket.window_hours < SOAK_WINDOW_HOURS:
            out.append(
                f"soak window {bucket.window_hours:.1f}h "
                f"(need >= {SOAK_WINDOW_HOURS:.0f}h of production shadow)"
            )
        if bucket.precision < PRECISION_GATE:
            out.append(
                f"path precision {bucket.precision:.4f} < {PRECISION_GATE} "
                f"({bucket.missing_paths} undeclared paths)"
            )
        if bucket.unit_precision < PRECISION_GATE:
            out.append(f"unit precision {bucket.unit_precision:.4f} < {PRECISION_GATE}")
        return out

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready snapshot with the per-lane verdicts resolved."""
        return {
            "since": self.since,
            "until": self.until,
            "events_scanned": self.events_scanned,
            "truncated": self.truncated,
            "precision_gate": PRECISION_GATE,
            "soak_window_hours": SOAK_WINDOW_HOURS,
            "min_gate_units": MIN_GATE_UNITS,
            "lanes": {
                lane: {
                    **bucket.as_dict(),
                    "verdict": self.verdict(lane),
                    "blockers": self.blockers(lane),
                }
                for lane, bucket in sorted(self.by_lane.items())
            },
            "realms": [bucket.as_dict() for _, bucket in sorted(self.by_realm.items())],
        }


def _parse_ts(text: str) -> datetime | None:
    """Parse an ``events.ts`` stamp; ``None`` for anything unparseable."""
    if not text:
        return None
    try:
        return datetime.strptime(text, _ISO_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@contextmanager
def _read(store: Any) -> Iterator[sqlite3.Connection]:
    """A pure-SELECT connection, mirroring ``PathLockStore._read`` exactly.

    Same reasoning as there: readers take the store's separate ``_read_lock`` so
    they are never delayed by a writer inside ``BEGIN IMMEDIATE``, and a
    ``:memory:`` store keeps taking the writer lock because it has no per-thread
    connections to spread over.
    """
    shared = getattr(store, "_shared_connection", None)
    read_lock = getattr(store, "_read_lock", None)
    guard = read_lock if (shared is None and read_lock is not None) else store._lock
    with guard:
        yield store._connection


def _rows_via_sql(
    store: Any, since_ts: str, limit: int
) -> list[tuple[str, str, dict[str, Any]]] | None:
    """Targeted SQL over ``events``. ``None`` when this store has no connection.

    The fast path matters at gate time: 72 hours of production traffic is a lot of
    ``audit.event`` rows and the protocol fallback has to page through every one of
    them to find the two actions this module writes. Guarded by duck-typing and a
    blanket except so a store that is not SQLite-backed simply falls back.
    """
    if not hasattr(store, "_connection") or not hasattr(store, "_lock"):
        return None
    placeholders = ", ".join("?" for _ in OBSERVE_ACTIONS)
    sql = (
        "SELECT ts, action, payload_json FROM events "
        f"WHERE type = ? AND target_type = ? AND action IN ({placeholders}) "
        "AND ts >= ? ORDER BY id ASC LIMIT ?"
    )
    params: list[Any] = [Events.AUDIT, SCOPE_TARGET_TYPE, *OBSERVE_ACTIONS, since_ts, limit]
    try:
        with _read(store) as conn:
            fetched = conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 -- fall back to the frozen protocol.
        LOG.debug("scope observe: direct events query unavailable", exc_info=True)
        return None
    return [(str(row[0]), str(row[1]), _payload(row[2])) for row in fetched]


def _rows_via_protocol(
    store: _EventStore, since_ts: str, limit: int
) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """Page ``get_events_after`` — the portable path. Returns (rows, truncated).

    ``truncated`` is True only when the ``max_events`` scan cap was reached, not
    when the table simply ended: a short batch means the store has no more rows,
    which is a complete answer and must not be reported as a partial one.
    """
    # `limit` counts MATCHING rows, exactly as the SQL reader's LIMIT does.
    #
    # It previously counted every audit.event scanned, including unrelated ones,
    # so the two readers silently disagreed: 50 noise rows followed by 5 scope
    # rows gave the SQL path all 5 and the protocol path 0, with the protocol path
    # additionally reporting truncated=True. Two readers of the same table
    # returning different evidence to a safety gate is worse than either being
    # slow -- whichever one a host happened to take would decide whether
    # enforcement is safe.
    out: list[tuple[str, str, dict[str, Any]]] = []
    after = 0
    scanned_rows = 0
    chunk = 500
    #: Hard stop on total rows walked, so a table dominated by unrelated audit
    #: events cannot page forever looking for `limit` matches. Reaching it IS
    #: truncation: the scan gave up before exhausting the table.
    scan_ceiling = max(limit * 50, 50_000)
    while len(out) < limit and scanned_rows < scan_ceiling:
        batch = store.get_events_after(after, [Events.AUDIT], chunk)
        if not batch:
            return out, False
        for row in batch:
            after = max(after, _as_int(row.get("id")))
            scanned_rows += 1
            if str(row.get("action") or "") not in OBSERVE_ACTIONS:
                continue
            if str(row.get("target_type") or "") != SCOPE_TARGET_TYPE:
                continue
            ts = str(row.get("ts") or "")
            if since_ts and ts < since_ts:
                continue
            out.append((ts, str(row["action"]), _payload(row.get("payload_json"))))
            if len(out) >= limit:
                # Limit reached: this IS truncation, and the check must precede
                # the short-batch check below. Otherwise a final partial page
                # (len(batch) < chunk, which is the common case) reported
                # truncated=False even though the cap had just cut the scan off --
                # which would let a capped report reach `pass`.
                return out, True
        if len(batch) < chunk:
            return out, False
    return out, True


def _payload(raw: Any) -> dict[str, Any]:
    """``payload_json`` as a dict; anything unparseable becomes ``{}``."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scope_gate_report(
    store: _EventStore,
    *,
    since: str | datetime | None = None,
    window_hours: float | None = None,
    max_events: int = 200_000,
) -> ScopeGateReport:
    """Aggregate both streams into the gate report. Reads only; writes nothing.

    ``since`` / ``window_hours`` bound the window (default: the whole table).
    Note that :attr:`ScopeCounters.window_hours` measures the OBSERVED span rather
    than the requested one, so asking for 72 hours of a table that only holds two
    cannot manufacture a passing window.

    Safe to call with telemetry off — it simply finds nothing. It is a read, so it
    is deliberately NOT gated on :func:`scope_observe_enabled`: an operator turning
    the flag off after a soak must still be able to read the soak's result.
    """
    until = datetime.now(UTC)
    if since is not None:
        start = since if isinstance(since, datetime) else _parse_ts(str(since))
    elif window_hours is not None:
        start = until - timedelta(hours=float(window_hours))
    else:
        start = None
    since_ts = start.astimezone(UTC).strftime(_ISO_FORMAT) if start is not None else ""

    truncated = False
    rows = _rows_via_sql(store, since_ts, max_events)
    if rows is None:
        rows, truncated = _rows_via_protocol(store, since_ts, max_events)
    else:
        truncated = len(rows) >= max_events

    by_lane: dict[str, ScopeCounters] = {}
    by_realm: dict[tuple[str, str], ScopeCounters] = {}

    def buckets(lane: str, realm: str) -> tuple[ScopeCounters, ScopeCounters]:
        lane_bucket = by_lane.get(lane)
        if lane_bucket is None:
            lane_bucket = by_lane[lane] = ScopeCounters(lane=lane)
        key = (lane, realm)
        realm_bucket = by_realm.get(key)
        if realm_bucket is None:
            realm_bucket = by_realm[key] = ScopeCounters(lane=lane, realm=realm)
        return lane_bucket, realm_bucket

    for ts, action, payload in rows:
        if action == ACTION_CONFLICT_SHADOW:
            lane = str(payload.get("candidate_lane") or payload.get("lane") or "")
        else:
            lane = str(payload.get("lane") or "")
        realm = str(payload.get("realm") or "")
        for bucket in buckets(lane, realm):
            if not bucket.first_ts or (ts and ts < bucket.first_ts):
                bucket.first_ts = ts
            if ts > bucket.last_ts:
                bucket.last_ts = ts
            if action == ACTION_CONFLICT_SHADOW:
                bucket.conflicts += 1
                bucket.blocked_seconds += _as_float(payload.get("blocked_s"))
                continue
            bucket.units += 1
            if not bucket.unit_first_ts or (ts and ts < bucket.unit_first_ts):
                bucket.unit_first_ts = ts
            if ts > bucket.unit_last_ts:
                bucket.unit_last_ts = ts
            source = str(payload.get("source") or "")
            if source == "unobserved":
                bucket.units_unobserved += 1
            if not bool(payload.get("counted")):
                continue
            bucket.units_counted += 1
            bucket.declared_paths += _as_int(payload.get("declared_count"))
            actual = _as_int(payload.get("actual_count"))
            covered = _as_int(payload.get("covered_count"))
            missing = _as_int(payload.get("missing_count"))
            bucket.actual_paths += actual
            bucket.covered_paths += covered
            bucket.missing_paths += missing
            if missing == 0:
                bucket.units_clean += 1
            for path in payload.get("missing") or ():
                bucket.missing_counter[str(path)] += 1

    return ScopeGateReport(
        by_lane=by_lane,
        by_realm=by_realm,
        since=since_ts,
        until=until.strftime(_ISO_FORMAT),
        events_scanned=len(rows),
        truncated=truncated,
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if parsed != parsed else parsed  # NaN != NaN
