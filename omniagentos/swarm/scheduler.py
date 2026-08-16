"""Swarm scheduler engine (WP5a): one coordinator + N resizable slot workers per run.

The most correctness-critical swarm module — every named race condition in the
plan doc has a specified fix here and a required test in
``tests/swarm/test_scheduler_races.py``:

COORDINATOR (one per run, heartbeat-leased)
    ``start_run`` wins the coordinator role via ``SwarmDal.try_activate_run``
    (claim-before-act CAS, the two-process discipline from
    ``docs/architecture/longhaul.md``); ``resume_swarm`` may adopt ONLY via
    ``SwarmDal.adopt_run`` when the heartbeat is > ``adopt_stale_minutes``
    stale — a fresh heartbeat means a live coordinator exists, so a partial
    restart can never produce two. The coordinator heartbeats every
    ``heartbeat_seconds`` (default 30) and rebuilds state ENTIRELY from the
    database (claims, live attempts, parked approvals) — in-memory state is a
    cache, never the truth.

COMPLETION SIGNALING
    Workers signal the coordinator through an in-process queue PLUS the
    coordinator re-derives everything from the DB on a ``fallback_poll_seconds``
    (default 30s) reconcile pass — a lost queue message can delay a decision by
    one poll, never wedge a run.

SLOTS (counter + condition, never a stock semaphore)
    ``target_n`` is a plain integer under the run condition; workers re-check
    it before EVERY pull and exit when their index ≥ ``target_n``
    (resize = set the variable; a live semaphore can never be resized safely).
    Grow is eager (missing worker threads started immediately), shrink is lazy
    (a running attempt is never killed by a shrink — the worker exits at its
    next pull). ``target_n = clamp(min(run_cap, eligible+running, fair_share),
    1, MAX_SLOTS)`` where ``run_cap`` is the run row's ``max_concurrency`` and
    ``fair_share`` divides ``limit_state.fleet_available().available_for_swarm``
    (plus this run's own live attempts, which that ledger already counts as
    consumed) across active swarms. Every resize emits a reasoned ``resize``
    event.

READY QUEUE (critical-path list scheduling)
    Eligible tasks are pulled longest-remaining-critical-path-first (sum of
    ``est_agent_minutes`` down the dependent chain to the integration task),
    then most-dependents-unblocked, then longest own estimate — Graham list
    scheduling, so a slot never burns on a leaf while the critical chain waits.

CLAIMS (leases over the collab CAS)
    ``CollabStore.claim_task`` (claim_version CAS) IS the claim; the
    coordinator's reconcile pass sweeps claims whose attempt died without
    terminalizing (claimed/in_progress, no live ``swarm_attempts`` row, no
    in-process worker) and ``release_claim``'s them. EVERY worker failure path
    releases the claim (the ``finally`` disposition switch in
    ``_execute_item``).

REVIEW (three-valued — CONFIRM / DENY / INFRA-FAILURE)
    CONFIRM → coordinator-owned pathspec-limited commit
    (``git add -- <owned_paths> && git commit``) + task done. DENY → cascade
    trace + re-enqueue with feedback; mechanical failures (failing
    ``verify_command``, empty output, crashed session) get ONE same-tier retry
    with the error fed back before a tier escalation is spent; LLM DENYs
    escalate a tier immediately; the 2-retry cap blocks the task, blocked
    propagates transitively (``SwarmDal.propagate_blocked``; the integration
    task is exempt and runs over completed work, the summary is marked
    partial). REVIEWER INFRA FAILURE is NOT a DENY: the reviewer is retried
    once, then the task lands blocked-on-review — no retry consumed, never an
    auto-CONFIRM.

RATE LIMITS
    A ``rate_limited`` terminal reports a durable cooldown through the
    ``LimitsPort`` (``limit_state.report_outcome``), emits
    ``provider_switched`` + ``rate_limit``, and re-enqueues WITHOUT consuming
    a retry, capped at ``rate_limit_requeue_cap`` (5) re-enqueues per task.
    All-cooling (router returns ``None``) parks the run in bounded backoff:
    wake at the earliest ``cooldown_until``, capped at
    ``cooldown_wait_cap_seconds`` (15 min) per wait, emitting
    ``rate_limit_stall`` — the run degrades to waiting, never spins, and never
    fails on cooldowns alone (the stall guard treats a park as progress).

BUDGET
    The gate reads live attempt/session spend (``SwarmDal.budget_spend``);
    ``swarm_runs.cost_usd`` remains a terminal-settlement cache/floor. Unknown
    session cost is explicit and surfaced as unenforceable, never counted as
    free. What an issue DOES depends on ``omniagentos.budget.policy`` (the operator,
    2026-07-24: budgets are task-sized guidance, not hard blockers):

    * ADVISORY (default) — a breach or unknown total is recorded and notified
      ONCE, naming the run and the observation. Nothing is blocked and no spawn
      is refused; the plan runs to completion.
    * BLOCK (``OMNIAGENTOS_BUDGET_ENFORCEMENT=block``) — the cap stops NEW
      spawns only. The pre-spawn gate runs once per slot pull, so the overshoot
      is bounded by at most ONE in-flight spawn per free slot (documented
      bound; recorded in the run metrics), every remaining schedulable task is
      blocked, and the summary is forced (run completes partial).

    The pre-spawn gate must NOT requeue in advisory mode: a task that is
    requeued for being over budget, with nothing to lower the cost, is requeued
    forever and the run never terminates.

Same-directory execution model (Phase 1): the workspace MUST be a git checkout
(refused otherwise); ALL git mutations are coordinator-owned and serialized
under a per-run git lock — a pre-attempt pathspec-limited snapshot of
coordinator-owned files, and a pathspec-limited commit after each CONFIRM. The
post-terminal ownership diff (session-reported files, falling back to the
uncommitted git delta) reverts out-of-scope changes from the snapshot and flags
the review. ``PLAN.md`` is regenerated (``planner.render_plan_md`` +
tmp/fsync/rename) on completion, split, and resize.

Worktree isolation (Phase 2, opt-in via ``configs/swarm.yaml``
``worktrees.enabled`` / ``OMNIAGENTOS_SWARM_WORKTREES`` — D4): when the flag
is on and a ``git worktree list`` probe succeeds, each non-integration/
bootstrap task runs in a PRIVATE worktree under
``var/swarm/worktrees/<run_id>/<task_key>`` on branch
``swarm/<run_id>/<task_key>``; workers commit freely there (their Seatbelt
write roots gain the git common dir, with ``.git/hooks``/``.git/config``
still write-denied) and the coordinator merges each branch ``--no-ff`` at
CONFIRM — automatically topological, since eligibility requires all deps
done and done ⇒ merged. A merge CONFLICT still completes the task, leaves
the branch alive, routes the conflict to the integration task's feedback,
and PARKS dependents (``propagate_blocked``, reason ``dep_merge_conflict``,
integration exempt — D5). Non-CONFIRM terminations salvage-commit partial
work on worktree removal; resume verifies worktree existence (missing →
crashed → mechanical requeue from the branch tip) and prunes orphans;
terminal cleanup removes worktrees for every terminal run and deletes the
run's branches ONLY on completed runs (failed runs keep them for forensics).
With the flag off this path is byte-identical to Phase 1 (pinned by test).

Scope locks (Phase 3, opt-in via ``configs/parallelism.yaml`` /
``OMNIAGENTOS_SCOPE_LOCKS`` — default OFF, and byte-identical when off,
pinned by ``tests/swarm/test_scope_wiring.py``): a task's planner-assigned
``owned_paths`` are taken as durable, cross-lane claims in
``resource_locks`` at attempt open and released in ``_execute_item``'s
``finally`` — the one place every terminal path, crash included, funnels
through. A refused claim parks the task (durable FIFO waiter + a short skip
window) instead of executing it: no attempt row, no spawn, no retry
consumed. Separately, and independently of the mode's value for
enforcement, every MAIN-workspace git mutation runs under
``_git_guard``, which adds a cross-PROCESS commit lock to the in-process
``git_lock`` — the latter is a ``threading.RLock`` and never could stop the
API process and the sessions daemon from interleaving a merge on one repo.
See ``omniagentos/swarm/scope_wiring.py`` for the realm choice and the
degrade-vs-wait policy, both of which are load-bearing.

Every collaborator is an injectable seam (spawner, session store, router,
reviewer, splitter, verifier, git, limits, clock) so the whole engine is
fake-spawner tested; the real claude/provider spawners integrate in WP5b.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import logging
import os
import posixpath
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from omniagentos.budget.policy import blocks as budget_blocks
from omniagentos.budget.token_pricing import (
    QUALITY_ESTIMATED,
    QUALITY_EXACT,
    QUALITY_UNKNOWN,
)
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import default_db_path
from omniagentos.path_containment import inode_paths_equal
from omniagentos.roles import job_role_from_swarm_json
from omniagentos.swarm.collision_safety import effective_collision_mode
from omniagentos.swarm.contracts import (
    ACTION_APPROVAL_PARKED,
    ACTION_BRANCH_MERGED,
    ACTION_BUDGET_UNENFORCEABLE,
    ACTION_MERGE_ABORTED,
    ACTION_MERGE_CONFLICT,
    ACTION_MERGE_STARTED,
    ACTION_PROVIDER_SWITCHED,
    ACTION_RATE_LIMIT,
    ACTION_RATE_LIMIT_STALL,
    ACTION_RESIZE,
    ACTION_REVIEW_CONFIRMED,
    ACTION_REVIEW_DENIED,
    ACTION_RUN_COMPLETED,
    ACTION_RUN_FAILED,
    ACTION_RUN_STARTED,
    ACTION_SLOT_OPENED,
    ACTION_SUBTASKS_DENIED,
    ACTION_SUBTASKS_GRANTED,
    ACTION_SUBTASKS_REQUESTED,
    ACTION_TASK_ASSIGNED,
    ACTION_TASK_BLOCKED,
    ACTION_TASK_COMPLETED,
    ACTION_TASK_SPLIT,
    ACTION_WORKTREE_CREATED,
    ACTION_WORKTREE_KEPT,
    TERMINAL_RUN_STATUSES,
    WORKTREE_GITDIR_RULE_LINES,
    SwarmEmitter,
    SwarmPlan,
)
from omniagentos.swarm.dal import (
    _ACTIVE_STATUSES,
    BUDGET_RESERVED_KEY,
    ProjectBudgetSpend,
    RunBudgetSpend,
    SwarmDal,
)
from omniagentos.swarm.plan_safety import (
    VerifierCommandError,
    parse_verifier_command,
    validate_verifier_targets,
)
from omniagentos.swarm.provider_exec import (
    LIVENESS_STATUSES,
    classify_liveness,
    is_making_progress,
)
from omniagentos.swarm.scope_wiring import (
    DEFAULT_COMMIT_WAIT_S,
    TASK_HOLDER_KIND,
    CommitFence,
    ScopeCommitBusy,
    claim_task_scope,
    clear_task_waiter,
    commit_guard,
    coordinator_holder,
    park_task_waiter,
    realm_for,
    release_holder_scope,
    renew_holder_scope,
    scope_locks_active,
)

if TYPE_CHECKING:
    from omniagentos.swarm.worktrees import SwarmWorktreesProto

LOG = logging.getLogger(__name__)

#: The subject half of a canonical holder id, i.e. everything after the
#: `lane:` prefix in `reliability/store.py:_CANONICAL_IDENTITY_RE`. Used to
#: decide whether a formation id can spell `lane:swarm.worker.<formation>`
#: (PLAN.md §1 invariant 1) without minting a non-canonical holder string.
_CANONICAL_LANE_SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Metacog packets may include stored memories, artifacts, and skill bodies.
# Keep the inline worker-prompt view deliberately smaller than source limits.
WORKER_CONTEXT_BYTE_CAP = 800

# Feature flag: WP6a's POST /api/swarm only provisions; run activation stays
# OFF until WP10 flips this (merge safety — see activate_run_if_enabled).
SWARM_EXECUTE_ENV = "OMNIAGENTOS_SWARM_EXECUTE"

_TERMINAL_TASK_STATUSES = frozenset({"done", "blocked", "cancelled"})
_TERMINAL_SESSION_STATES = frozenset({"completed", "failed", "cancelled", "killed"})
_AWAITING_APPROVAL = "awaiting_approval"

# Escalation ladder + tiered attempt timeouts (minutes). Complexity bands are
# simple/standard/complex (plan pass-2 item 22); a task starts at its
# tier_hint/complexity rung and each escalation moves one rung right.
TIER_LADDER: tuple[str, ...] = ("simple", "standard", "complex")
DEFAULT_TIMEOUT_MINUTES: dict[str, float] = {
    "simple": 15.0,
    "standard": 30.0,
    "complex": 60.0,
}

# NESTED-TIMEOUT MONOTONICITY: The timeout ladder has THREE layers, each strictly
# increasing, so the innermost layer (provider session idle) ALWAYS fails first:
#
#   1. Gate spec timeout (600s) — verify_command execution, must be < smallest idle
#   2. Provider session idle timeout — IDLE_TIMEOUT_FRACTION × tier_budget, most specific error
#   3. Coordinator wall deadline — tier_budget, generic "tier timeout" fallback
#
# Example: simple tier (15min = 900s wall deadline):
#   - idle_timeout = 900s × 0.75 = 675s = 11.25min (session goes idle → specific error)
#   - gate_timeout = 600s < 675s (verification stall → caught by idle timer)
#   - wall_deadline = 900s (fallback if both above somehow miss)
#
# Each inner timeout must be strictly less than its parent. Tests and construction
# assertions enforce this invariant; violations are hard failures at startup.
IDLE_TIMEOUT_FRACTION: float = 0.75

# PERIODIC PRE-DEADLINE LIVENESS.
#
# The three layers above all fire at a DEADLINE. That is the right shape for a
# kill decision and the wrong shape for a question: until the wall deadline is
# reached, a wedged agent and a productive one are indistinguishable from
# outside, so time-to-detect for "is it stuck?" was the full tier budget even
# though the freshness signal (``sessions.last_activity_at``, written per output
# chunk by provider_exec's reader) was available the whole time.
#
# So the await loop also classifies on a fixed cadence, INDEPENDENT of the
# deadline. Three properties keep that cheap and safe:
#   * bounded sampling — one DB read per attempt per LIVENESS_POLL_SECONDS,
#     regardless of how fast the await loop itself polls;
#   * change-only emission — an event per state TRANSITION, never per tick, so a
#     long healthy attempt costs exactly one event;
#   * OBSERVE-ONLY — a pre-deadline tick never kills, never escalates a tier and
#     never touches the attempt. Killing stays where it already is, behind the
#     wall deadline. A classifier that reaped would make its own false-positive
#     rate expensive before that rate has ever been measured.
LIVENESS_POLL_SECONDS: float = 60.0

# swarm.event actions for the liveness tick. Deliberately NOT added to
# contracts.SWARM_EVENT_ACTIONS by this change: that tuple is frozen tail-first
# by tests/contracts/test_mission_contracts.py (the group_* actions must remain
# the last five), and StoreSwarmEmitter records unknown actions rather than
# dropping them, so the events are durable today and registering the vocabulary
# is a separate, contract-owning change.
ACTION_LIVENESS_CHANGED = "liveness_changed"
ACTION_LIVENESS_SUMMARY = "liveness_summary"


# Per-run hard clamp on target_n. MUST equal swarm.planner.TARGET_N_HARD_CEILING:
# the planner caps what it writes into swarm_runs.target_concurrency, this caps
# what the coordinator will actually run, and a mismatch means one of the two is
# dead config (a lower value here silently clamps every wide plan back down).
# tests/swarm/test_fleet_scale.py asserts the equality.
MAX_SLOTS = 20
DEFAULT_RETRY_CAP = 2  # consumed retries before a task blocks
DEFAULT_RATE_LIMIT_REQUEUE_CAP = 5  # rate-limit re-enqueues per task
DEFAULT_COOLDOWN_WAIT_CAP_SECONDS = 15 * 60.0
DEFAULT_STALL_MINUTES = 30.0
DEFAULT_ADOPT_STALE_MINUTES = 2.0

# Coordinator-written files never count against a worker's ownership diff and
# are the complete positive entitlement for pre-attempt snapshot commits.
_COORDINATOR_FILES = frozenset({"PLAN.md"})

# How long a task whose declared scope was REFUSED is skipped before a worker
# offers to claim it again. Without a skip window the refusal is instant, the
# claim is released, and the same worker re-picks the same task on its next
# pull — a hot loop against SQLite for as long as the blocker holds. Only ever
# non-empty when scope locking is enforcing.
DEFAULT_SCOPE_RETRY_SECONDS = 5.0


# --- the retry cap reads what the pumps write (109) -----------------------------
#
# `DEFAULT_RETRY_CAP` shipped, was tested, and governed NOTHING outside this
# scheduler: rework-pump.sh and sim-pump.sh never touched the ledger, and
# review-pump.sh / verdict-pump.sh only read `fleet-ledger.py`. A pump that
# writes a row nothing reads buys nothing, and a cap that reads a row nothing
# writes is the same defect from the other end — so the pumps now write
# `swarm_attempts` rows carrying `verdict_hash`, and BOTH the scheduler's
# `_consume_retry` and every pump's pre-dispatch gate go through the functions
# below. One reader, one writer, one cap.
#
# Only `verdict_hash IS NOT NULL` rows are counted (migration 109 spells out
# why): the scheduler's own executor leaves the column NULL, so the free
# mechanical retry and the rate-limit re-enqueues that consume no retry keep
# behaving exactly as they did before 109. This is wiring, not a policy change.


def pump_attempt_count(dal: Any, board_task_id: str) -> int:
    """CONSECUTIVE unproductive pump dispatches for this task.

    Counted since the last attempt that ended ``completed``, not over the
    lifetime. A lifetime count would block a HEALTHY lane on its fourth
    successful pass, which is not a cap, it is an expiry date — and it would
    make the sim pump (a designed, indefinite prober) self-terminate after
    three dispatches. What the measured runaway looked like was 726
    dispatches with ZERO completions; consecutive-since-success is exactly the
    shape that catches it and nothing else. A live (unclosed) row counts: an
    in-flight dispatch is spent, not free.

    Duck-typed on purpose: the scheduler is constructed with test doubles and
    alternate DALs, and a cap that raised AttributeError on a stub would fail
    in the worst possible direction — it would abort the retry path that is
    supposed to be terminating the loop. A DAL that cannot answer reports 0,
    which reproduces exactly the pre-109 behaviour.
    """
    reader = getattr(dal, "pump_attempts", None)
    if reader is None:
        return 0
    try:
        rows = reader(board_task_id)
    except Exception:  # noqa: BLE001 -- an unreadable ledger must not break scheduling.
        logging.getLogger(__name__).debug(
            "pump attempt count unavailable for %s", board_task_id, exc_info=True
        )
        return 0
    consecutive = 0
    for row in rows:  # oldest-first
        consecutive = 0 if str(row.get("end_reason") or "") == "completed" else consecutive + 1
    return consecutive


def effective_retry_count(dal: Any, board_task_id: str, *, swarm_json_retries: int = 0) -> int:
    """Attempts this task has genuinely consumed, from BOTH accounting spines.

    `max`, not `sum`: the scheduler's own counter and the pump ledger describe
    the same task from two vantage points, and adding them would double-count a
    dispatch the scheduler had already booked.
    """
    return max(int(swarm_json_retries) + 1, pump_attempt_count(dal, board_task_id))


def retry_cap_exceeded(
    dal: Any,
    board_task_id: str,
    *,
    swarm_json_retries: int = 0,
    retry_cap: int = DEFAULT_RETRY_CAP,
) -> bool:
    """True when this task has spent its retries and must not dispatch again."""
    return effective_retry_count(dal, board_task_id, swarm_json_retries=swarm_json_retries) > int(
        retry_cap
    )


def swarm_execute_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether run activation is switched on.

    ``env`` makes the default state probe-able; omitted uses ``os.environ``.
    The flag defaults OFF.

    Only ``1``, ``true``, ``yes``, and ``on`` enable execution; all other
    values remain disabled.
    Matching is whitespace-insensitive and case-insensitive.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return source.get(SWARM_EXECUTE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _cap_cost_quality(known_breach: bool, accrued_breach: bool) -> str:
    """Which evidence carried a dollar-cap decision.

    ``exact``     measured dollars alone crossed the ceiling.
    ``estimated`` only tokens priced from published rates crossed it -- the cap
                  is enforceable, but the total is still not known.
    ``unknown``   nothing crossed it; the issue is the missing price itself.

    Same vocabulary as ``provider_call_usage.cost_quality`` so one word means
    one thing across the whole ledger.
    """
    if known_breach:
        return QUALITY_EXACT
    if accrued_breach:
        return QUALITY_ESTIMATED
    return QUALITY_UNKNOWN


# ---------------------------------------------------------------------------
# Seams (all injectable; fake-spawner tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDecision:
    """The router's pick for one attempt (WP5b replaces the default router).

    ``reservation_id`` (WP5b additive) carries the ``limit_state`` account
    reservation the router made for ``account_id``; the spawner converts it to
    real inflight at launch and releases it on spawn failure. ``None`` when the
    route needed no reservation (default-login provider or fake routers).

    ``effort`` (048 additive) is the router-decided reasoning effort for the
    attempt (``configs/swarm.yaml`` ``router.effort_by_tier`` +
    ``router.effort_overrides``). Recorded on the ``swarm_attempts`` row at
    open_attempt and threaded to the spawner; ``None`` means "provider/session
    default" (fake routers, and pre-effort decisions)."""

    provider: str = "claude"
    model: str = "sonnet"
    tier: str = "standard"
    account_id: str | None = None
    reservation_id: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class SpawnRequest:
    """Everything a spawner needs to launch one attempt session.

    The REAL spawner (WP5b ``provider_exec`` / SessionSupervisor path) MUST:

    - pass ``--disallowedTools Task`` (and the provider equivalents) so a
      swarm worker can never fan out sub-agents of its own — the scheduler is
      the only parallelism authority;
    - title the session with the ``[swarm:<attempt_id>]`` ownership marker
      (longhaul's idiom) so the supervisor's auth-retry/broken-login notifiers
      and the A2 reaper leave swarm sessions to this scheduler — the SOLE
      respawner (one death → exactly one successor);
    - honor ``idle_minutes`` and ``budget_usd_max`` on the session row so the
      A2 reaper enforces scheduler policy instead of racing it.
    """

    run_id: str
    task_id: str
    task_key: str
    attempt_id: str
    working_dir: str
    prompt: str
    provider: str
    model: str
    tier: str
    account_id: str | None = None
    idle_minutes: float = 30.0
    budget_usd_max: float | None = None
    # WP5b additive: the router's limit_state reservation for account_id.
    # The spawner converts it at launch / releases it on spawn failure.
    reservation_id: str | None = None
    # 048 additive: router-decided reasoning effort. codex threads it as the
    # `-c model_reasoning_effort="..."` config token, grok as
    # `--reasoning-effort`; kimi/gemini/qwen have no CLI knob (skipped cleanly) and
    # the claude bridge runs at session default — the decided value is still
    # recorded on the attempt row for all of them.
    effort: str | None = None
    # Phase-2 worktree isolation additive: extra Seatbelt write roots beyond
    # the working dir. In worktree mode this carries the main repo's git
    # COMMON dir — without it every worker `git commit` inside a worktree
    # dies EPERM (objects/refs live in the main .git, outside the worktree
    # write root). The sandbox profile still denies .git/hooks + .git/config
    # inside a granted git dir (runner.sandbox.project_config_write_deny_targets).
    extra_write_roots: tuple[str, ...] = ()
    # PKG-REQUEST-SUBTASKS additive: the ATTEMPT-BOUND absolute path at which a
    # worker may write its subtasks_request.json (feature on → set; off → None).
    # The spawner re-appends the fan-out protocol section to relay/continuation
    # prompts using this value, so a rate-limited/timed-out predecessor's
    # successor still carries the instruction with ITS OWN attempt filename.
    subtasks_request_path: str | None = None


class SwarmSpawnerProto(Protocol):
    """Injectable spawner seam: returns the spawned session id."""

    def spawn(self, request: SpawnRequest) -> str: ...


class SessionStoreProto(Protocol):
    """The slice of ``SessionsDal`` the scheduler monitors sessions through."""

    def get_session(self, session_id: str) -> dict[str, Any] | None: ...

    def request_kill(self, session_id: str, *, killed_by: str | None = None) -> Any: ...


class SwarmRouterProto(Protocol):
    """Route seam (WP5b: modelintel + lineage map + risk pin + pressure filter).

    Returns ``None`` when NO provider currently has capacity (all cooling) —
    the coordinator then parks the run in bounded backoff instead of spinning.
    """

    def route(self, task: Mapping[str, Any], tier: str) -> RouteDecision | None: ...


ReviewVerdictKind = Literal["confirm", "deny", "error"]


@dataclass(frozen=True)
class SwarmReviewOutcome:
    """Three-valued review result. ``error`` = reviewer INFRASTRUCTURE failure
    (adapter down, unparseable output) — explicitly NOT a deny and never an
    auto-confirm."""

    verdict: ReviewVerdictKind
    feedback: str = ""
    reviewer: str = ""
    # True when the reviewer ALREADY made more than one attempt (failed over
    # across lineages and/or issued its corrective re-prompt). The scheduler's
    # outer "retry the reviewer once" must not then re-run the whole chain —
    # that multiplies invocations (2x for infra, 4x for drift) to repeat work
    # whose outcome is already known. Defaults False so an outcome built
    # anywhere else keeps the original single-retry semantics, which is the
    # right behaviour for a single-attempt failure that may be transient.
    exhausted: bool = False


class SwarmReviewerProto(Protocol):
    def review(
        self,
        *,
        task: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
        verify_output: str,
        flags: Sequence[str],
    ) -> SwarmReviewOutcome: ...


class SwarmGitProto(Protocol):
    """Coordinator-owned git surface (Phase 1 same-directory model)."""

    def is_checkout(self, working_dir: str) -> bool: ...

    def pre_attempt_dirty_paths(self, working_dir: str) -> list[str]: ...

    def snapshot(
        self,
        working_dir: str,
        message: str,
        coordinator_paths: Sequence[str],
        expected_digests: Mapping[str, str] | None = None,
        expected_modes: Mapping[str, str] | None = None,
    ) -> str: ...

    def changed_paths(self, working_dir: str) -> list[str]: ...

    def revert_paths(self, working_dir: str, snapshot_sha: str, paths: Sequence[str]) -> None: ...

    def commit_paths(self, working_dir: str, paths: Sequence[str], message: str) -> str | None: ...


class LimitsPortProto(Protocol):
    """Durable limit-state surface (defaults to ``routing.limit_state``)."""

    def report_rate_limited(
        self, provider: str, account_id: str | None, detail: str, reset_at: str | None
    ) -> None: ...

    def earliest_cooldown_until(self) -> datetime | None: ...

    def swarm_slots_remaining(self) -> int:
        """SIGNED swarm-fleet headroom: how many more swarm sessions the fleet
        ledger allows right now (negative when the ceiling dropped below the
        current live count — the signed value is what lets fair_share SHRINK
        a run below its own current width)."""
        ...


class SchedulerClock:
    """Injectable time: wall clock, monotonic, and sleep (fake in tests)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(max(0.0, seconds))


# splitter seam: (task_row, swarm_json) -> list of ≤4 subtask spec dicts
# ({title, description, owned_paths, complexity, est_agent_minutes,
#   verify_command, acceptance}) or None on failure.
TaskSplitter = Callable[[Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]] | None]
# verifier seam: (task_row, swarm_json, working_dir) -> (ok, output)
TaskVerifier = Callable[[Mapping[str, Any], Mapping[str, Any], str], tuple[bool, str]]
# terminal classifier seam: session row -> completed|rate_limited|auth_failed|crashed|killed
TerminalClassifier = Callable[[Mapping[str, Any]], str]


# ---------------------------------------------------------------------------
# Default seam implementations
# ---------------------------------------------------------------------------


class DefaultSwarmRouter:
    """Fallback/test default (claude, session model by tier); production injects SwarmRouter at scheduler.py:6831.

    WP5b replaces this with the full chain (modelintel route → lineage→provider
    map from configs/swarm.yaml → risk_class pin → provider_pressure filter →
    learned start tier). This default never reports all-cooling — cooldown
    awareness arrives with the WP5b router; the scheduler's park machinery is
    exercised through the injectable seam.
    """

    _MODELS = {"simple": "sonnet", "standard": "sonnet", "complex": "opus"}

    def route(self, task: Mapping[str, Any], tier: str) -> RouteDecision | None:
        del task
        return RouteDecision(provider="claude", model=self._MODELS.get(tier, "sonnet"), tier=tier)


def _balanced_json_objects(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every top-level ``{...}`` span, string/escape aware.

    Deliberately hand-rolled rather than regex: a reviewer's ``feedback`` value
    routinely contains braces and escaped quotes, and a greedy/lazy pattern
    either swallows the tail or stops inside a string literal.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, index + 1))
                start = -1
    return spans


def _stands_alone(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is the ONLY thing on its own line(s).

    This is the whole fail-closed test for an embedded object. A reviewer that
    ends with its verdict on its own line — or inside a ```json fence, which is
    the same shape — is making an utterance. A reviewer that writes

        The worker provided {"verdict":"confirm",...} but that is not my verdict.

    is QUOTING, and reading that as a CONFIRM is a fail-OPEN that merges unre-
    viewed work. Prose on the same line, either side, disqualifies the span.
    """
    before = text.rfind("\n", 0, start)
    if text[before + 1 : start].strip():
        return False
    after = text.find("\n", end)
    tail = text[end:] if after == -1 else text[end:after]
    return not tail.strip()


# Reviewer names that ARE concrete ``--model`` ids for their CLI, so pinning
# them makes the recorded reviewer match what actually ran. Everything else
# (``cli-codex``, ``sol``, ``fable``-style logical/harness aliases whose CLI
# spelling is not established here) is left unset ON PURPOSE: a wrong --model
# makes the CLI refuse to start, which is a worse failure than the attribution
# gap it would close. Grow this map from evidence, never from a guess.
_REVIEWER_MODEL_IDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # adapters/claude.py passes these straight through to `claude --model`
        # (the default is "sonnet"; "fable" is exercised by the live planner).
        "anthropic": frozenset({"opus", "sonnet", "haiku", "fable"}),
    }
)
_REVIEWER_MODEL_PREFIXES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "openai": ("gpt-",),  # adapters/codex.py default: gpt-5.6-luna
        "xai": ("grok-",),  # adapters/grok.py default: grok-4.5
        "google": ("gemini-", "gemma-"),
        "alibaba": ("qwen-",),
        "moonshot": ("kimi-",),
    }
)


# ``risk_class`` values the PLANNER really emits for work that cannot be
# quietly undone (planner.py stamps task.risk_class into every child card's
# swarm_json; the plan schema's enum is none|external|deploy|destructive).
HIGH_BLAST_RADIUS_RISK_CLASSES: Final[frozenset[str]] = frozenset({"deploy", "destructive"})


def _high_blast_radius(swarm_json: Mapping[str, Any]) -> bool:
    """Whether reviewer substitution is forbidden for this task.

    ``review_surface`` is checked first because the assignment path already
    honours it — but NOTHING in production writes it (the planner does not emit
    it; only direct callers do), so gating solely on it made the rule dead
    config that no real task could ever reach. ``risk_class`` is the signal that
    genuinely arrives over the planner -> provision_run -> swarm_json path, so
    it is what actually decides this.
    """
    if str(swarm_json.get("review_surface") or "standard").strip().lower() in (
        "security",
        "verification",
    ):
        return True
    return str(swarm_json.get("risk_class") or "").strip().lower() in HIGH_BLAST_RADIUS_RISK_CLASSES


def _reviewer_model_id(candidate: str, lineage: str) -> str | None:
    """The ``--model`` id to pin for a reviewer name, or None to use the default."""
    name = candidate.strip()
    if name in _REVIEWER_MODEL_IDS.get(lineage, frozenset()):
        return name
    if name.startswith(_REVIEWER_MODEL_PREFIXES.get(lineage, ())):
        return name
    return None


def _extract_verdict_payload(text: str) -> dict[str, Any] | None:
    """Verdict object from a reviewer's raw text — exact match PREFERRED.

    Acceptance is deliberately narrow, because the failure modes are asymmetric:
    refusing a real verdict costs one corrective re-prompt, while accepting a
    quoted one merges unreviewed work.

    1. The entire response parsed as JSON — what the contract asks for.
    2. Otherwise the LAST balanced object with a ``verdict`` key that STANDS
       ALONE on its own line(s) (``_stands_alone``). That admits the two shapes
       a cooperating model actually produces — a trailing object after a
       preamble, and a ```json fence — and rejects mid-sentence quotation.

    Anything else returns None, which the caller classifies as FORMAT DRIFT and
    answers with a corrective re-prompt rather than a guess.

    Residual, documented: a model that emits a syntactically perfect verdict
    object alone on a line while meaning something else would still be read as
    its verdict. That is far narrower than "anywhere in the body", and the
    schema-repair and corrective-re-prompt paths both sit in front of it.

    A non-verdict dict is still returned when the whole body parsed cleanly, so
    a structurally valid reply with a nonsense verdict keeps reaching the
    invalid-verdict path instead of silently degrading to "unparseable".
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        exact = json.loads(stripped)
    except json.JSONDecodeError:
        exact = None
    if isinstance(exact, dict) and "verdict" in exact:
        return exact
    for start, end in reversed(_balanced_json_objects(stripped)):
        if not _stands_alone(stripped, start, end):
            continue
        try:
            decoded = json.loads(stripped[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "verdict" in decoded:
            return decoded
    return exact if isinstance(exact, dict) else None


class CrossLineageSwarmReviewer:
    """Default reviewer: the CrossLineageReviewer posture (cheap NON-Anthropic
    ``cli-codex`` adapter, read-only, same JSON verdict schema) with the ONE
    swarm-critical difference: infrastructure failure is surfaced as
    ``verdict="error"`` instead of the orchestrator reviewer's fail-open
    CONFIRM — WP5a's review contract forbids an auto-CONFIRM when the reviewer
    infrastructure is down (plan refinement item 11).

    Two properties exist because of a class of outage seen with logged-out
    reviewer CLIs (dozens of blocked attempts piling up within an hour):

    INFRASTRUCTURE FAILURE IS NAMED, NOT GUESSED AT
        ``configs/formations.yaml`` pins ``reviewer: grok`` for the coding and
        operations formations; the grok CLI was logged out, so every review
        exited non-zero with ``{"type":"error","message":"Not signed in..."}``
        and ``AgentResult.status == ERROR``. This class read only
        ``output_json``/``output_text`` — both empty on an ERROR result — and
        reported ``reviewer returned no parseable verdict`` for all of them.
        The true cause sat unread in ``AgentResult.error`` and in the
        transcript at ``AgentResult.log_path``. Both are now surfaced verbatim.

    A DEAD HARNESS FAILS OVER, IT DOES NOT RETRY ITSELF
        The scheduler's "retry the reviewer once" re-invoked the SAME dead CLI
        with the SAME prompt, so "failed twice" was arithmetically guaranteed
        and 100% of reviews blocked until a human re-authenticated. An
        infrastructure failure now advances to the next candidate of a
        DIFFERENT lineage (still never the implementer's own), because a
        logged-out provider is exactly the situation a cross-lineage pool is
        for. Format drift is treated as the opposite kind of failure: the same
        reviewer is re-asked ONCE with the output contract pinned.

    Fail-closed is unchanged: when no legal reviewer can produce a verdict the
    outcome is still ``verdict="error"`` — never an auto-CONFIRM — only now the
    feedback says which harnesses were tried and why each one failed.
    """

    # Kept as the trailing line of the review prompt. ``tests/entrypoints``
    # detects a review invocation by the presence of ``"verdict"`` and
    # "CONFIRM or DENY"; both survive here by construction.
    _CONTRACT = (
        'Return JSON and NOTHING else: {"verdict": "confirm"|"deny", "feedback": "..."}. '
        '"verdict" MUST be exactly "confirm" or "deny" (lowercase). '
        "No prose before or after it, no markdown fence."
    )
    _CORRECTIVE = (
        "Your previous reply could not be read as a verdict. Reply with a single "
        'JSON object and nothing else: {"verdict": "confirm", "feedback": "..."} '
        'or {"verdict": "deny", "feedback": "..."}. '
        '"verdict" MUST be exactly the word "confirm" or the word "deny".'
    )
    # Per-harness budget for the blocked-attempt detail. swarm_attempts.detail
    # is itself truncated to 500 by the caller, so keeping each harness's line
    # short is what lets ALL of them survive into the row an operator reads.
    _DIAGNOSIS_CHARS = 400

    def __init__(self, adapter: Any | None = None) -> None:
        self._adapter = adapter
        # Per-invocation index for unique reviewer run_ids: run_id names the
        # adapter's var/logs/<run_id>/ transcript dir, and a task-only id made
        # every retry OVERWRITE the earlier review transcript (the seq-0/1
        # denial forensics of run swr_850835 were lost exactly this way).
        self._invocation_counter = itertools.count(1)

    def _resolve(
        self,
        swarm_json: Mapping[str, Any] | None = None,
        *,
        reviewer: str | None = None,
    ) -> Any:
        if self._adapter is not None:
            return self._adapter
        from omniagentos.adapters.registry import resolve_adapter
        from omniagentos.contracts import HarnessType
        from omniagentos.formation.lineage import (
            ReviewerAssignmentError,
            lineage_for_model,
        )

        # Preserve the declared reviewer's lineage all the way to execution.
        # The former special case sent ``fable`` through CLI_CODEX, turning a
        # declared Anthropic reviewer into an actual OpenAI reviewer after the
        # assignment check.
        #
        # ``reviewer`` is the ASSIGNED name when the caller has one. Reading
        # ``formation_reviewer`` here while reporting the assigned label to the
        # database let the two diverge silently: the row said one reviewer, the
        # transcript was written by another.
        reviewer = str(
            reviewer
            if reviewer is not None
            else ((swarm_json or {}).get("formation_reviewer") or "cli-codex")
        ).strip()
        lineage = lineage_for_model(reviewer)
        harness_by_lineage = {
            "anthropic": HarnessType.CLI_CLAUDE,
            "openai": HarnessType.CLI_CODEX,
            "xai": HarnessType.CLI_GROK,
            "google": HarnessType.CLI_GEMINI,
            "alibaba": HarnessType.CLI_QWEN,
            "moonshot": HarnessType.CLI_KIMI,
        }
        harness = harness_by_lineage.get(lineage)
        if harness is None:
            raise ReviewerAssignmentError(
                f"reviewer {reviewer!r} has known lineage {lineage!r} "
                "but no executable reviewer harness; refusing"
            )
        return resolve_adapter(harness)

    def _prompt(
        self,
        *,
        task: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
        verify_output: str,
        flags: Sequence[str],
        corrective: str | None = None,
    ) -> str:
        lines = [
            "You are a SEPARATE, cross-lineage REVIEWER (read-only). CONFIRM or DENY",
            "whether the swarm worker's output satisfies the task acceptance criteria.",
            "",
            "## Task",
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            "",
            "## Acceptance",
            str(swarm_json.get("acceptance") or "(none recorded)"),
        ]
        if flags:
            lines += ["", "## Flags (address explicitly)"]
            lines += [f"- {flag}" for flag in flags]
        if verify_output:
            lines += ["", "## Verify command output", verify_output[:4000]]
        lines += [
            "",
            "## Worker output",
            str(session.get("output_text") or "(no textual output)")[:8000],
            "",
            self._CONTRACT,
        ]
        if corrective:
            lines += ["", "## Output contract (your previous reply violated it)", corrective]
        return "\n".join(lines)

    def _probe(
        self,
        *,
        candidate: str,
        task: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
        verify_output: str,
        flags: Sequence[str],
        working_dir: str | None,
        corrective: str | None,
    ) -> tuple[str, dict[str, Any] | None, str]:
        """Run ONE reviewer invocation.

        Returns ``(kind, payload, diagnosis)`` where kind is:

        * ``"ok"``    — payload carries a valid confirm/deny verdict;
        * ``"infra"`` — the harness itself could not answer (raised, non-zero
          exit, timeout, auth failure). Another lineage may still work;
        * ``"drift"`` — the harness ran fine but the FORMAT is wrong. Another
          lineage would drift too; the fix is to re-ask this one, corrected.

        Collapsing these two into one bucket is what produced a day of
        identical, unactionable "no parseable verdict" blocks.
        """
        from omniagentos.contracts import AgentInput, ResultStatus
        from omniagentos.formation.lineage import LineageAssignmentError, lineage_for_model
        from omniagentos.orchestrator.review import _REVIEW_SCHEMA

        # Attribution must match execution: recording reviewer "opus" while the
        # claude adapter silently ran its "sonnet" default (AgentInput.model
        # unset) makes the audit trail say a review happened that did not.
        try:
            model = _reviewer_model_id(candidate, lineage_for_model(candidate))
        except LineageAssignmentError:
            model = None
        # Unique per invocation (task + attempt session + retry index) so
        # retried reviews never overwrite an earlier transcript's log dir.
        # The per-process counter restarts at 1 after a crash-resume, so a
        # short random entropy component (filesystem-safe hex) keeps run_ids
        # unique ACROSS process restarts too — a resumed coordinator's review
        # must never overwrite an earlier process's transcript dir.
        run_id = (
            f"swarm-review-{str(task.get('id'))[:16]}"
            f"-{str(session.get('id') or 'na')[:12]}"
            f"-r{next(self._invocation_counter)}"
            f"-{uuid.uuid4().hex[:6]}"
        )
        try:
            adapter = self._resolve(swarm_json, reviewer=candidate)
            out = adapter.run(
                AgentInput(
                    run_id=run_id,
                    task_id=str(task.get("id") or ""),
                    prompt=self._prompt(
                        task=task,
                        swarm_json=swarm_json,
                        session=session,
                        verify_output=verify_output,
                        flags=flags,
                        corrective=corrective,
                    ),
                    working_dir=working_dir,
                    model=model,
                    output_schema=_REVIEW_SCHEMA,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- infra failure, NOT a deny.
            LOG.warning("swarm reviewer %s infrastructure failed: %s", candidate, exc)
            return "infra", None, f"{candidate}: harness raised: {exc}"

        # The adapter's OWN status is the first-class infrastructure signal and
        # was previously discarded outright. A logged-out CLI returns
        # status=ERROR with an empty output_text, so reading only the text made
        # "Not signed in. To authenticate ... run `grok login`" indistinguishable
        # from a model that rambled. ``status`` is absent on the lightweight
        # doubles used by the scheduler tests; a missing status stays OK.
        raw = str(getattr(out, "output_text", "") or "")
        status = getattr(out, "status", None)
        status_name = str(getattr(status, "value", status))
        if status is not None and status_name != ResultStatus.OK.value:
            detail = str(getattr(out, "error", "") or "").strip() or "no error detail reported"
            log_path = str(getattr(out, "log_path", "") or "")
            where = f" (transcript: {log_path})" if log_path else ""
            # ERROR is overloaded by the adapter layer. ``CliAdapter.run``
            # returns ERROR **with the model's own text in output_text** when
            # structured-output repair fails (adapters/common.py, the
            # `output_json is None` branch after the repair invocation) — that
            # is a model that ANSWERED BADLY, and failing it over to another
            # lineage just reproduces the drift somewhere else. A dead harness
            # (non-zero exit, timeout, raised exception) carries no output_text
            # at all. The presence of substantive text is therefore the exact
            # discriminator, and it maps to the treatment each one needs:
            # drift -> corrective re-prompt here, infra -> next lineage.
            if status_name == ResultStatus.ERROR.value and raw.strip():
                return (
                    "drift",
                    None,
                    f"{candidate}: harness rejected the output "
                    f"({detail}); raw output: {raw.strip()[:300]!r}",
                )
            LOG.warning("swarm reviewer %s harness unusable: %s", candidate, detail)
            return (
                "infra",
                None,
                f"{candidate}: harness status {status_name}: {detail}{where}",
            )

        payload = getattr(out, "output_json", None)
        if not isinstance(payload, dict):
            payload = _extract_verdict_payload(raw)
        if not isinstance(payload, dict):
            return (
                "drift",
                None,
                f"{candidate}: returned no parseable verdict; raw output: {raw.strip()[:300]!r}",
            )
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict not in ("confirm", "deny"):
            return (
                "drift",
                None,
                f"{candidate}: returned invalid verdict {verdict!r}",
            )
        return "ok", {"verdict": verdict, "feedback": str(payload.get("feedback", "")).strip()}, ""

    def review(
        self,
        *,
        task: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
        verify_output: str,
        flags: Sequence[str],
    ) -> SwarmReviewOutcome:
        from omniagentos.formation.lineage import (
            LineageAssignmentError,
            assign_reviewer,
            lineage_for_model,
        )

        # The candidate POOL must span lineages, not contain a single fixed default.
        #
        # Passing only `formation_reviewer or "cli-codex"` means a codex/openai
        # implementer is offered exactly one reviewer of its OWN lineage, so the
        # cross-lineage rule correctly refuses — and the attempt is BLOCKED. That is
        # a refusal on the DEFAULT path: measured in the simharness as
        # `seq=0 crashed -> seq=1 blocked`, turning a recoverable malformed-JSON run
        # into a failed one for every codex implementer.
        #
        # The declared reviewer stays first (an explicit choice is honoured when it
        # is legal); the rest are fallbacks of other lineages. assign_reviewer picks
        # the first legal one, and still refuses if NONE differ — which is the
        # property worth keeping.
        implementer = str(swarm_json.get("implementer_model") or "")
        surface = str(swarm_json.get("review_surface") or "standard")
        declared = str(swarm_json.get("formation_reviewer") or "").strip()
        pool = [c for c in (declared, "cli-kimi", "cli-codex", "cli-claude", "cli-grok") if c]
        reviewer_label = assign_reviewer(
            implementer=implementer,
            candidates=pool,
            surface=surface,
        )[0]
        # FAILOVER CHAIN — the primary, then one legal reviewer per remaining
        # lineage. Built here rather than in the scheduler's retry because the
        # scheduler's retry cannot know WHICH failure it is retrying: re-running
        # a logged-out CLI is guaranteed to fail again ("failed twice" repeats
        # identically against the same unauthenticated CLI every time).
        #
        # Excluded on purpose:
        #   * an injected adapter — there is exactly one harness to try, and
        #     hammering a test double N times is not a failover;
        #   * high-blast-radius tasks — substituting a stand-in reviewer for a
        #     declared one is the favourable-default this repo exists to remove,
        #     and on work that cannot be undone the honest answer to "the
        #     declared reviewer is down" is to block, not to improvise.
        chain = [reviewer_label]
        if self._adapter is None and not _high_blast_radius(swarm_json):
            seen = {lineage_for_model(reviewer_label)}
            for candidate in pool:
                # LineageAssignmentError is the BASE: an unregistered name raises
                # UnknownModelLineageError, which is a sibling of
                # ReviewerAssignmentError, not a subclass of it. Catching the
                # narrow type would let an unknown fallback escape as a crash
                # instead of being skipped.
                try:
                    candidate_lineage = lineage_for_model(candidate)
                except LineageAssignmentError:
                    continue
                if candidate_lineage in seen:
                    continue
                try:
                    # Re-run the real policy per candidate: a fallback must be
                    # as independent of the implementer as the primary was.
                    assign_reviewer(
                        implementer=implementer,
                        candidates=[candidate],
                        surface=surface,
                    )
                except LineageAssignmentError:
                    continue
                seen.add(candidate_lineage)
                chain.append(candidate)
        # The reviewer MUST run in the task's workspace or it cannot read the
        # files it is judging (live failure swr_ce8dda: "app.js is outside the
        # review workspace" -- workers' real output review_denied because
        # AgentInput had no working_dir and the adapter refused the cwd
        # fallback by design).
        working_dir = str(session.get("project_dir") or "") or None
        # Pre-review workspace stat from THIS (unsandboxed) process: a missing
        # workspace is an INFRASTRUCTURE failure, never the worker's fault — it
        # must surface as verdict="error" (retry the reviewer / block on
        # review), not let a broken environment produce a syntactically valid
        # DENY that burns worker retries (live failure swr_850835: 3 tasks hit
        # the retry cap on reviews that physically could not read anything).
        if working_dir is not None and not os.path.isdir(working_dir):
            return SwarmReviewOutcome(
                verdict="error",
                feedback=f"reviewer workspace missing: {working_dir}",
                reviewer="cli-codex",
            )

        def probe(candidate: str, corrective: str | None) -> tuple[str, dict[str, Any] | None, str]:
            return self._probe(
                candidate=candidate,
                task=task,
                swarm_json=swarm_json,
                session=session,
                verify_output=verify_output,
                flags=flags,
                working_dir=working_dir,
                corrective=corrective,
            )

        failures: list[str] = []
        attempts = 0
        for candidate in chain:
            attempts += 1
            kind, payload, diagnosis = probe(candidate, None)
            if kind == "drift":
                # FORMAT drift, not a dead harness: re-ask the SAME reviewer with
                # the contract pinned. This is the retry the old code claimed to
                # perform and never did — it re-sent a byte-identical prompt, so
                # a model that misread the contract once misread it twice.
                first = diagnosis
                attempts += 1
                kind, payload, diagnosis = probe(candidate, self._CORRECTIVE)
                if kind != "ok":
                    diagnosis = f"{first}; after corrective re-prompt: {diagnosis}"
            if kind == "ok" and payload is not None:
                return SwarmReviewOutcome(
                    verdict=payload["verdict"],  # type: ignore[arg-type]
                    feedback=payload["feedback"],
                    reviewer=candidate,
                )
            # Bound EACH harness's diagnosis, not the joined string. A wholesale
            # tail-trim let the first harness's verbose error consume the budget
            # and delete the later ones — which are precisely the lines that say
            # whether the failover found anything better.
            failures.append(diagnosis[: self._DIAGNOSIS_CHARS])
        # Fail-closed. Every legal reviewer was tried and none produced a
        # verdict, so this blocks — but the feedback now names each harness and
        # its actual error instead of one indistinguishable sentence.
        return SwarmReviewOutcome(
            verdict="error",
            feedback=" | ".join(f for f in failures if f)
            or "reviewer returned no parseable verdict",
            reviewer=reviewer_label,
            # More than one invocation already happened, so the scheduler's
            # outer retry would only repeat known-failing work.
            exhausted=attempts > 1,
        )


class SubprocessSwarmGit:
    """Real git ops via subprocess; identity pinned so commits never depend on
    the ambient git config. All calls are made under the run's git lock.

    Hooks are disabled (``-c core.hooksPath=``) on EVERY call (m9, defense in
    depth mirroring ``SubprocessSwarmWorktrees``): these are the coordinator's
    mechanical plumbing commits (snapshots, pathspec confirms, salvage,
    reverts) — a repo's commit/checkout hooks firing inside them would make
    them nondeterministic, and in worktree mode a worker with the git common
    dir writable must never be able to plant a hook the coordinator then
    executes (the Seatbelt profile denies ``.git/hooks`` writes; this is the
    second layer)."""

    _IDENTITY = (
        "-c",
        "user.email=00000000+omniagentos-bot[bot]@users.noreply.github.com",
        "-c",
        "user.name=OmniAgentOS Swarm",
    )
    _NO_HOOKS = ("-c", "core.hooksPath=")
    # M2b: bounded index.lock retry budget for the CONFIRM-path commit — an
    # A2-reaped worker can die mid-write and leave the lock behind; a
    # reviewer-CONFIRMED commit must not crash on the first EEXIST.
    _LOCK_RETRY_ATTEMPTS = 3
    _LOCK_RETRY_SLEEP = 0.5

    def _git(
        self,
        working_dir: str,
        *args: str,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed argv, never shell
            ("git", "-C", working_dir, *self._IDENTITY, *self._NO_HOOKS, *args),
            capture_output=True,
            text=True,
            timeout=120,
            check=check,
            env=None if env is None else {**os.environ, **env},
        )

    def _git_retry_lock(
        self,
        working_dir: str,
        *args: str,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one git command, retrying (bounded) while it fails on a stale
        or contended ``index.lock`` (M2b). Non-lock failures return
        immediately; a lock that outlives the budget returns the last proc
        (the caller decides whether that is fatal)."""
        proc = self._git(working_dir, *args, check=False, env=env)
        for _ in range(self._LOCK_RETRY_ATTEMPTS - 1):
            if proc.returncode == 0 or "index.lock" not in (proc.stderr + proc.stdout):
                return proc
            time.sleep(self._LOCK_RETRY_SLEEP)
            proc = self._git(working_dir, *args, check=False, env=env)
        return proc

    def is_checkout(self, working_dir: str) -> bool:
        try:
            proc = self._git(working_dir, "rev-parse", "--is-inside-work-tree", check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def pre_attempt_dirty_paths(self, working_dir: str) -> list[str]:
        """Return paths dirty before a worker attempt starts."""
        return self.changed_paths(working_dir)

    def snapshot(
        self,
        working_dir: str,
        message: str,
        coordinator_paths: Sequence[str],
        expected_digests: Mapping[str, str] | None = None,
        expected_modes: Mapping[str, str] | None = None,
    ) -> str:
        """Commit this run's coordinator-file delta as the next branch base.

        ``--only`` is load-bearing: path-limited ``git add`` alone would still
        let an unrelated path that the operator had already staged leak into
        this commit. ``--allow-empty`` preserves the branch-base commit when no
        run-written coordinator file changed (or PLAN.md could not be
        materialized).

        When ``expected_digests`` is supplied, each path is staged only if the
        working-tree bytes still hash to the approved digest, and the committed
        blob is those verified bytes via the index (not a later re-read of the
        pathname). ``expected_modes`` (when supplied) binds the git file mode the
        same way — live working-tree mode bits are never consulted for
        digest-bound paths, so an operator-only ``chmod`` cannot enter history.
        ``git commit --only -- path`` is avoided on this path because it
        re-reads the working tree and would re-introduce the TOCTOU.
        """
        sha = self._commit_paths(
            working_dir,
            coordinator_paths,
            message,
            allow_empty=True,
            retry_lock=False,
            expected_digests=expected_digests,
            expected_modes=expected_modes,
        )
        assert sha is not None  # allow_empty always creates the branch-base commit.
        return sha

    def changed_paths(self, working_dir: str) -> list[str]:
        """Uncommitted working-tree delta vs HEAD plus untracked files.

        HEAD is the reference (not the attempt snapshot): confirmed sibling
        work is committed pathspec-limited between snapshots, so the
        uncommitted delta is exactly the still-unreviewed work in the shared
        directory.
        """
        tracked = self._git(working_dir, "diff", "--name-only", "HEAD").stdout.splitlines()
        untracked = self._git(
            working_dir, "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        return sorted({p.strip() for p in (*tracked, *untracked) if p.strip()})

    def revert_paths(self, working_dir: str, snapshot_sha: str, paths: Sequence[str]) -> None:
        for path in paths:
            exists_at_snapshot = (
                self._git(
                    working_dir, "cat-file", "-e", f"{snapshot_sha}:{path}", check=False
                ).returncode
                == 0
            )
            if exists_at_snapshot:
                self._git(working_dir, "checkout", snapshot_sha, "--", path)
            else:
                # New out-of-scope file: remove it from the working tree.
                target = os.path.join(working_dir, path)
                try:
                    if os.path.isfile(target) or os.path.islink(target):
                        os.unlink(target)
                except OSError:
                    LOG.warning("could not remove out-of-scope file %s", target)
                self._git(
                    working_dir, "rm", "--cached", "--ignore-unmatch", "-q", "--", path, check=False
                )

    def commit_paths(self, working_dir: str, paths: Sequence[str], message: str) -> str | None:
        return self._commit_paths(
            working_dir,
            paths,
            message,
            allow_empty=False,
            retry_lock=True,
        )

    def _hash_object(self, working_dir: str, content: bytes) -> str:
        """Write ``content`` as a blob and return its SHA (bytes-preserving)."""
        proc = subprocess.run(  # noqa: S603 -- fixed argv, never shell
            (
                "git",
                "-C",
                working_dir,
                *self._IDENTITY,
                *self._NO_HOOKS,
                "hash-object",
                "-w",
                "--stdin",
            ),
            input=content,
            capture_output=True,
            timeout=120,
            check=True,
        )
        return proc.stdout.decode().strip()

    @staticmethod
    def _file_mode(working_dir: str, path: str) -> str:
        target = os.path.join(working_dir, path)
        try:
            return (
                "100755" if os.access(target, os.X_OK) and not os.path.isdir(target) else "100644"
            )
        except OSError:
            return "100644"

    def _stage_exact_content(
        self,
        working_dir: str,
        path: str,
        content: bytes,
        *,
        retry_lock: bool,
        env: Mapping[str, str] | None = None,
        mode: str | None = None,
    ) -> str | None:
        """Stage the given bytes for ``path``; return blob SHA or None on soft fail.

        Stages via ``update-index --cacheinfo`` so the blob is the exact
        ``content`` bytes — never a later re-read of the pathname. When
        ``mode`` is supplied it is the sole mode source (approved provenance);
        otherwise the live path mode is used (unbound / non-digest path).
        """
        blob = self._hash_object(working_dir, content)
        resolved_mode = mode if mode is not None else self._file_mode(working_dir, path)
        args = ("update-index", "--add", "--cacheinfo", f"{resolved_mode},{blob},{path}")
        proc = (
            self._git_retry_lock(working_dir, *args, env=env)
            if retry_lock
            else self._git(working_dir, *args, check=False, env=env)
        )
        if proc.returncode != 0:
            if not retry_lock:
                raise subprocess.CalledProcessError(
                    proc.returncode,
                    list(proc.args),
                    output=proc.stdout,
                    stderr=proc.stderr,
                )
            return None
        return blob

    def _commit_paths(
        self,
        working_dir: str,
        paths: Sequence[str],
        message: str,
        *,
        allow_empty: bool,
        retry_lock: bool,
        expected_digests: Mapping[str, str] | None = None,
        expected_modes: Mapping[str, str] | None = None,
    ) -> str | None:
        """Stage and commit exactly ``paths``, ignoring ambient index entries.

        When ``expected_digests`` maps a path to a SHA-256 hex digest, that path
        is included only if its current bytes match, and the staged blob is the
        verified content committed from an isolated index. ``expected_modes``
        binds the git mode the same way (never live working-tree mode bits).
        ``git commit --only -- path`` is intentionally not used on that path:
        it re-reads the working tree and would unbind the approved digest from
        the committed blob.
        """
        digests = dict(expected_digests) if expected_digests else {}
        if digests:
            return self._commit_paths_digest_bound(
                working_dir,
                paths,
                message,
                allow_empty=allow_empty,
                retry_lock=retry_lock,
                digests=digests,
                modes=dict(expected_modes) if expected_modes else {},
            )

        staged_any = False
        known_paths: list[str] = []
        for path in paths:
            tracked_before = self._git(
                working_dir,
                "ls-files",
                "--error-unmatch",
                "--",
                path,
                check=False,
            )
            if tracked_before.returncode != 0 and not os.path.lexists(
                os.path.join(working_dir, path)
            ):
                continue
            proc = (
                self._git_retry_lock(working_dir, "add", "-A", "--", path)
                if retry_lock
                else self._git(working_dir, "add", "-A", "--", path, check=False)
            )
            if proc.returncode == 0:
                staged_any = True
                tracked = self._git(
                    working_dir,
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    path,
                    check=False,
                )
                existed_at_head = self._git(
                    working_dir,
                    "cat-file",
                    "-e",
                    f"HEAD:{path}",
                    check=False,
                )
                if tracked.returncode == 0 or existed_at_head.returncode == 0:
                    known_paths.append(path)
            elif not retry_lock:
                raise subprocess.CalledProcessError(
                    proc.returncode,
                    list(proc.args),
                    output=proc.stdout,
                    stderr=proc.stderr,
                )
        if not staged_any and not allow_empty:
            return None
        if not allow_empty and (
            not known_paths
            or self._git(
                working_dir,
                "diff",
                "--cached",
                "--quiet",
                "HEAD",
                "--",
                *known_paths,
                check=False,
            ).returncode
            == 0
        ):
            return None  # nothing actually staged
        commit_args = ["commit", "--only"]
        if allow_empty:
            commit_args.append("--allow-empty")
        commit_args.extend(("-m", message))
        if known_paths:
            commit_args.extend(("--", *known_paths))
        commit = (
            self._git_retry_lock(working_dir, *commit_args)
            if retry_lock
            else self._git(working_dir, *commit_args, check=False)
        )
        if commit.returncode != 0:
            raise subprocess.CalledProcessError(
                commit.returncode,
                list(commit.args),
                output=commit.stdout,
                stderr=commit.stderr,
            )
        return self._git(working_dir, "rev-parse", "HEAD").stdout.strip()

    def _head_tree_mode(self, working_dir: str, path: str) -> str | None:
        """Return the mode recorded for ``path`` in HEAD, or None if untracked."""
        proc = self._git(working_dir, "ls-tree", "HEAD", "--", path, check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        # mode SP type SP object TAB path
        meta = proc.stdout.splitlines()[0].split("\t", 1)[0].split()
        if not meta:
            return None
        return meta[0]

    def _head_content_digest(self, working_dir: str, path: str) -> str | None:
        """SHA-256 of the blob bytes for ``path`` at HEAD, or None if absent."""
        rev = self._git(working_dir, "rev-parse", f"HEAD:{path}", check=False)
        if rev.returncode != 0 or not rev.stdout.strip():
            return None
        # Binary-safe: ``_git`` decodes text; cat-file via raw subprocess.
        raw = subprocess.run(  # noqa: S603 -- fixed argv, never shell
            (
                "git",
                "-C",
                working_dir,
                *self._IDENTITY,
                *self._NO_HOOKS,
                "cat-file",
                "blob",
                rev.stdout.strip(),
            ),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if raw.returncode != 0:
            return None
        return hashlib.sha256(raw.stdout).hexdigest()

    def _approved_stage_mode(
        self,
        working_dir: str,
        path: str,
        modes: Mapping[str, str],
        *,
        content_digest: str | None = None,
    ) -> str:
        """Resolve mode for digest-bound staging without reading live path bits.

        Never consults the working-tree executable bit (operator ``chmod``).

        When ``content_digest`` matches HEAD's content for ``path``, pin HEAD's
        mode so a mode-only ambient change cannot enter history even if an
        approved-mode map was poisoned by a live-mode capture (seed path).
        Otherwise: explicit approved mode → HEAD tree mode → regular file.
        """
        head_mode = self._head_tree_mode(working_dir, path)
        if content_digest is not None and head_mode is not None:
            head_digest = self._head_content_digest(working_dir, path)
            if head_digest is not None and head_digest == content_digest:
                return head_mode
        approved = modes.get(path)
        if approved in ("100644", "100755", "120000", "160000"):
            return approved
        if head_mode is not None:
            return head_mode
        return "100644"

    def _commit_paths_digest_bound(
        self,
        working_dir: str,
        paths: Sequence[str],
        message: str,
        *,
        allow_empty: bool,
        retry_lock: bool,
        digests: Mapping[str, str],
        modes: Mapping[str, str] | None = None,
    ) -> str | None:
        """Commit paths with approved digests bound to staged blobs.

        Uses an isolated temporary index (``GIT_INDEX_FILE``) so ambient staged
        paths cannot leak into the commit and are not unstaged from the real
        index. Stages only bytes that still match the approved digest via
        ``update-index --cacheinfo``, then commits from that temp index.
        Mismatched paths are omitted — never replaced by later working-tree
        bytes. Mode is taken from approved provenance (or HEAD), never live
        working-tree bits. ``git commit --only -- path`` is avoided: it
        re-reads the working tree and would unbind digests.
        """
        approved_modes = dict(modes) if modes else {}
        # Capture digest-verified bytes up front. Staging uses only these
        # bytes (via hash-object + cacheinfo), never a later pathname re-read.
        verified: list[tuple[str, bytes, str]] = []  # path, content, mode
        unbound_paths: list[str] = []
        for path in paths:
            expected = digests.get(path)
            if expected is None:
                unbound_paths.append(path)
                continue
            target = os.path.join(working_dir, path)
            try:
                content = Path(target).read_bytes()
            except OSError:
                continue
            if hashlib.sha256(content).hexdigest() != expected:
                # Operator (or other) rewrite after eligibility — skip.
                continue
            mode = self._approved_stage_mode(
                working_dir,
                path,
                approved_modes,
                content_digest=expected,
            )
            verified.append((path, content, mode))

        if not verified and not unbound_paths:
            if not allow_empty:
                return None
            # Empty branch-base: ``--only`` without pathspecs leaves ambient
            # index entries alone (same contract as the non-digest path).
            commit_args = ["commit", "--only", "--allow-empty", "-m", message]
            commit = (
                self._git_retry_lock(working_dir, *commit_args)
                if retry_lock
                else self._git(working_dir, *commit_args, check=False)
            )
            if commit.returncode != 0:
                raise subprocess.CalledProcessError(
                    commit.returncode,
                    list(commit.args),
                    output=commit.stdout,
                    stderr=commit.stderr,
                )
            return self._git(working_dir, "rev-parse", "HEAD").stdout.strip()

        with tempfile.TemporaryDirectory(prefix="swarm-snap-") as tmp:
            index_path = os.path.join(tmp, "index")
            index_env: dict[str, str] = {"GIT_INDEX_FILE": index_path}
            read_tree = (
                self._git_retry_lock(working_dir, "read-tree", "HEAD", env=index_env)
                if retry_lock
                else self._git(working_dir, "read-tree", "HEAD", check=False, env=index_env)
            )
            if read_tree.returncode != 0:
                if not retry_lock:
                    raise subprocess.CalledProcessError(
                        read_tree.returncode,
                        list(read_tree.args),
                        output=read_tree.stdout,
                        stderr=read_tree.stderr,
                    )
                return None

            staged_blobs: list[tuple[str, str, str]] = []  # path, mode, blob
            for path, content, mode in verified:
                blob = self._stage_exact_content(
                    working_dir,
                    path,
                    content,
                    retry_lock=retry_lock,
                    env=index_env,
                    mode=mode,
                )
                if blob is None:
                    continue
                staged_blobs.append((path, mode, blob))

            for path in unbound_paths:
                tracked_before = self._git(
                    working_dir,
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    path,
                    check=False,
                    env=index_env,
                )
                if tracked_before.returncode != 0 and not os.path.lexists(
                    os.path.join(working_dir, path)
                ):
                    continue
                proc = (
                    self._git_retry_lock(working_dir, "add", "-A", "--", path, env=index_env)
                    if retry_lock
                    else self._git(working_dir, "add", "-A", "--", path, check=False, env=index_env)
                )
                if proc.returncode != 0:
                    if not retry_lock:
                        raise subprocess.CalledProcessError(
                            proc.returncode,
                            list(proc.args),
                            output=proc.stdout,
                            stderr=proc.stderr,
                        )
                    continue
                ls = self._git(
                    working_dir,
                    "ls-files",
                    "-s",
                    "--",
                    path,
                    check=False,
                    env=index_env,
                )
                if ls.returncode != 0 or not ls.stdout.strip():
                    continue
                # mode SP sha SP stage TAB path
                meta = ls.stdout.splitlines()[0].split("\t", 1)[0].split()
                if len(meta) >= 2:
                    staged_blobs.append((path, meta[0], meta[1]))

            if not staged_blobs:
                if not allow_empty:
                    return None
                commit_args = ["commit", "--only", "--allow-empty", "-m", message]
                commit = (
                    self._git_retry_lock(working_dir, *commit_args)
                    if retry_lock
                    else self._git(working_dir, *commit_args, check=False)
                )
                if commit.returncode != 0:
                    raise subprocess.CalledProcessError(
                        commit.returncode,
                        list(commit.args),
                        output=commit.stdout,
                        stderr=commit.stderr,
                    )
                return self._git(working_dir, "rev-parse", "HEAD").stdout.strip()

            if not allow_empty:
                known = [p for p, _m, _b in staged_blobs]
                unchanged = self._git(
                    working_dir,
                    "diff",
                    "--cached",
                    "--quiet",
                    "HEAD",
                    "--",
                    *known,
                    check=False,
                    env=index_env,
                )
                if unchanged.returncode == 0:
                    return None

            commit_args = ["commit"]
            if allow_empty:
                commit_args.append("--allow-empty")
            commit_args.extend(("-m", message))
            commit = (
                self._git_retry_lock(working_dir, *commit_args, env=index_env)
                if retry_lock
                else self._git(working_dir, *commit_args, check=False, env=index_env)
            )
            if commit.returncode != 0:
                raise subprocess.CalledProcessError(
                    commit.returncode,
                    list(commit.args),
                    output=commit.stdout,
                    stderr=commit.stderr,
                )

            # Sync only committed paths into the real index; ambient staged
            # entries stay put (``git commit --only`` semantics).
            for path, mode, blob in staged_blobs:
                sync_args = ("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
                sync = (
                    self._git_retry_lock(working_dir, *sync_args)
                    if retry_lock
                    else self._git(working_dir, *sync_args, check=False)
                )
                if sync.returncode != 0 and not retry_lock:
                    raise subprocess.CalledProcessError(
                        sync.returncode,
                        list(sync.args),
                        output=sync.stdout,
                        stderr=sync.stderr,
                    )

            return self._git(working_dir, "rev-parse", "HEAD").stdout.strip()


class DurableLimits:
    """Default ``LimitsPort``: durable cooldowns + fleet budget via limit_state."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path

    def report_rate_limited(
        self, provider: str, account_id: str | None, detail: str, reset_at: str | None
    ) -> None:
        if not account_id:
            return
        from omniagentos.routing.limit_state import (
            OUTCOME_QUOTA_EXHAUSTED,
            OUTCOME_TRANSIENT_RATE_LIMIT,
            report_outcome,
        )

        outcome = OUTCOME_QUOTA_EXHAUSTED if reset_at else OUTCOME_TRANSIENT_RATE_LIMIT
        try:
            report_outcome(
                provider,
                account_id,
                outcome,
                detail,
                reset_at=reset_at,
                db_path=self._db_path,
            )
        except Exception:  # noqa: BLE001 -- limit reporting is best-effort here.
            LOG.warning("could not report rate limit for %s/%s", provider, account_id)

    def earliest_cooldown_until(self) -> datetime | None:
        from omniagentos.db.store import _connect
        from omniagentos.routing.limit_state import _parse_iso

        conn = _connect(self._db_path or default_db_path())
        try:
            rows = conn.execute(
                "SELECT cooldown_until FROM claude_accounts "
                "WHERE enabled = 1 AND cooldown_until IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        now = datetime.now(UTC)
        candidates = [
            parsed
            for row in rows
            if (parsed := _parse_iso(row["cooldown_until"])) is not None and parsed > now
        ]
        return min(candidates) if candidates else None

    def swarm_slots_remaining(self) -> int:
        from omniagentos.routing.limit_state import fleet_available

        try:
            fleet = fleet_available(db_path=self._db_path)
        except Exception:  # noqa: BLE001 -- a broken ledger must not stop a run.
            LOG.warning("fleet_available failed; assuming one slot", exc_info=True)
            return 1
        # Signed version of available_for_swarm (which clamps at 0): both the
        # global headroom and the swarm-ceiling headroom, whichever binds.
        ceiling = fleet.max_sessions_global - fleet.reserved_small_task_slots
        return min(
            fleet.max_sessions_global - fleet.total_live,
            ceiling - fleet.swarm_live,
        )


def _detect_mechanical_suite(working_dir: str) -> list[str]:
    """Best-effort local suite commands when formation_mechanical_gate is true.

    Prefer planner ``verify_command``; when that is empty, run a small, repo-local
    suite so coding/ops formations do not vacuous-pass. Every detected command
    stays inside the same strict verifier grammar used for planner commands.
    """
    root = Path(working_dir)
    cmds: list[str] = []
    # Python project: targeted pytest when tests/ exists (fast, no network).
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        if (root / "tests").is_dir() or (root / "test").is_dir():
            cmds.append("python -m pytest -q --tb=no -x tests")
    return cmds[:2]


def assert_touched_modules_importable(working_dir: str, touched: Sequence[str]) -> tuple[bool, str]:
    """P0.3 — every touched Python module must be reachable by its dotted path
    AND resolve to the file that was edited.

    A flat ``pkg/mod.py`` shadowed by a ``pkg/mod/`` package imports the PACKAGE,
    so an edit to the flat file has zero production effect while its own tests —
    if they load it by path — still pass. That exact shape shipped today and was
    caught only by an independent reviewer, never by the suite.
    """
    root = Path(working_dir).resolve()
    problems: list[str] = []
    for rel in touched:
        if not rel.endswith(".py") or rel.endswith("__init__.py"):
            continue
        parts = Path(rel).with_suffix("").parts
        if not parts or any(not part.isidentifier() for part in parts):
            continue  # not an importable dotted path (scripts/, tests fixtures)
        dotted = ".".join(parts)
        try:
            spec = importlib.util.find_spec(dotted)
        except (ImportError, ValueError, ModuleNotFoundError):
            continue  # not importable in this context; not our signal
        if spec is None or not spec.origin:
            continue
        try:
            resolved = Path(spec.origin).resolve()
        except OSError:
            continue
        # Safety (`is not True`): unknown identity is treated as import shadowing.
        if inode_paths_equal(resolved, (root / rel).resolve()) is not True:
            problems.append(
                f"{rel} is shadowed: '{dotted}' resolves to {resolved} — edits to "
                "this file have no effect on the imported module"
            )
    if problems:
        return False, "; ".join(problems[:5])
    return True, ""


def default_verifier(
    task: Mapping[str, Any], swarm_json: Mapping[str, Any], working_dir: str
) -> tuple[bool, str]:
    """Run planner ``verify_command`` and, when mechanical gate is on, a suite.

    Formation mechanical gate (F3 / MODEL-ROLE-POLICY, DEFECT #5):
    - ``false`` → skip auto-detected suite ONLY, require and run explicit
      verify_command
    - ``true`` / unset → run verify_command if present; if empty, detect and run
      a local mechanical suite (pytest / typecheck) so coding/ops never
      vacuous-pass solely because the planner omitted a command.
    """
    del task
    gate = swarm_json.get("formation_mechanical_gate")
    gate_is_false = gate is False or str(gate).lower() in {"0", "false", "no"}

    # P0.3: a module shadowed by a same-named package imports the PACKAGE, so an
    # edit to the shadowed file has zero production effect while its own tests
    # can still pass. Check this BEFORE running any suite — a green suite over
    # unreachable code is the failure mode, not the signal.
    touched_paths = swarm_json.get("touched_paths") or swarm_json.get("owned_paths") or []
    if isinstance(touched_paths, (list, tuple)) and touched_paths:
        shadow_ok, shadow_detail = assert_touched_modules_importable(
            working_dir, [str(p) for p in touched_paths]
        )
        if not shadow_ok:
            return False, f"import-shadow check failed: {shadow_detail}"

    logs: list[str] = []
    command = str(swarm_json.get("verify_command") or "").strip()
    raw_suite = swarm_json.get("mechanical_suite_commands") or []
    if not isinstance(raw_suite, (list, tuple)):
        return False, "mechanical_suite_commands must be a list of verifier commands"
    suite = list(raw_suite)
    suite = [str(c).strip() for c in suite if str(c).strip()]

    # Auto-detect suite only when gate is NOT false and no explicit command
    if not command and not suite:
        if not gate_is_false:
            suite = _detect_mechanical_suite(working_dir)

    commands: list[str] = []
    if command:
        commands.append(command)
    # Only add auto-detected suite if gate is not false
    if not gate_is_false:
        commands.extend(suite)
    elif command:
        # gate is false but explicit verify_command was given; run it anyway
        pass

    if not commands:
        if gate_is_false:
            return (
                False,
                "mechanical gate disabled and no verify_command — refusing vacuous pass.",
            )
        else:
            # Fail closed when gate is on: empty workspace must not vacuous-pass
            # (Opus c4ddea9 P1). Planner should set verify_command or drop gate.
            return (
                False,
                "mechanical gate on but no verify_command/suite detected — refusing vacuous pass",
            )

    from omniagentos.gates.engine import GateSpec, run_gates

    for cmd in commands:
        try:
            argv = parse_verifier_command(cmd)
            validate_verifier_targets(argv, working_dir)
        except VerifierCommandError as exc:
            return False, f"unsafe verifier command refused: {exc}"
        spec = GateSpec(argv=argv, timeout_s=600)
        results = run_gates([spec], working_dir)
        if not results:
            continue
        res = results[0]
        if res.infra_error is not None:
            return False, res.infra_error

        logs.append(f"$ {res.command}\n{res.output[-2000:]}")
        if not res.ok:
            return False, "\n---\n".join(logs)[-4000:]
    return True, "\n---\n".join(logs)[-4000:]


def default_terminal_classifier(session: Mapping[str, Any]) -> str:
    """Map a terminal session row to an attempt outcome.

    WP5b plugs ``longhaul.limits.classify_terminal`` in here (the existing
    output-pattern rate-limit classifier, extended per provider). Until then:
    an explicit ``swarm_outcome`` on the row wins (fakes/tests), a completed
    session is ``completed``, killed/cancelled are ``killed``, everything else
    is ``crashed``.
    """
    explicit = str(session.get("swarm_outcome") or "").strip()
    if explicit:
        return explicit
    state = str(session.get("state") or "")
    if state == "completed":
        return "completed"
    if state in ("killed", "cancelled"):
        return "killed"
    return "crashed"


def default_task_splitter(
    task: Mapping[str, Any], swarm_json: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    """Bounded planner call: split a twice-timed-out task into ≤4 subtasks that
    PARTITION the parent's owned_paths. Any failure returns None (caller
    blocks the task instead)."""
    try:
        from omniagentos.swarm.planner import _intake_planning

        fable, _, _ = _intake_planning()
        schema = {
            "type": "object",
            "required": ["subtasks"],
            "properties": {
                "subtasks": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "required": ["title", "description"],
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "owned_paths": {"type": "array", "items": {"type": "string"}},
                            "est_agent_minutes": {"type": "integer"},
                            "complexity": {"type": "string"},
                            "verify_command": {
                                "type": "string",
                                "description": (
                                    "Strict non-shell pytest/ruff/mypy/pyright/git verifier "
                                    "with repository-relative targets"
                                ),
                            },
                            "acceptance": {"type": "string"},
                        },
                    },
                }
            },
        }
        prompt = "\n".join(
            [
                "A swarm task timed out twice even after a tier escalation. Split it",
                "into 2-4 smaller INDEPENDENT subtasks that together deliver the",
                "original task. Subtask owned_paths must PARTITION the parent's",
                f"owned_paths: {json.dumps(list(swarm_json.get('owned_paths') or []))}",
                "",
                "## Parent task",
                str(task.get("title") or ""),
                str(task.get("description") or ""),
                "",
                f"Acceptance: {swarm_json.get('acceptance') or '(none)'}",
                "Each verify_command must use only pytest/python -m pytest, ruff check,",
                "mypy, or pyright with repo-relative targets, or exact git diff --check.",
                'Return JSON: {"subtasks": [{title, description, owned_paths, ...}]}',
            ]
        )
        raw = fable.run_fable_json(prompt, schema, effort="medium", max_turns=3, wall_ms=180_000)
        if not isinstance(raw, dict):
            return None
        subtasks = raw.get("subtasks")
        if not isinstance(subtasks, list) or not 1 <= len(subtasks) <= 4:
            return None
        return [dict(item) for item in subtasks if isinstance(item, Mapping)] or None
    except Exception:  # noqa: BLE001 -- a failed split degrades to blocked, never crashes the run.
        LOG.warning("default task splitter failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Worker brief (read-only plan slice at spawn)
# ---------------------------------------------------------------------------


def subtasks_request_protocol_lines(request_path: str, *, insession: bool = False) -> list[str]:
    """The worker-initiated fan-out protocol section (PKG-REQUEST-SUBTASKS),
    keyed to the EXACT attempt-bound ``request_path``. SINGLE source of truth:
    ``build_worker_brief`` splices these into the first-attempt brief, and the
    spawner re-appends them to relay/continuation prompts (with the successor
    attempt's path) so the instruction survives a rate-limit/timeout handoff.

    ``insession`` (PKG-INSESSION-FANOUT) swaps in the grant-wait variant: the
    SAME request file, but the worker is told it may receive a coordinator
    grant to run the children as live subagents inside this session. False →
    the section is byte-identical to before the feature existed (pinned)."""
    if insession:
        from omniagentos.swarm.insession import (
            grant_path_for_request,
            grant_wait_seconds,
            insession_max_children,
        )

        grant_path = grant_path_for_request(request_path)
        wait = grant_wait_seconds()
        return [
            "",
            "## If this task genuinely decomposes (worker-initiated fan-out)",
            "If — and ONLY if — this task actually breaks into 2-4 INDEPENDENT",
            "subtasks (DISJOINT file ownership, NO ordering between them) that this",
            "attempt cannot finish, you may REQUEST a split instead of forcing the",
            "work. The coordinator stays the ONLY parallelism authority — never",
            "use the Task tool before a grant exists. To request a split:",
            f"1. Write a JSON file at EXACTLY {request_path}",
            '   with this schema: {"reason": "<why it must split>", "subtasks":',
            '   [{"title": str, "description": str, "owned_paths": [str, ...],',
            '   "est_minutes": int (optional)}, ... 2 to 4 entries]}. Every',
            "   subtask's owned_paths MUST be workspace-relative and fall INSIDE",
            "   this task's owned paths above (a partition of them).",
            f"2. Then poll up to {wait}s for a grant file at EXACTLY",
            f"   {grant_path}",
            "   (e.g. a shell loop testing -f with short sleeps).",
            "3. GRANT APPEARS → in-session fan-out is authorized: spawn ONE",
            "   subagent per child listed in the GRANT FILE via the Task tool,",
            "   giving each child ONLY its own title/description/owned_paths and",
            "   the rule to create/edit files ONLY inside those paths. The grant",
            f"   caps Task calls at {insession_max_children()}; an ungranted or over-budget Task",
            "   call is DENIED by policy — if that happens, do that child's work",
            "   yourself. When all children finish: integrate, run your verify,",
            "   and COMPLETE THIS TASK normally in THIS attempt (do NOT end",
            "   early — your in-session work replaces the split).",
            f"4. NO grant within {wait}s → END this attempt with a short summary",
            "   of what remains; the coordinator will register the children as",
            "   separate tasks or deny, exactly as without this section.",
            "Write the request ONLY at that exact path — a file under any other name",
            "is ignored and swept. If the task does NOT truly decompose, ignore this",
            "and just do the work.",
        ]
    return [
        "",
        "## If this task genuinely decomposes (worker-initiated fan-out)",
        "If — and ONLY if — this task actually breaks into 2-4 INDEPENDENT",
        "subtasks (DISJOINT file ownership, NO ordering between them) that this",
        "attempt cannot finish, you may REQUEST a split instead of forcing the",
        "work. Do NOT attempt the subtasks yourself and do NOT expect to see the",
        "children — you cannot spawn anything; the coordinator validates your",
        "request and registers them as first-class tasks. To request a split:",
        f"1. Write a JSON file at EXACTLY {request_path}",
        '   with this schema: {"reason": "<why it must split>", "subtasks":',
        '   [{"title": str, "description": str, "owned_paths": [str, ...],',
        '   "est_minutes": int (optional)}, ... 2 to 4 entries]}. Every',
        "   subtask's owned_paths MUST be workspace-relative and fall INSIDE",
        "   this task's owned paths above (a partition of them).",
        "2. Then END this attempt with a short summary of what remains.",
        "Write the request ONLY at that exact path — a file under any other name",
        "is ignored and swept. If the task does NOT truly decompose, ignore this",
        "and just do the work.",
    ]


def _house_document_root() -> Path:
    """The checkout whose AGENTS.md / ARCHI.md / DECISIONS.md a brief names.

    A named seam, not an inline ``Path(__file__)`` expression, because tests
    that need a checkout WITHOUT one of those documents otherwise have to
    monkeypatch ``pathlib.Path.exists`` process-wide — which every thread in a
    concurrent scheduler run then executes, including sqlite and the store.
    Redirecting this one function is the local, thread-safe way to ask "what
    would the brief say against a different tree?".

    Underscore-prefixed because that is all it is: a seam for THIS module's
    brief builder and the tests that redirect it, not a capability anything
    outside the file is meant to call.
    """
    return Path(__file__).resolve().parent.parent.parent


def build_worker_brief(
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    swarm_json: Mapping[str, Any],
    neighbor_statuses: Mapping[str, str],
    subtasks_request_path: str | None = None,
    *,
    insession: bool = False,
    project_store: Any | None = None,
) -> str:
    """The worker's read-only plan slice: its task + contracts + neighbor
    statuses, with the plan version + hash embedded (a worker distrusts a
    PLAN.md whose hash mismatches its brief).

    Phase-2 worktree mode (``swarm_json.worktree_branch`` set by the
    coordinator) swaps the opening + hard rules for the private-worktree
    variant; without it the Phase-1 shared-directory brief is BYTE-IDENTICAL
    to before worktree mode existed (pinned by test).

    The INTEGRATION task of a worktree-mode run
    (``swarm_json.worktree_integration``) gets its OWN variant: it runs in
    the MAIN workspace and must be able to manually merge the coordinator-
    routed conflict branches, so the Phase-1 "NEVER run git" hard rule would
    contradict the conflict feedback ("merge branch X manually"). Its rules
    explicitly permit ``git merge``/``add``/``commit`` in the main workspace
    STRICTLY for the routed conflict branches (listed by name) and forbid
    every other git mutation."""
    owned = list(swarm_json.get("owned_paths") or [])
    feedback = list(swarm_json.get("feedback") or [])
    worktree_branch = str(swarm_json.get("worktree_branch") or "")
    integration_worktree = bool(
        swarm_json.get("integration") and swarm_json.get("worktree_integration")
    )
    if integration_worktree:
        routed_merges = [
            (str(entry.get("branch")), str(entry.get("sha") or ""))
            for entry in feedback
            if isinstance(entry, Mapping)
            and str(entry.get("source") or "") == "merge_conflict"
            and entry.get("branch")
        ]
        opening = [
            "You are the INTEGRATION task of an OmniAgentOS swarm run in WORKTREE",
            "mode. You run in the MAIN workspace; the coordinator already merged",
            "every completed task branch EXCEPT the conflicted ones routed to you.",
        ]
        hard_rules = [
            "## Hard rules",
            "- You MAY run `git merge`, `git add`, and `git commit` IN THIS MAIN",
            "  WORKSPACE — ONLY to merge the coordinator-routed conflict branches",
            "  listed below and to commit their conflict resolutions.",
            "- Conflict merges routed to you (merge each, resolve, commit).",
            "  Merge the EXACT sha listed — never the branch name (R4: a sibling",
            "  could have moved the ref after review):",
            *(
                [
                    (f"  - merge {sha} (branch {branch})" if sha else f"  - merge {branch}")
                    for branch, sha in routed_merges
                ]
                or ["  - (none routed — in that case run NO git mutation at all)"]
            ),
            "- ALL other git mutation is forbidden: NEVER push, pull, rebase,",
            "  reset, switch branches, delete branches, run any `git worktree`",
            "  command, or merge any branch not listed above.",
            "- Never edit PLAN.md.",
            "- Out-of-scope changes are reverted automatically and flag your review.",
        ]
    elif worktree_branch:
        opening = [
            "You are ONE WORKER in an OmniAgentOS swarm run. You work in a PRIVATE",
            "git worktree; other agents work concurrently in their own worktrees.",
            "Deliver ONLY your task, ONLY inside your owned paths.",
        ]
        hard_rules = [
            "## Hard rules",
            f"- You are on private git branch {worktree_branch} in a dedicated",
            "  worktree — commit your own work freely with `git add` / `git commit`.",
            "- NEVER push, pull, merge, rebase, switch branches, or run any",
            "  `git worktree` command — the coordinator merges your branch.",
            *WORKTREE_GITDIR_RULE_LINES,
            "- Never create or modify files outside this working directory.",
            "- Never edit PLAN.md.",
            "- Out-of-scope changes are reverted automatically and flag your review.",
        ]
    else:
        opening = [
            "You are ONE WORKER in an OmniAgentOS swarm run. Other agents are working",
            "in the SAME directory concurrently. Deliver ONLY your task, ONLY inside",
            "your owned paths.",
        ]
        hard_rules = [
            "## Hard rules",
            "- NEVER run `git add`, `git commit`, or any other git mutation — all git",
            "  operations are coordinator-owned in this shared directory.",
            "- Never edit PLAN.md.",
            "- Out-of-scope changes are reverted automatically and flag your review.",
        ]
    # Applies to every execution model: low-effort/simple-tier workers (and
    # non-claude CLIs) otherwise answer in chat and "complete" with zero file
    # changes — drill-proven failure mode; the mechanical verify then denies.
    hard_rules.append("- Deliver by CREATING or EDITING FILES with your tools inside your owned")
    hard_rules.append("  paths. A chat-only answer with no file changes is a FAILED attempt.")
    # PKG-REQUEST-SUBTASKS: worker-initiated fan-out door. Present ONLY when the
    # coordinator threaded a request path (feature on); absent → the brief is
    # byte-identical to before the feature existed (pinned by test).
    subtasks_section = (
        subtasks_request_protocol_lines(subtasks_request_path, insession=insession)
        if subtasks_request_path
        else []
    )
    try:
        from omniagentos.health.digest import build_health_digest, read_health_snapshot

        health_digest = build_health_digest(read_health_snapshot())
    except Exception:  # noqa: BLE001 -- optional health context must not block a worker.
        LOG.exception("health digest omitted from worker brief")
        health_digest = ""
    lines = [
        *opening,
        "",
        f"Plan version {swarm_json.get('plan_version', 1)}, "
        f"hash {str(swarm_json.get('plan_hash') or '')[:12]} — PLAN.md in the",
        "workspace is a derived projection; trust it only if its hash matches.",
        "",
        "## Task",
        str(task.get("title") or ""),
        "",
        str(task.get("description") or ""),
        "",
        f"Acceptance: {swarm_json.get('acceptance') or '(none recorded)'}",
        f"Verify: {swarm_json.get('verify_command') or '(none)'}",
        "",
        "## Owned paths (the ONLY files you may create or modify)",
        *([f"- {p}" for p in owned] or ["- (none — produce analysis/output only)"]),
        "",
        *([health_digest, ""] if health_digest else []),
        *hard_rules,
        *subtasks_section,
    ]
    if neighbor_statuses:
        lines += ["", "## Neighbor task statuses"]
        lines += [f"- {key}: {status}" for key, status in sorted(neighbor_statuses.items())]
    if feedback:
        lines += ["", "## Prior attempt feedback (address this)"]
        # M5: merge-conflict entries are EXEMPT from the last-3 truncation —
        # the integration task must see EVERY routed conflict, or a busy run
        # (4+ conflicts, or conflicts followed by retries) silently drops
        # merges it was told to perform.
        recent_start = max(0, len(feedback) - 3)
        selected = [
            entry
            for index, entry in enumerate(feedback)
            if index >= recent_start
            or (isinstance(entry, Mapping) and str(entry.get("source") or "") == "merge_conflict")
        ]
        lines += [f"- {str(entry.get('text') or entry)[:500]}" for entry in selected]

    # LANE B: Your documents and worker_context_block append
    run_id = str(run.get("id") or swarm_json.get("run_id") or "")
    task_id = str(task.get("id") or swarm_json.get("task_id") or "")

    repo_root = _house_document_root()
    agents_path = (repo_root / "AGENTS.md").resolve()
    testing_path = (repo_root / "TESTING.md").resolve()
    # Resolve ARCHI.md (full architecture) with fallback to ARCHITECTURE.md (stub) if absent
    archi_md_path = (repo_root / "ARCHI.md").resolve()
    archi_json_path = (repo_root / "ARCHI.json").resolve()
    architecture_path = (
        archi_md_path if archi_md_path.exists() else (repo_root / "ARCHITECTURE.md").resolve()
    )
    decisions_path = (repo_root / "DECISIONS.md").resolve()

    if run_id and task_id:
        from omniagentos.swarm.spawn import swarm_workbook_path

        workbook_path = swarm_workbook_path(run_id, task_id).resolve()
        task_md_path = (workbook_path.parent / "TASK.md").resolve()
    else:
        workbook_path = repo_root / "var" / "swarm" / "run_id" / "task_id" / "WORKBOOK.md"
        task_md_path = repo_root / "var" / "swarm" / "run_id" / "task_id" / "TASK.md"

    # board_tasks has no project_id column. Resolve from the denormalized run
    # value, then swarm metadata, then (when gated) the registry roots below.
    project_value = run.get("project_id") or swarm_json.get("project_id")
    project_id = str(project_value).strip() if project_value else None
    try:
        from omniagentos.brandpacks.pack import project_contract_mode

        contract_mode = project_contract_mode()
    except Exception:  # noqa: BLE001 -- a broken optional gate remains disabled.
        contract_mode = "off"

    contract = None
    if contract_mode != "off":
        contract_store = project_store
        owns_contract_store = False
        try:
            from omniagentos.brandpacks.pack import resolve_project_contract
            from omniagentos.db.store import SqliteStore

            if contract_store is None:
                contract_store = SqliteStore(default_db_path())
                owns_contract_store = True
            contract = resolve_project_contract(
                contract_store,
                project_id=project_id,
                # A generated worker worktree is not necessarily beneath the
                # project's registered root. Registry fallback must use the
                # original run workspace; execution_dir is only a last resort
                # for direct/unit callers whose run mapping has no workspace.
                working_dir=run.get("working_dir") or swarm_json.get("execution_dir"),
                repo_root=repo_root,
            )
            if contract is not None:
                resolved_id = str(contract.project.get("id") or "").strip()
                if contract_mode == "enforce":
                    project_id = resolved_id or project_id
                elif contract_mode == "shadow":
                    LOG.info(
                        "project contract shadow project_id current=%s "
                        "would_resolve=%s applied=false",
                        project_id,
                        resolved_id or project_id,
                    )
        except Exception:  # noqa: BLE001 -- project context is additive and non-blocking.
            LOG.debug("could not resolve project contract", exc_info=True)
        finally:
            if owns_contract_store and contract_store is not None:
                contract_store.close()

    memory_path = (
        (repo_root / "var" / "memories" / project_id / "MEMORY.md").resolve()
        if project_id
        else None
    )

    lines.append("")
    lines.append("## Your documents")
    lines.append(f"- Task Contract: {task_md_path}")
    lines.append(f"- Task Workbook (Progress Log): {workbook_path}")
    lines.append(f"- House Rules & Orchestration: {agents_path}")
    lines.append(f"- Testing Guide: {testing_path}")
    # Surface the resolved architecture path and the machine-readable sibling if ARCHI.md exists
    arch_doc_line = f"- Architecture Index: {architecture_path}"
    if archi_md_path.exists():
        arch_doc_line += f" (machine-readable: {archi_json_path})"
    lines.append(arch_doc_line)
    lines.append(f"- Decision Ledger (ADRs): {decisions_path}")
    if contract_mode == "enforce":
        lines.append("- Memory Ritual: Bounded project memory content is injected below.")
    elif memory_path is not None:
        lines.append(
            f"- Memory Ritual: Before starting work, read the lessons learned in {memory_path}. After completing, append yours."
        )
    else:
        lines.append(
            "- Memory Ritual: No project is registered for this task, so there is no shared memory file yet."
        )

    if contract is not None and contract_mode == "enforce":
        try:
            from omniagentos.brandpacks.pack import render_project_contract
            from omniagentos.swarm.prompt_safety import fence_data_block

            lines.extend(
                (
                    "",
                    fence_data_block(
                        "PROJECT_CONTRACT",
                        render_project_contract(
                            contract,
                            objective=str(task.get("description") or task.get("title") or ""),
                            audience=str(task.get("audience") or swarm_json.get("audience") or ""),
                            output_format=str(
                                task.get("output_format")
                                or task.get("format")
                                or swarm_json.get("output_format")
                                or swarm_json.get("format")
                                or ""
                            ),
                            deliverable_spec=str(
                                task.get("deliverable_spec")
                                or swarm_json.get("deliverable_spec")
                                or task.get("acceptance")
                                or swarm_json.get("acceptance")
                                or ""
                            ),
                        ),
                    ),
                )
            )
        except Exception:  # noqa: BLE001 -- rendering context must not block a worker.
            LOG.debug("could not render project contract", exc_info=True)

    # Plan-07: the worker's own brief is the ambient capability query. Resolve the
    # company from the same project axis used by the run, then retrieve only that
    # namespace plus estate. The wrapper is fail-open and knowledge defaults off, so
    # an unavailable PG never delays or blocks spawn.
    try:
        from omniagentos.knowledge.capabilities import (
            infer_domains,
            resolve_company_id,
            safe_ambient_capability_block,
        )
        from omniagentos.knowledge.config import knowledge_enabled

        if knowledge_enabled():
            scope_store = project_store
            owns_scope_store = False
            if scope_store is None:
                from omniagentos.db.store import SqliteStore

                scope_store = SqliteStore(default_db_path())
                owns_scope_store = True
            try:
                company_id = resolve_company_id(scope_store, project_id)
            finally:
                if owns_scope_store:
                    scope_store.close()
            summary = "\n".join(
                str(value or "")
                for value in (
                    task.get("title"),
                    task.get("description"),
                    swarm_json.get("acceptance"),
                )
            )
            raw_domains = swarm_json.get("domains")
            domains = (
                [str(value) for value in raw_domains]
                if isinstance(raw_domains, list)
                else infer_domains(summary, owned)
            )
            capability_block = safe_ambient_capability_block(
                summary,
                company_id=company_id,
                paths=owned,
                domains=domains,
            )
            if capability_block:
                lines.extend(("", "## Ambient capabilities", capability_block))
    except Exception:  # noqa: BLE001 -- optional recall never blocks worker launch.
        LOG.debug("ambient capability recall omitted from worker brief", exc_info=True)

    try:
        from omniagentos.swarm.metacog_context import worker_context_block

        ctx_block = worker_context_block(
            task_title=str(task.get("title") or ""),
            task_description=str(task.get("description") or ""),
            project_id=project_id,
        )
        if ctx_block and ctx_block.strip():
            from omniagentos.swarm.prompt_safety import fence_data_block, truncate_utf8

            lines.append("")
            lines.append(
                fence_data_block(
                    "MEMORY_SKILL_ARTIFACT_CONTEXT",
                    truncate_utf8(ctx_block.strip(), WORKER_CONTEXT_BYTE_CAP),
                )
            )
    except Exception:
        pass

    # U-R8: Inline standing capability grants for the detected lane identity
    try:
        from omniagentos.grants.reader import format_grant_lines

        # U-R8 lane identity, adjudicated at Phase-2 integration.
        #
        # PLAN.md §1 invariant 1 (and reliability/store.py's canonical-identity
        # docstring) name `lane:swarm.worker.<formation>` canonical. The lane
        # shipped the bare `lane:swarm.worker` on the stated grounds that
        # `swarm_json` carries no formation. It does: planner.py's
        # `_formation_stamp` writes `formation_id` into every bound run's
        # swarm_json, and the six ids are exactly the vocabulary invariant 1
        # lists. So the qualified spelling IS producible here and is produced.
        #
        # BOTH spellings are read, general first. The bare id stays the common
        # floor -- grants issued today name it, and an unbound plan has no
        # formation at all -- while the qualified id is what a formation-scoped
        # grant lands on. Reading only the qualified one would query a string no
        # grant is issued against (the lane's real concern); reading only the
        # bare one leaves the plan and the code disagreeing. Reading both costs
        # one indexed lookup and leaves neither.
        lane_holders = ["lane:swarm.worker"]
        formation_id = str(swarm_json.get("formation_id") or "").strip()
        # Canonical subject grammar: starts alphanumeric, then [A-Za-z0-9._-].
        # A formation id that cannot spell a canonical holder is dropped rather
        # than sanitized -- a mangled id would query a holder nobody grants to.
        if _CANONICAL_LANE_SUBJECT_RE.fullmatch(formation_id):
            lane_holders.append(f"lane:swarm.worker.{formation_id}")
        grant_lines = format_grant_lines(lane_holders, db_path=None)
        if grant_lines:
            lines.extend(grant_lines)
    except Exception:  # noqa: BLE001 -- grant inlining is non-blocking
        # If grant reading fails, the brief still works without this section
        LOG.debug("could not inline standing grants", exc_info=True)

    return "\n".join(lines)


@dataclass
class _LivenessTracker:
    """One awaiting worker's view of its attempt's liveness over time.

    Thread-confined by construction: created inside ``_await_and_settle`` and
    never shared, so the cadence and the change-only dedupe need no locking.

    ``next_check`` is advanced by whole intervals rather than reset to
    ``now + interval`` so the cadence does not drift with the await poll
    period; missed intervals (a long provider read, a paused clock) are
    COLLAPSED rather than replayed, so waking late costs one tick, not a burst.
    """

    next_check: float
    status: str | None = None
    ticks: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def due(self, now: float) -> bool:
        return now >= self.next_check

    def schedule_next(self, now: float, interval: float) -> None:
        nxt = self.next_check + interval
        if nxt <= now:
            nxt = now + interval
        self.next_check = nxt

    def record(self, status: str) -> bool:
        """Count the tick; return True when the status CHANGED."""
        self.ticks += 1
        self.counts[status] = self.counts.get(status, 0) + 1
        changed = status != self.status
        self.status = status
        return changed


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class _RunState:
    run_id: str
    working_dir: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    cond: threading.Condition = field(init=False)
    git_lock: threading.RLock = field(default_factory=threading.RLock)
    signals: queue.Queue[tuple[str, Any]] = field(default_factory=queue.Queue)
    target_n: int = 1
    workers: dict[int, threading.Thread] = field(default_factory=dict)
    active_claims: dict[str, int] = field(default_factory=dict)  # task_id -> slot
    running_attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    parked: dict[str, dict[str, Any]] = field(default_factory=dict)
    attaching: set[str] = field(default_factory=set)
    resume_items: deque = field(default_factory=deque)
    park_until: datetime | None = None  # rate-limit park (all-cooling)
    # Two distinct facts: the run went OVER its budget (always recorded), and the
    # budget STOPPED it (only in blocking mode -- see omniagentos.budget.policy).
    budget_overshot: bool = False
    budget_exhausted: bool = False
    # Dollar enforcement was impossible because at least one linked session
    # reported tokens but no price. BLOCK fails closed; ADVISORY records loudly
    # and continues.
    budget_unenforceable: bool = False
    unknown_cost_sessions: int = 0
    merge_started: bool = False
    # Phase-2 worktree isolation: resolved ONCE per coordinator launch
    # (_resolve_worktree_mode) — flag + `git worktree list` probe; False keeps
    # the Phase-1 same-directory model byte-identical.
    worktree_mode: bool = False
    # m6: the run row's RECORDED worktree registration — GC/cleanup paths
    # gate on this (not the live mode resolution), so a recorded worktree
    # run whose probe failed on resume still gets its worktrees cleaned.
    worktree_recorded: bool = False
    git_common_dir: str | None = None
    # M3: <common>/refs/heads/swarm/<run_id> — the ONE refs subtree a worker
    # sandbox may write (its own run's branch namespace); granted as an extra
    # write root so the profile can re-open it AFTER the refs/heads deny.
    git_ref_namespace: str | None = None
    # M2c: worktrees deliberately LEFT IN PLACE because salvaging their
    # confirmed work failed — terminal cleanup must not force-remove them.
    kept_worktrees: set[str] = field(default_factory=set)
    # Coordinator files are operator-owned until this coordinator writes a
    # content delta. Capture their pre-run state before the coordinator thread
    # starts, then record the exact state produced by each successful write.
    # State is (exists, content_sha256|None, git_mode). A snapshot commits only
    # a still content-matching pending state, staged with the approved mode, so
    # ambient PLAN.md content or mode dirt is never mistaken for this run's
    # output.
    coordinator_file_state: dict[str, tuple[bool, str | None, str]] = field(default_factory=dict)
    coordinator_file_pending: dict[str, tuple[bool, str | None, str]] = field(default_factory=dict)
    plan_dirty: bool = False
    stopping: bool = False
    cancelled: bool = False
    # H-19: external terminalization (cancel / fail / complete stamped by an
    # outside agent) is applied exactly once per coordinator process. Guards
    # the kill → finalize-attempts → wait sequence so a reconcile burst cannot
    # double-kill or skip finalization. ``terminal_reason`` is the stamped
    # status (cancelled/failed/completed/…) so await/settle can honor ALL
    # external terminals, not only cancel.
    terminalizing: bool = False
    terminal_reason: str | None = None
    # m10 lease fencing: the lease_generation this coordinator holds; its
    # heartbeat is conditional on it. ``displaced`` flips when the beat fails
    # (another process adopted the run) — the loop then aborts CLEANLY: no
    # claim releases, no attempt closes, no git mutation after displacement.
    lease_generation: int = 0
    displaced: bool = False
    # T3.x scope locks (default OFF — omniagentos.scope.config.scope_locks_mode).
    # ``scope_realm`` is the MAIN workspace's realm key, resolved ONCE per
    # coordinator launch (realm_of can shell out to git) and left None whenever
    # locking is off or the path has no realm — None is the signal every scope
    # call site checks, so with the feature off none of them touch the database.
    # ``scope_held`` is the set of task ids whose work locks this coordinator is
    # responsible for renewing; ``scope_backoff`` is task_id -> monotonic
    # deadline, the "somebody else owns these paths right now" skip list that
    # stops a refused task from being re-claimed in a hot loop.
    scope_realm: str | None = None
    scope_held: set[str] = field(default_factory=set)
    scope_backoff: dict[str, float] = field(default_factory=dict)
    # Any task that returned without opening useful work is skipped until one
    # complete worker poll pass has elapsed. A pass advances only when the
    # condition wait times out, not when notify_all wakes it early; this keeps
    # the gate deterministic under injected clocks and spurious notifications.
    worker_poll_pass: int = 0
    requeue_after_pass: dict[str, int] = field(default_factory=dict)
    finished: bool = False
    last_progress: float = 0.0
    last_heartbeat: float = 0.0
    thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.cond = threading.Condition(self.lock)


class SwarmRunHandle:
    """Join handle for one coordinated run."""

    def __init__(self, run_id: str, state: _RunState) -> None:
        self.run_id = run_id
        self._state = state

    def join(self, timeout: float | None = None) -> bool:
        thread = self._state.thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def is_alive(self) -> bool:
        thread = self._state.thread
        return thread is not None and thread.is_alive()


class _NullEmitter:
    def emit(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> None:
        del run_id, action, payload


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class SwarmScheduler:
    """One coordinator thread + N resizable slot-worker threads per run.

    Threads and locks (per run): the coordinator thread owns run-level
    decisions (heartbeat, reconcile, resize, completion, cancel fanout,
    PLAN.md); worker threads own one attempt lifecycle each. ``_RunState.cond``
    (RLock-backed) guards the slot counter and all registries;
    ``_RunState.git_lock`` serializes every git mutation (shared-index race);
    ``_RunState.signals`` is the worker→coordinator completion queue, with the
    coordinator's timed reconcile pass as the lost-message fallback. The DALs
    (SwarmDal, CollabStore, SessionsDal) each serialize internally.
    """

    def __init__(
        self,
        *,
        dal: SwarmDal,
        collab: CollabStore,
        emitter: SwarmEmitter | None = None,
        spawner: SwarmSpawnerProto,
        session_store: SessionStoreProto | None = None,
        router: SwarmRouterProto | None = None,
        reviewer: SwarmReviewerProto | None = None,
        splitter: TaskSplitter | None = None,
        verifier: TaskVerifier | None = None,
        git: SwarmGitProto | None = None,
        worktrees: SwarmWorktreesProto | None = None,
        worktrees_enabled: bool | None = None,
        worker_subtask_requests: bool | None = None,
        limits: LimitsPortProto | None = None,
        clock: SchedulerClock | None = None,
        classifier: TerminalClassifier | None = None,
        heartbeat_seconds: float = 30.0,
        fallback_poll_seconds: float = 30.0,
        worker_poll_seconds: float = 0.5,
        await_poll_seconds: float = 1.0,
        liveness_poll_seconds: float = LIVENESS_POLL_SECONDS,
        stall_minutes: float = DEFAULT_STALL_MINUTES,
        adopt_stale_minutes: float = DEFAULT_ADOPT_STALE_MINUTES,
        cooldown_wait_cap_seconds: float = DEFAULT_COOLDOWN_WAIT_CAP_SECONDS,
        rate_limit_requeue_cap: int = DEFAULT_RATE_LIMIT_REQUEUE_CAP,
        retry_cap: int = DEFAULT_RETRY_CAP,
        timeout_minutes: Mapping[str, float] | None = None,
        db_path: str | None = None,
        summary_writer: Callable[[str], Any] | None = None,
        scope_commit_wait_s: float = DEFAULT_COMMIT_WAIT_S,
        scope_retry_seconds: float = DEFAULT_SCOPE_RETRY_SECONDS,
    ) -> None:
        self._dal = dal
        self._collab = collab
        self._emitter: SwarmEmitter = emitter or _NullEmitter()
        self._spawner = spawner
        self._session_store = session_store
        self._router: SwarmRouterProto = router or DefaultSwarmRouter()
        self._reviewer: SwarmReviewerProto = reviewer or CrossLineageSwarmReviewer()
        self._splitter: TaskSplitter = splitter or default_task_splitter
        self._verifier: TaskVerifier = verifier or default_verifier
        self._git: SwarmGitProto = git or SubprocessSwarmGit()
        # Worktree seam resolves lazily (imports swarm.worktrees only when a
        # run actually enters worktree mode); ``worktrees_enabled=None`` means
        # "resolve from configs/swarm.yaml + OMNIAGENTOS_SWARM_WORKTREES".
        self._worktrees_seam: SwarmWorktreesProto | None = worktrees
        self._worktrees_enabled = worktrees_enabled
        # PKG-REQUEST-SUBTASKS: worker-initiated fan-out. ``None`` resolves from
        # configs/swarm.yaml ``worker_subtask_requests`` (ABSENT defaults ON);
        # an explicit bool (tests) wins. Feature OFF makes the detection at
        # attempt completion a no-op — the subtasks_request.json file is ignored.
        self._worker_subtask_requests_override = worker_subtask_requests
        self._limits: LimitsPortProto = limits or DurableLimits(db_path)
        self._clock = clock or SchedulerClock()
        self._classifier = classifier or default_terminal_classifier
        self._heartbeat_seconds = heartbeat_seconds
        self._fallback_poll_seconds = fallback_poll_seconds
        self._worker_poll_seconds = worker_poll_seconds
        self._await_poll_seconds = await_poll_seconds
        # Cadence of the pre-deadline liveness tick. Clamped at >= 0: a
        # negative cadence would make every await poll a DB read.
        self._liveness_poll_seconds = max(0.0, float(liveness_poll_seconds))
        self._stall_minutes = stall_minutes
        self._adopt_stale_minutes = adopt_stale_minutes
        self._cooldown_wait_cap_seconds = cooldown_wait_cap_seconds
        self._rate_limit_requeue_cap = rate_limit_requeue_cap
        self._retry_cap = retry_cap
        self._timeouts = dict(timeout_minutes or DEFAULT_TIMEOUT_MINUTES)

        # Validate NESTED-TIMEOUT MONOTONICITY: the invariant that matters is that
        # within each tier, the three layers are strictly ordered so the innermost
        # layer (provider session idle timeout) always fails first with the most
        # specific error. Equal tier budgets are allowed; the nesting invariant
        # is per-tier, not between tiers.
        #
        # The nesting layers: gate_timeout (600s) < idle_timeout (tier_budget * 0.75) < wall_deadline (tier_budget)

        # Validate idle timeout fraction: must be between 0 and 1
        # This ensures idle_timeout < wall_deadline for each tier:
        # idle = tier_budget * IDLE_TIMEOUT_FRACTION, so 0 < fraction < 1 means idle < tier_budget
        assert 0 < IDLE_TIMEOUT_FRACTION < 1.0, (
            f"IDLE_TIMEOUT_FRACTION must be between 0 and 1, got {IDLE_TIMEOUT_FRACTION}"
        )

        self._db_path = db_path
        self._summary_writer = summary_writer
        # Scope locks (ship dark): how long a git op waits out ANOTHER process's
        # commit lock, and how long a task whose declared scope was refused is
        # skipped before it is offered to a worker again.
        self._scope_commit_wait_s = float(scope_commit_wait_s)
        self._scope_retry_seconds = float(scope_retry_seconds)
        self._runs: dict[str, _RunState] = {}
        self._runs_lock = threading.Lock()

    # -- session store (lazy default) ----------------------------------------

    def _sessions(self) -> SessionStoreProto:
        if self._session_store is None:
            from omniagentos.sessions.dal import SessionsDal

            self._session_store = SessionsDal(self._db_path or default_db_path())
        return self._session_store

    # -- worktrees (Phase-2 seam; lazy default) -------------------------------

    def _worktrees(self) -> SwarmWorktreesProto:
        if self._worktrees_seam is None:
            from omniagentos.swarm.spawn import default_swarm_var_root
            from omniagentos.swarm.worktrees import (
                SubprocessSwarmWorktrees,
                default_coral_shared_root,
            )

            resolved_var_root = default_swarm_var_root()
            resolved_coral_root = default_coral_shared_root(resolved_var_root)
            self._worktrees_seam = SubprocessSwarmWorktrees(
                var_root=resolved_var_root,
                coral_shared_root=resolved_coral_root,
            )
        return self._worktrees_seam

    # -- scope locks (T3.x; SHIPS DARK — scope_locks_mode() defaults off) -----

    def _locks(self) -> Any:
        """The cross-lane :class:`PathLockStore`, bound to the dal's connection.

        Never called on the disabled path: every scope call site checks
        ``state.scope_realm is not None`` first, and that stays None while the
        mode is off.
        """
        return self._dal.path_locks

    def _resolve_scope_realm(self, state: _RunState) -> None:
        """Resolve the MAIN workspace's realm ONCE per coordinator launch.

        Left None when the mode is off (the whole mechanism is then inert) or
        when the workspace has no resolvable realm. Resolving once matters:
        ``realm_of`` can shell out to ``git rev-parse``, and the alternative is
        one subprocess per git call.

        Deliberately NOT re-read later. The mode is a per-launch decision, the
        same discipline ``_resolve_worktree_mode`` uses, so an operator flipping
        the flag mid-run never leaves a coordinator half-locked.
        """
        if not scope_locks_active():
            state.scope_realm = None
            return
        state.scope_realm = realm_for(state.working_dir)

    def _check_run_lease_checkpoint(self, state: _RunState) -> bool:
        """Poll/verify that the run lease is still held by this coordinator before destructive mutation."""
        if state.displaced:
            return False
        if not state.run_id:
            return True
        try:
            if hasattr(self, "_dal") and self._dal is not None:
                ok = self._dal.heartbeat(state.run_id, generation=state.lease_generation)
                if not ok:
                    state.displaced = True
                    return False
            return True
        except Exception:  # noqa: BLE001
            if effective_collision_mode() == "enforce":
                state.displaced = True
                return False
            return True

    @contextmanager
    def _git_guard(self, state: _RunState, target_dir: str, label: str) -> Iterator[CommitFence]:
        """state.git_lock — plus the cross-process commit lock for MAIN.

        _RunState.git_lock is a threading.RLock and therefore in-process
        only: two coordinators in two processes (the API and the sessions daemon,
        or two dev shells) can interleave a git merge on the same repo today.
        This adds the durable half for mutations of the MAIN workspace, and keeps
        the RLock — it is far cheaper for the common intra-process case, and
        dropping it would send every worker thread in the process to SQLite.

        Mutations of a task's own WORKTREE pass their worktree as target_dir
        and get the in-process lock alone: a worktree is a private realm, so
        there is no cross-process claimant to exclude, and taking a lock there
        would only write rows nobody can conflict with.

        With scope locking off this is byte-for-byte with state.git_lock:.
        """
        if state.displaced or not self._check_run_lease_checkpoint(state):
            raise ScopeCommitBusy(f"run {state.run_id}: lease lost before git mutation ({label})")

        # Safety (`is not False`): unknown MAIN identity still takes its cross-process lock.
        realm = (
            state.scope_realm
            if inode_paths_equal(target_dir, state.working_dir) is not False
            else None
        )
        with commit_guard(
            self._locks() if realm is not None else None,
            realm=realm,
            holder=coordinator_holder(state.run_id) if realm is not None else None,
            generation=state.lease_generation,
            in_process_lock=state.git_lock,
            clock=self._clock,  # type: ignore[arg-type]  # structural: monotonic + sleep
            wait_s=self._scope_commit_wait_s,
            label=f"{state.run_id}:{label}",
        ) as fence:
            if state.displaced or not self._check_run_lease_checkpoint(state):
                raise ScopeCommitBusy(
                    f"run {state.run_id}: lease lost after acquire before git mutation ({label})"
                )
            yield fence

    def _claim_task_scope(
        self, state: _RunState, task_id: str, exec_dir: str, owned_paths: Sequence[str]
    ) -> bool:
        """Take a task's declared scope before its attempt opens. False == refused.

        On refusal the task is parked (a durable FIFO waiter row naming the
        blocker) and put in a short skip window so the worker that just released
        the claim does not immediately re-pick it. Nothing else happens: no
        attempt row, no spawn, no retry consumed — the task is exactly where it
        was, waiting for paths somebody else is holding.

        A refusal deliberately does NOT count as progress for the stall guard.
        A run whose every task sits blocked on another lane for
        ``stall_minutes`` HAS stalled, and it should say so rather than wait
        silently forever.
        """
        result, realm = claim_task_scope(
            self._locks() if state.scope_realm is not None else None,
            exec_dir=exec_dir,
            owned_paths=owned_paths,
            task_id=task_id,
            run_id=state.run_id,
            generation=state.lease_generation,
        )
        if result.granted:
            if result.lock_ids:
                with state.cond:
                    state.scope_held.add(task_id)
                    state.scope_backoff.pop(task_id, None)
                clear_task_waiter(self._locks(), task_id)
            return True
        if result.status == "fenced":
            # Adopted away: this coordinator must stop, not retry (a retry at a
            # bumped generation would be it stealing its adopter's locks back).
            LOG.warning(
                "run %s: scope acquire FENCED for task %s — this coordinator was adopted away",
                state.run_id,
                task_id,
            )
            with state.cond:
                state.displaced = True
                state.stopping = True
                state.cond.notify_all()
            return False
        LOG.info(
            "run %s: task %s scope refused (%s) — parking",
            state.run_id,
            task_id,
            result.conflict.describe() if result.conflict is not None else "scope unavailable",
        )
        park_task_waiter(self._locks(), realm, task_id, result)
        with state.cond:
            state.scope_backoff[task_id] = self._clock.monotonic() + self._scope_retry_seconds
        return False

    def _reclaim_task_scope(
        self, state: _RunState, task_id: str, exec_dir: str, owned_paths: Sequence[str]
    ) -> None:
        """Re-take scope for an attempt this coordinator ADOPTED (re-attach).

        Never refuses. The session is already running and already writing; there
        is nothing a refusal could undo, and pretending otherwise would only
        deny the adopter the lease it needs to keep renewing. ``replace=True``
        so a task whose declaration narrowed on resume actually gives the
        surrendered paths back.
        """
        result, _realm = claim_task_scope(
            self._locks() if state.scope_realm is not None else None,
            exec_dir=exec_dir,
            owned_paths=owned_paths,
            task_id=task_id,
            run_id=state.run_id,
            generation=state.lease_generation,
            replace=True,
        )
        if result.lock_ids:
            with state.cond:
                state.scope_held.add(task_id)
        elif not result.granted:
            LOG.info(
                "run %s: could not re-take scope for adopted task %s (%s) — the "
                "attempt continues; its files are unprotected until it settles",
                state.run_id,
                task_id,
                result.conflict.describe() if result.conflict is not None else result.status,
            )

    def _release_task_scope(self, state: _RunState, task_id: str) -> None:
        """Drop one task's work locks. Generation-fenced, so a displaced
        coordinator calling this frees nothing."""
        with state.cond:
            held = task_id in state.scope_held
            state.scope_held.discard(task_id)
        if not held:
            # NOTHING WAS HELD, SO THIS IS THE REFUSAL PATH -- and the anti-spin
            # backoff window MUST survive it.
            #
            # _claim_task_scope arms state.scope_backoff[task_id] and returns
            # False; _execute_task then returns "requeue"; _execute_item's finally
            # calls this method. Popping the backoff before this guard deleted the
            # window microseconds after it was armed, on 100% of refusals -- the
            # task was re-picked immediately, producing a hot loop measured by
            # review at roughly 600 git commits/second against the main workspace.
            # The pop belongs only on the path where a claim was genuinely held.
            return
        with state.cond:
            state.scope_backoff.pop(task_id, None)
        release_holder_scope(self._locks(), TASK_HOLDER_KIND, task_id, state.lease_generation)
        clear_task_waiter(self._locks(), task_id)

    def _renew_scope_best_effort(self, state: _RunState) -> None:
        """Extend every lock this coordinator is responsible for, once per beat.

        Rides the coordinator heartbeat (30s by default) against a 90s lease, so
        two consecutive missed beats are needed before a live worker's paths go
        back on the market. A renewal that returns False is NOT escalated here:
        the heartbeat CAS immediately above is the authoritative displacement
        signal, and a lock that lapsed under a live worker is re-taken by the
        worker's own next acquire.
        """
        with state.cond:
            held = sorted(state.scope_held)
        if not held:
            return
        for task_id in held:
            renew_holder_scope(self._locks(), TASK_HOLDER_KIND, task_id, state.lease_generation)

    def _release_run_scope_best_effort(self, state: _RunState) -> None:
        """Terminal cleanup: give every path this run still holds back.

        Gated exactly like the worktree GC — only when the run's DB status is
        terminal, and never after displacement. A coordinator exiting for a
        non-terminal reason (process shutdown, adoption handoff) leaves the locks
        to their TTL, because its successor may be about to re-attach the very
        attempts that hold them.
        """
        if state.displaced:
            return
        with state.cond:
            held = sorted(state.scope_held)
        if not held:
            return
        try:
            run = self._dal.get_run(state.run_id)
            status = str((run or {}).get("status") or "")
            if status not in {str(s) for s in TERMINAL_RUN_STATUSES}:
                return
        except Exception:  # noqa: BLE001 -- cleanup is best-effort; the TTL is the backstop.
            LOG.warning("scope: terminal status read failed for %s", state.run_id, exc_info=True)
            return
        for task_id in held:
            self._release_task_scope(state, task_id)

    # -- worker-initiated fan-out flag (PKG-REQUEST-SUBTASKS) -----------------

    def _subtask_requests_enabled(self) -> bool:
        """Whether the coordinator reads a worker's subtasks_request.json.

        An explicit constructor override wins (tests); otherwise the
        ``worker_subtask_requests`` top-level key of configs/swarm.yaml is read
        best-effort — ABSENT (or an unreadable config) defaults ON, an explicit
        ``false`` disables the detection entirely (the request file is ignored,
        and today's completion flow is byte-identical)."""
        if self._worker_subtask_requests_override is not None:
            return bool(self._worker_subtask_requests_override)
        try:
            from omniagentos.routing.limit_state import load_swarm_config

            value = load_swarm_config().get("worker_subtask_requests", True)
        except Exception:  # noqa: BLE001 -- config resolution must not break a run.
            LOG.warning(
                "worker_subtask_requests flag resolution failed; defaulting ON", exc_info=True
            )
            return True
        return True if value is None else bool(value)

    def _insession_enabled(self) -> bool:
        """PKG-INSESSION-FANOUT master switch (config + env). Fail-CLOSED: a
        broken flag must never open the fan-out door."""
        try:
            from omniagentos.swarm.insession import insession_enabled

            return insession_enabled()
        except Exception:  # noqa: BLE001
            LOG.warning("insession flag resolution failed; treating as OFF", exc_info=True)
            return False

    def _resolve_worktree_mode(self, state: _RunState) -> None:
        """Resolve worktree mode ONCE per coordinator launch (D4 opt-in).

        m6: the RESOLVED mode is persisted on the run row (``params_json``)
        the first time a coordinator resolves it (activation), and a recorded
        value WINS over the ambient env/config/injected flag on every later
        launch — a worktree-mode run resumed in a process without
        ``OMNIAGENTOS_SWARM_WORKTREES`` still runs its missing-worktree
        checks and worktree GC, and a Phase-1 run never flips to worktree
        mode mid-flight. ``state.worktree_recorded`` carries the recorded
        registration for GC/cleanup gating even when the live probe fails.

        Enabling still requires: a workspace that already passed the non-git
        refusal, a clean ``git worktree list`` probe, AND a resolvable git
        common dir (the worker write root). Any miss logs the fallback and
        keeps ``worktree_mode=False`` — the Phase-1 same-directory model runs
        byte-identical, and the non-git refusal is never relaxed."""
        recorded: bool | None = None
        try:
            params = self._dal.get_run_params(state.run_id)
            if "worktree_mode" in params:
                recorded = bool(params["worktree_mode"])
        except Exception:  # noqa: BLE001 -- a params read failure degrades to ambient.
            LOG.warning("could not read run params for %s", state.run_id, exc_info=True)
        enabled: bool | None
        if recorded is not None:
            enabled = recorded
        else:
            enabled = self._worktrees_enabled
            if enabled is None:
                try:
                    from omniagentos.swarm.worktrees import swarm_worktrees_enabled

                    enabled = swarm_worktrees_enabled()
                except Exception:  # noqa: BLE001 -- config resolution must not kill the run.
                    LOG.warning("worktree flag resolution failed; using Phase 1", exc_info=True)
                    enabled = False
        common: str | None = None
        if enabled:
            try:
                worktrees = self._worktrees()
                if worktrees.supported(state.working_dir):
                    common = worktrees.git_common_dir(state.working_dir)
            except Exception:  # noqa: BLE001 -- probe failure degrades, never fails the run.
                LOG.warning("worktree probe raised for run %s", state.run_id, exc_info=True)
        resolved = bool(enabled and common)
        if recorded is None:
            # Activation (or first post-upgrade launch): record the RESOLVED
            # mode so every later resume/adoption replays it.
            try:
                self._dal.merge_run_params(state.run_id, {"worktree_mode": resolved})
            except Exception:  # noqa: BLE001 -- best-effort; ambient fallback still works.
                LOG.warning("could not record worktree_mode for %s", state.run_id, exc_info=True)
        state.worktree_recorded = bool(recorded) if recorded is not None else resolved
        if enabled and not common:
            log = LOG.error if recorded else LOG.warning
            log(
                "worktree mode %s but the workspace probe failed for run %s; "
                "falling back to the Phase-1 shared-directory model",
                "was RECORDED for this run" if recorded else "requested",
                state.run_id,
            )
        if not resolved or common is None:
            return
        state.worktree_mode = True
        state.git_common_dir = common
        # M3: the run's branch-namespace write root — re-opened by the
        # Seatbelt profile AFTER its refs/heads deny so workers can commit
        # their own branch while main/sibling-run refs stay unwritable.
        try:
            from omniagentos.swarm.spawn import _safe_component

            state.git_ref_namespace = os.path.join(
                common, "refs", "heads", "swarm", _safe_component(state.run_id)
            )
        except ValueError:  # pragma: no cover -- run ids are always safe components.
            state.git_ref_namespace = None

    def _clear_wedged_merge_state(self, state: _RunState, resumed: bool) -> None:
        """M4c launch/resume hygiene: a coordinator that died mid-merge can
        leave ``MERGE_HEAD`` (+ a half-staged index) in the MAIN workspace,
        wedging every later merge/commit there. Checked at EVERY coordinator
        start — fresh launch and adoption both pass through ``_coordinate`` —
        and aborted best-effort with a log line + activity event. Gated on
        the RECORDED registration too (R5): a recorded-worktree run whose
        probe failed on this launch falls back to Phase 1, and its very first
        snapshot (``git add -A`` + commit) would otherwise COMPLETE a dead
        coordinator's wedged merge — conflict markers and all."""
        if not (state.worktree_mode or state.worktree_recorded):
            return
        try:
            worktrees = self._worktrees()
            with self._git_guard(state, state.working_dir, "wedged-merge-abort"):
                if not worktrees.has_pending_merge(state.working_dir):
                    return
                aborted = worktrees.abort_merge(state.working_dir)
            LOG.warning(
                "run %s: wedged merge state (MERGE_HEAD) found in %s at coordinator "
                "start — abort %s",
                state.run_id,
                state.working_dir,
                "succeeded" if aborted else "FAILED",
            )
            self._emit(
                state.run_id,
                ACTION_MERGE_ABORTED,
                {
                    "resumed": resumed,
                    "aborted": bool(aborted),
                    "reason": "MERGE_HEAD present at coordinator start",
                },
            )
        except Exception:  # noqa: BLE001 -- hygiene is best-effort, never blocks the run.
            LOG.warning("wedged-merge check failed for %s", state.run_id, exc_info=True)

    # -- summary writer (WP7 terminal-status hook; lazy default) --------------

    def _write_summary_best_effort(self, run_id: str) -> None:
        """Fire the WP7 run-summary hook for a run that just went terminal.

        Called from ``_coordinate``'s single ``finally`` block — the ONE place
        every exit from that method passes through (normal completion, an
        early return, or the crash-recovery except branch) — so a terminal
        run gets exactly one summary write per coordinator launch. Never
        blocks or raises into the coordinator — any failure here is logged
        and swallowed, mirroring ``_emit``'s "observability, never control
        flow" contract.
        """
        writer = self._summary_writer
        if writer is None:
            from omniagentos.swarm.summary import write_summary

            def writer(rid: str) -> None:
                write_summary(rid, dal=self._dal, emitter=self._emitter)

        try:
            writer(run_id)
        except Exception:  # noqa: BLE001 -- WP7 hook is best-effort by contract.
            LOG.warning("swarm summary write failed for run %s", run_id, exc_info=True)
        # C0 completion bell: reconcile_board deliberately skips swarm cards
        # (the scheduler owns their status), so the terminal notification must
        # be emitted HERE — first live run completed silently without it
        # (swr_ce8dda). Completed runs with a board card get the done bell;
        # failed/cancelled runs get a swarm_failed-kind alert (they were
        # previously fully silent — the 2026-07-23 failed runs produced no
        # operator signal; the kind is distinct from longhaul's 'blocked'
        # capacity-wait so the two can never dedupe-suppress each other). Both
        # helpers dedupe kind-aware so a re-fired terminal (crash-resume
        # re-coordination, or the supervisor stale sweep) never double-bells.
        try:
            run = self._dal.get_run(run_id)
            status = str(run.get("status") or "") if run else ""
            if run and status == "completed" and run.get("board_task_id"):
                from omniagentos.notifications.service import notify_task_done

                notify_task_done(
                    board_task_id=str(run["board_task_id"]),
                    task_title=str(run.get("goal") or "Swarm run complete")[:120],
                    workspace=str(run.get("working_dir") or "") or None,
                    run_id=run_id,
                )
            elif run and status in ("failed", "cancelled"):
                from omniagentos.notifications.service import notify_run_terminal_failure

                notify_run_terminal_failure(
                    run_id=run_id,
                    status=status,
                    goal=str(run.get("goal") or "")[:120],
                    board_task_id=(str(run["board_task_id"]) if run.get("board_task_id") else None),
                    workspace=str(run.get("working_dir") or "") or None,
                )
        except Exception:  # noqa: BLE001 -- notification is best-effort.
            LOG.warning("swarm terminal-notification failed for %s", run_id, exc_info=True)

    def _apply_ultra_fable_cap(
        self, state: _RunState, task_id: str, decision: RouteDecision
    ) -> RouteDecision | None:
        """D10 UltraCode: cap live claude sessions per ultra run and prefer
        distinct accounts.

        Returns the (possibly re-reserved) decision, or ``None`` when the run
        already holds :data:`ULTRA_DISTINCT_ACCOUNT_CAP` live claude attempts
        — the task requeues and the slot serves other work meanwhile. All
        failure paths degrade to the router's original decision (never
        block): a cap that cannot be evaluated must not stall the run."""
        from omniagentos.routing.limit_state import (
            ULTRA_DISTINCT_ACCOUNT_CAP,
            reserve_distinct_accounts,
        )

        try:
            live = [
                attempt
                for attempt in self._dal.attempts_for_run(state.run_id)
                if attempt.get("end_reason") is None
                and str(attempt.get("provider") or "") == "claude"
                and str(attempt.get("board_task_id") or "") != task_id
            ]
            if len(live) >= ULTRA_DISTINCT_ACCOUNT_CAP:
                if decision.reservation_id:
                    self._release_router_reservation(decision.reservation_id)
                self._emit(
                    state.run_id,
                    ACTION_TASK_ASSIGNED,
                    {
                        "task_id": task_id,
                        "deferred": "ultra_fable_cap",
                        "live_claude_attempts": len(live),
                    },
                )
                return None
            used_accounts = {
                str(attempt["account_id"]) for attempt in live if attempt.get("account_id")
            }
            if decision.account_id and decision.account_id in used_accounts:
                # Same account as a live sibling: prefer an untouched one.
                distinct = reserve_distinct_accounts(
                    provider="claude", n=1, exclude_account_ids=used_accounts
                )
                if distinct:
                    replacement = distinct[0]
                    if str(replacement.account.account_id) not in used_accounts:
                        if decision.reservation_id:
                            self._release_router_reservation(decision.reservation_id)
                        return replace(
                            decision,
                            account_id=str(replacement.account.account_id),
                            reservation_id=str(replacement.id),
                        )
                    # Degrade: no untouched account had capacity — the
                    # helper reused one; keep the router's pick instead.
                    self._release_router_reservation(str(replacement.id))
        except Exception:  # noqa: BLE001 -- the cap must never stall an ultra run.
            LOG.warning("ultra fable-cap consult failed for %s", task_id, exc_info=True)
        return decision

    def _release_router_reservation(self, reservation_id: str) -> bool:
        """Release through the router's process-lifetime limits DAL when available."""
        release = getattr(self._router, "release_reservation", None)
        if callable(release):
            return bool(release(reservation_id))
        from omniagentos.routing.limit_state import release_reservation

        return release_reservation(reservation_id)

    def _kept_worktree_paths(self, run_id: str) -> set[str]:
        """Durable R2a kept markers for a run: worktree paths whose task
        swarm_json carries ``worktree_kept`` — unsalvageable work that no GC
        path (terminal cleanup, orphan prune, blocked-task removal) may
        remove, in ANY coordinator process."""
        kept: set[str] = set()
        try:
            run = self._dal.get_run(run_id) or {}
            for task in self._member_tasks(run_id, run):
                swarm_json = self._swarm_json_of(task)
                if swarm_json.get("worktree_kept") and swarm_json.get("worktree_path"):
                    kept.add(str(swarm_json["worktree_path"]))
        except Exception:  # noqa: BLE001 -- lookup is best-effort; in-memory set remains.
            LOG.warning("kept-worktree lookup failed for %s", run_id, exc_info=True)
        return kept

    def _cleanup_worktrees_best_effort(self, state: _RunState) -> None:
        """Terminal worktree GC (Phase 2), from ``_coordinate``'s finally.

        Runs ONLY when the run's DB status is terminal — a coordinator exiting
        for a non-terminal reason (process shutdown, adoption handoff) must
        leave worktrees in place for the successor coordinator. Remaining
        worktrees are removed WITH salvage (partial work survives on the
        branch); branches are deleted only on COMPLETED runs — failed and
        cancelled runs keep them for forensics. Gated on the RECORDED
        worktree registration (m6), not the live mode resolution: a recorded
        worktree run whose probe failed on this launch is still cleaned."""
        if state.displaced:
            # m10: no git ops after displacement (the terminal-status gate
            # below would skip anyway — a displaced run is non-terminal —
            # but the invariant is worth its own line of defense).
            return
        if not (state.worktree_mode or state.worktree_recorded):
            return
        # H-19: never remove worktrees while a live writer may still hold them.
        # External terminalization kills + finalizes + waits first; if writers
        # still remain after that, leave the trees for forensics rather than
        # racing a still-running provider process.
        with state.cond:
            live_attempts = len(state.running_attempts)
            live_workers = sum(1 for t in state.workers.values() if t.is_alive())
        if live_attempts or live_workers:
            LOG.error(
                "refusing worktree cleanup for %s: %d live running_attempt(s), "
                "%d live worker thread(s) — cleanup would race a writer",
                state.run_id,
                live_attempts,
                live_workers,
            )
            return
        try:
            run = self._dal.get_run(state.run_id)
            status = str((run or {}).get("status") or "")
            if status not in {str(s) for s in TERMINAL_RUN_STATUSES}:
                return
            with state.cond:
                kept = set(state.kept_worktrees)
            # R2a: the durable kept markers survive coordinator restarts —
            # union them with this process's in-memory set.
            kept |= self._kept_worktree_paths(state.run_id)
            with self._git_guard(state, state.working_dir, "terminal-worktree-gc"):
                worktrees = self._worktrees()
                for path, task_key in worktrees.list_run_worktrees(state.working_dir, state.run_id):
                    if path in kept:
                        # M2c: this worktree holds confirmed work that could
                        # not be salvage-committed — never force-remove it.
                        continue
                    outcome = worktrees.remove(
                        state.working_dir,
                        path,
                        salvage=True,
                        message=(f"swarm {state.run_id}: salvage at terminal cleanup ({task_key})"),
                    )
                    if outcome.status == "salvage_failed":
                        kept.add(path)
                        LOG.error(
                            "terminal cleanup kept worktree %s (%s): salvage failed",
                            path,
                            task_key,
                        )
                if status == "completed" and not kept:
                    worktrees.delete_run_branches(state.working_dir, state.run_id)
        except Exception:  # noqa: BLE001 -- GC is best-effort; resume sweep + prune are the backstop.
            LOG.warning("worktree terminal cleanup failed for %s", state.run_id, exc_info=True)

    def _emit(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> None:
        try:
            self._emitter.emit(run_id, action, payload or {})
        except Exception:  # noqa: BLE001 -- emission is observability, never control flow.
            LOG.debug("swarm emit failed (%s %s)", run_id, action, exc_info=True)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def start_run(self, run_id: str, *, block: bool = False) -> SwarmRunHandle | None:
        """Activate a provisioned run and become its coordinator.

        Returns None when the claim-before-act CAS loses (already running or
        terminal) — the caller must never coordinate without the lease.
        """
        if not self._dal.try_activate_run(run_id):
            LOG.info("start_run(%s): activation CAS lost (already active/terminal)", run_id)
            return None
        return self._launch(run_id, resumed=False, block=block)

    def resume_swarm(self, run_id: str, *, block: bool = False) -> SwarmRunHandle | None:
        """Adopt an orphaned run — ONLY when the coordinator heartbeat is
        > ``adopt_stale_minutes`` stale (heartbeat-lease CAS; a fresh heartbeat
        means a live coordinator and adoption is refused). State is rebuilt
        entirely from the database: dead claims are swept, live attempts are
        re-attached (never re-opened — the partial unique index forbids a
        second live attempt), parked approvals resume parked.
        """
        run = self._dal.get_run(run_id)
        if run is None or str(run["status"]) in {str(s) for s in TERMINAL_RUN_STATUSES}:
            return None
        if not self._dal.adopt_run(run_id, stale_minutes=self._adopt_stale_minutes):
            LOG.info("resume_swarm(%s): heartbeat fresh — live coordinator exists", run_id)
            return None
        return self._launch(run_id, resumed=True, block=block)

    def wake(self, run_id: str) -> None:
        """Nudge a run's coordinator (e.g. after an external approval resolution)."""
        with self._runs_lock:
            state = self._runs.get(run_id)
        if state is not None:
            state.signals.put(("wake", None))

    def shutdown(self) -> None:
        with self._runs_lock:
            states = list(self._runs.values())
        for state in states:
            with state.cond:
                state.stopping = True
                state.cond.notify_all()
            state.signals.put(("wake", None))
        for state in states:
            if state.thread is not None:
                state.thread.join(timeout=10)
        close_router = getattr(self._router, "close", None)
        if callable(close_router):
            close_router()

    def _launch(self, run_id: str, *, resumed: bool, block: bool) -> SwarmRunHandle | None:
        run = self._dal.get_run(run_id)
        if run is None:
            return None
        with self._runs_lock:
            existing = self._runs.get(run_id)
            if existing is not None and existing.thread is not None and existing.thread.is_alive():
                LOG.warning("run %s already coordinated in this process", run_id)
                return None
            state = _RunState(run_id=run_id, working_dir=str(run["working_dir"]))
            state.coordinator_file_state = self._coordinator_file_states(state.working_dir)
            self._seed_coordinator_file_delta(state, run)
            self._runs[run_id] = state
        thread = threading.Thread(
            target=self._coordinate,
            args=(state, resumed),
            name=f"swarm-coord-{run_id}",
            daemon=True,
        )
        state.thread = thread
        thread.start()
        handle = SwarmRunHandle(run_id, state)
        if block:
            handle.join()
        return handle

    # ------------------------------------------------------------------
    # Coordinator
    # ------------------------------------------------------------------

    def _coordinate(self, state: _RunState, resumed: bool) -> None:
        try:
            state.last_progress = self._clock.monotonic()
            run = self._dal.get_run(state.run_id)
            if run is None:
                return
            if not os.path.isdir(state.working_dir) or not self._git.is_checkout(state.working_dir):
                self._fail_run(
                    state,
                    "workspace is not a git checkout — the Phase 1 same-directory "
                    "model requires one (snapshot commits are the recovery path)",
                )
                return
            # m10: capture the lease generation this coordinator holds (the
            # activate/adopt CAS already stamped a fresh heartbeat, so no
            # rival adoption fits between that CAS and this read). Every
            # heartbeat below is conditional on it.
            state.lease_generation = int(run.get("lease_generation") or 0)
            # Resolved before ANY git call below, since _clear_wedged_merge_state
            # is itself a main-workspace mutation.
            self._resolve_scope_realm(state)
            self._resolve_worktree_mode(state)
            self._clear_wedged_merge_state(state, resumed)
            self._emit(state.run_id, ACTION_RUN_STARTED, {"resumed": resumed})
            if resumed:
                self._rebuild_from_db(state)
            with state.cond:
                state.target_n = max(
                    1, min(int(run.get("target_concurrency") or 1), self._run_cap(run))
                )
            self._reconcile(state)
            self._ensure_workers(state)

            while True:
                with state.cond:
                    if state.stopping:
                        break
                try:
                    message = state.signals.get(
                        timeout=min(self._fallback_poll_seconds, self._heartbeat_seconds)
                    )
                except queue.Empty:
                    message = None
                if message is not None:
                    self._handle_signal(state, message)
                    # Drain without blocking so one reconcile covers a burst.
                    while True:
                        try:
                            self._handle_signal(state, state.signals.get_nowait())
                        except queue.Empty:
                            break
                self._reconcile(state)

            self._join_workers(state)
            if not state.displaced:
                # m10: a displaced coordinator writes NOTHING more into the
                # workspace — the adopting coordinator owns PLAN.md now.
                self._write_plan_doc(state)
        except Exception:  # noqa: BLE001 -- the coordinator must fail the run loudly, never vanish.
            LOG.exception("swarm coordinator crashed for run %s", state.run_id)
            try:
                self._fail_run(state, "coordinator crashed; see logs")
            except Exception:  # noqa: BLE001
                LOG.exception("could not record coordinator crash for %s", state.run_id)
        finally:
            # WP7 terminal-status hook: EVERY exit from this method — the
            # normal post-loop path, an early return (workspace not a git
            # checkout, run row vanished), or the crash branch above — lands
            # here exactly once per coordinator launch, and by then the run's
            # DB status is either already terminal or the run never existed
            # (write_summary no-ops on a missing run, so that is safe too).
            # Worktree GC runs FIRST so the summary describes a cleaned run.
            self._cleanup_worktrees_best_effort(state)
            # ...and the scope release AFTER it: the GC's own main-workspace git
            # calls run under the commit guard, which must not find this run's
            # locks already gone while it is still using the workspace.
            self._release_run_scope_best_effort(state)
            self._write_summary_best_effort(state.run_id)
            with self._runs_lock:
                if self._runs.get(state.run_id) is state:
                    del self._runs[state.run_id]

    def _handle_signal(self, state: _RunState, message: tuple[str, Any]) -> None:
        kind, payload = message
        if kind == "all_cooling":
            self._enter_rate_limit_park(state)
        elif kind == "unknown_dependency":
            self._fail_run(state, self._unknown_dependency_diagnosis(payload))
            return
        # "budget" is deliberately just a wake: the reconcile pass re-reads
        # live attempt/session spend, sets the flags AND blocks remaining tasks
        # in one place (setting flags here would make reconcile think the issue
        # was already handled). "wake"/"completed" likewise only trigger the
        # reconcile that follows.
        del payload

    def _enter_rate_limit_park(self, state: _RunState) -> None:
        """Bounded backoff park: wake at the earliest cooldown_until, capped at
         15 min per wait. Re-entered (with a fresh stall event) if providers
        are still cooling at expiry — a wait per park bounds the loop."""
        now = self._clock.now()
        with state.cond:
            if state.park_until is not None and state.park_until > now:
                return  # already parked; do not spam stall events
        until = None
        try:
            until = self._limits.earliest_cooldown_until()
        except Exception:  # noqa: BLE001
            LOG.debug("earliest_cooldown_until failed", exc_info=True)
        remaining = (
            (until - now).total_seconds() if until is not None else self._cooldown_wait_cap_seconds
        )
        wait = max(1.0, min(remaining, self._cooldown_wait_cap_seconds))
        with state.cond:
            state.park_until = now + timedelta(seconds=wait)
            state.last_progress = self._clock.monotonic()  # a park is not a stall
            state.cond.notify_all()
        self._emit(
            state.run_id,
            ACTION_RATE_LIMIT_STALL,
            {
                "seconds": round(wait, 1),
                "until": state.park_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": "all providers cooling",
            },
        )

    def _reconcile(self, state: _RunState) -> None:
        """The DB-poll fallback pass: heartbeat, cancel, budget, claim sweep,
        parked-approval poll, orphan-attempt adoption, resize, completion,
        stall guard. Everything here is derived from the database so a lost
        queue message never wedges the run."""
        now_m = self._clock.monotonic()
        if now_m - state.last_heartbeat >= self._heartbeat_seconds:
            # m10 fencing: the beat is conditional on OUR lease generation.
            # rowcount=0 means another process adopted the run (adopt_run
            # bumped the generation) — this coordinator was displaced and
            # must stop CLEANLY: no destructive git ops, no claim releases,
            # no attempt closes; the adopter owns all of it now.
            if not self._dal.heartbeat(state.run_id, generation=state.lease_generation):
                LOG.warning(
                    "run %s: heartbeat lease lost (displaced by adoption) — "
                    "stopping this coordinator cleanly",
                    state.run_id,
                )
                with state.cond:
                    state.displaced = True
                    state.stopping = True
                    state.cond.notify_all()
                return
            state.last_heartbeat = now_m
            # The lock lease rides the same beat: still the coordinator, so the
            # paths its workers hold stay held.
            self._renew_scope_best_effort(state)

        run = self._dal.get_run(state.run_id)
        if run is None:
            with state.cond:
                state.stopping = True
                state.cond.notify_all()
            return
        status = str(run["status"])
        if status == "cancelled":
            # H-19: cancel is external terminalization — kill, finalize, wait.
            self._handle_external_terminalization(state, reason="cancelled")
            return
        if status in {str(s) for s in TERMINAL_RUN_STATUSES}:
            # H-19: failed/completed stamped outside this process must also
            # stop live writers and finalize attempt/provider rows before the
            # coordinator exits into worktree cleanup.
            self._handle_external_terminalization(state, reason=status)
            return

        # Budget issue. ADVISORY (default): record + notify, keep scheduling.
        # BLOCK (opt-in): stop new spawns and block every remaining schedulable
        # task. A known breach and an unknown dollar total are distinct facts:
        # the latter is unenforceable, never silently treated as free.
        budget_issue = self._budget_issue(run)
        if budget_issue is not None:
            first_issue = False
            first_unknown = False
            with state.cond:
                # Exact dollars OR priced tokens: both are a cap actually
                # reached. Reading known_cost_usd alone here is what let an
                # unpriced fleet run past its ceiling forever.
                known_breach = self._cap_breached(budget_issue)
                unknown_sessions = int(budget_issue["unknown_cost_sessions"])
                if known_breach and not state.budget_overshot:
                    state.budget_overshot = True
                    first_issue = True
                if unknown_sessions:
                    state.unknown_cost_sessions = max(state.unknown_cost_sessions, unknown_sessions)
                    if not state.budget_unenforceable:
                        state.budget_unenforceable = True
                        first_unknown = True
                        first_issue = True
                if first_issue:
                    # Only a BLOCKING posture stops new spawns; advisory keeps
                    # the fleet scheduling and records the control failure.
                    state.budget_exhausted = budget_blocks()
                    state.cond.notify_all()
            if first_unknown:
                self._emit(
                    state.run_id,
                    ACTION_BUDGET_UNENFORCEABLE,
                    dict(budget_issue),
                )
            if first_issue:
                if budget_blocks():
                    self._block_remaining_for_budget(state, budget_issue)
                else:
                    self._notify_budget_issue(run, budget_issue)

        tasks = self._member_tasks(state.run_id, run)
        unknown_dependencies = self._unknown_dependency_ids(state.run_id)
        if unknown_dependencies:
            self._fail_run(
                state,
                self._unknown_dependency_diagnosis(unknown_dependencies),
            )
            return

        # Rate-limit park expiry.
        with state.cond:
            if state.park_until is not None and self._clock.now() >= state.park_until:
                state.park_until = None
                state.cond.notify_all()

        # Claim-lease sweep + orphan live-attempt adoption.
        with state.cond:
            busy = set(state.active_claims) | set(state.parked) | state.attaching
        for task in tasks:
            task_id = str(task["id"])
            if str(task["status"]) not in ("claimed", "in_progress") or task_id in busy:
                continue
            attempt = self._dal.current_attempt(task_id)
            if attempt is None:
                # The attempt died without terminalizing: the claim is a
                # stale lease — release it so the task becomes eligible again.
                if self._collab.release_claim(task_id):
                    LOG.info("swept stale claim on task %s", task_id)
            else:
                # Live attempt with no worker (crash-resume, or a lost
                # completion): attach a worker to await/settle it.
                self._enqueue_attach(state, task, attempt)

        # Parked-approval poll (the 30s fallback pass also polls approvals).
        with state.cond:
            parked_items = list(state.parked.items())
        for task_id, info in parked_items:
            row = self._sessions().get_session(str(info["session_id"]))
            session_state = str((row or {}).get("state") or "")
            if row is None or session_state != _AWAITING_APPROVAL:
                # Approval resolved (resumed, finished, or failed): the task
                # re-enqueues as an attach item and takes a slot again.
                with state.cond:
                    entry = state.parked.pop(task_id, None)
                    if entry is not None:
                        state.attaching.add(task_id)
                        state.resume_items.append(("attach", entry))
                        state.last_progress = self._clock.monotonic()
                        state.cond.notify_all()
                continue
            # Still parked. Classify it on the same cadence as a running
            # attempt (the CARRIER a deadline-only liveness check misses just
            # as badly): a parked attempt has no worker thread, so without this
            # its state is invisible until its deadline fires. The tracker
            # lives on the park entry, so it is created once per park and
            # disposed of with it; the classification is "approval_waiting",
            # never "stalled" — silence while parked is expected, and reading
            # it as a stall is precisely the false positive to avoid.
            tracker = info.get("liveness")
            if not isinstance(tracker, _LivenessTracker):
                tracker = _LivenessTracker(next_check=self._clock.monotonic())
                info["liveness"] = tracker
            self._liveness_tick(
                state,
                tracker,
                task_id=task_id,
                attempt_id=str(info.get("attempt_id") or ""),
                session_id=str(info["session_id"]),
                tier=str(info.get("tier") or "standard"),
                session_state=session_state,
            )
            # The tier timeout applies to a parked attempt exactly as it does to
            # a running one. An approval that is never delivered (lost hook POST
            # -> no approval row at all) or never decided must not hold the
            # attempt, its claim and the run open indefinitely.
            deadline = info.get("deadline")
            if deadline is None:
                # Belt and braces: an entry from a park site that predates this
                # (or a future one that forgets) is bounded from NOW rather than
                # exempted forever. Mutates the live entry, not the snapshot.
                with state.cond:
                    live = state.parked.get(task_id)
                    if live is not None and live.get("deadline") is None:
                        live["deadline"] = self._clock.monotonic() + self._timeout_seconds(
                            str(live.get("tier") or "standard")
                        )
                continue
            if self._clock.monotonic() < float(deadline):
                continue
            self._timeout_parked_attempt(state, tasks, info)

        self._recompute_target(state, run)
        # M-34: respawn dead worker threads every reconcile (not only on grow).
        # Capacity must not degrade permanently from a single pull-path fault.
        self._recover_worker_capacity(state, run, tasks)
        self._check_completion(state, run)

        # Run-level stall guard: nothing running, nothing parked, no claims,
        # no rate-limit park — and no progress for stall_minutes → fail loudly.
        with state.cond:
            idle = (
                not state.active_claims
                and not state.running_attempts
                and not state.parked
                and not state.attaching
                and not state.resume_items
                and state.park_until is None
                and not state.finished
                and not state.stopping
            )
        if idle and (self._clock.monotonic() - state.last_progress) > self._stall_minutes * 60.0:
            open_tasks = [t for t in tasks if str(t["status"]) == "open"]
            self._fail_run(
                state,
                "run stalled: nothing started and nothing running for "
                f"{self._stall_minutes:g} minutes ({len(open_tasks)} open task(s) "
                "that no slot could start — check router/provider/claim state)",
            )
            return

        if state.plan_dirty:
            state.plan_dirty = False
            self._write_plan_doc(state)

        with state.cond:
            state.cond.notify_all()

    def _handle_cancel(self, state: _RunState) -> None:
        """Cancel fanout — thin wrapper over H-19 external terminalization."""
        self._handle_external_terminalization(state, reason="cancelled")

    def _handle_external_terminalization(self, state: _RunState, *, reason: str) -> None:
        """External run terminalization fail-closed (H-19).

        When a run is stamped terminal outside the coordinator (operator
        cancel, API fail, adoption of a completed/failed row), live workers
        must not be abandoned:

          1. stop new work (``stopping`` / ``cancelled`` flags);
          2. ``request_kill`` every live worker session (provider process);
          3. finalize open attempt rows (idempotent ``close_attempt``);
          4. wait (bounded) for sessions to leave non-terminal states.

        Worktree cleanup runs later — only in the coordinator ``finally``
        after ``_join_workers`` — and itself refuses to race live writers.
        Late session completions after cancel are recorded and ignored by
        ``_settle_terminal``.
        """
        with state.cond:
            if state.terminalizing:
                # Still refresh stop flags so a late reconcile cannot re-spawn.
                state.stopping = True
                state.terminal_reason = state.terminal_reason or reason
                if reason == "cancelled":
                    state.cancelled = True
                state.cond.notify_all()
                return
            state.terminalizing = True
            state.terminal_reason = reason
            if reason == "cancelled":
                state.cancelled = True
            state.stopping = True
            state.cond.notify_all()
            running = list(state.running_attempts.values())
            parked = list(state.parked.values())

        killed_by = f"swarm-{reason}"
        session_ids: set[str] = set()
        for info in (*running, *parked):
            session_id = info.get("session_id")
            if session_id:
                session_ids.add(str(session_id))

        # DB is the source of truth for attempts that have not yet registered
        # in ``running_attempts`` (race between open_attempt and spawn bind).
        try:
            for task in self._member_tasks(state.run_id):
                attempt = self._dal.current_attempt(str(task["id"]))
                if attempt is None:
                    continue
                session_id = attempt.get("session_id")
                if session_id:
                    session_ids.add(str(session_id))
        except Exception:  # noqa: BLE001 -- kill path must still run on DB hiccup
            LOG.warning(
                "live-attempt scan failed during external terminalization of %s",
                state.run_id,
                exc_info=True,
            )

        for session_id in session_ids:
            try:
                self._sessions().request_kill(session_id, killed_by=killed_by)
            except Exception:  # noqa: BLE001
                LOG.warning("request_kill failed for %s", session_id, exc_info=True)

        # Finalize attempt rows BEFORE worktree cleanup can run. close_attempt
        # is idempotent (ended_at IS NULL CAS), so workers that later settle
        # the same attempt no-op.
        detail = f"run {reason} — external terminalization"
        try:
            for task in self._member_tasks(state.run_id):
                attempt = self._dal.current_attempt(str(task["id"]))
                if attempt is None:
                    continue
                try:
                    self._dal.close_attempt(str(attempt["id"]), "killed", detail[:500])
                except Exception:  # noqa: BLE001
                    LOG.warning(
                        "close_attempt failed for %s during external terminalization",
                        attempt.get("id"),
                        exc_info=True,
                    )
        except Exception:  # noqa: BLE001
            LOG.warning(
                "attempt finalization failed during external terminalization of %s",
                state.run_id,
                exc_info=True,
            )

        # Bounded wait so request_kill can land and await loops can exit before
        # the coordinator proceeds to join + worktree cleanup.
        self._wait_for_sessions_terminal(session_ids)

    def _wait_for_sessions_terminal(
        self,
        session_ids: set[str],
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Poll until every live session is terminal (or the deadline hits).

        H-19: worktree cleanup must not race a still-running writer. A timeout
        is not a silent success — cleanup later re-checks and refuses when
        writers remain.
        """
        if not session_ids:
            return
        deadline = self._clock.monotonic() + max(0.0, timeout_seconds)
        remaining = set(session_ids)
        while remaining and self._clock.monotonic() < deadline:
            still: set[str] = set()
            for session_id in remaining:
                try:
                    row = self._sessions().get_session(session_id)
                except Exception:  # noqa: BLE001
                    continue
                if row is None:
                    continue
                if str(row.get("state") or "") not in _TERMINAL_SESSION_STATES:
                    still.add(session_id)
            remaining = still
            if remaining:
                self._clock.sleep(min(self._await_poll_seconds, 0.2))
        if remaining:
            LOG.warning(
                "external terminalization: %d session(s) still non-terminal after wait",
                len(remaining),
            )

    @staticmethod
    def _cap_breached(issue: Mapping[str, Any]) -> bool:
        """Whether ``issue`` reports a cap actually reached (exact OR estimated).

        Reads the discriminator the gate already computed instead of re-deriving
        it from ``known_cost_usd``, which by construction excludes the priced
        tokens that can carry the breach. A payload from some older producer
        that carries no discriminator falls back to the original arithmetic, so
        this can only ever add breaches, never lose one.
        """
        quality = issue.get("cost_quality")
        if quality is not None:
            return str(quality) in {QUALITY_EXACT, QUALITY_ESTIMATED}
        try:
            return float(issue.get("known_cost_usd") or 0.0) >= float(issue["budget_usd_max"])
        except (KeyError, TypeError, ValueError):  # pragma: no cover -- malformed payload
            return False

    def _budget_issue(
        self,
        run: Mapping[str, Any],
        spend: RunBudgetSpend | None = None,
        project_spend: ProjectBudgetSpend | None = None,
    ) -> dict[str, Any] | None:
        """Return the live run or project dollar-control issue for ``run``.

        Run caps retain their current precedence.  Project caps aggregate all
        runs on the M1 ``project_id`` axis; unknown spend is unenforceable, not
        a free $0 observation.

        C4: a cap is also reached when MEASURED-but-unpriced tokens, priced into
        ``estimated_cost_usd``, push accrued spend over the ceiling.  That is
        reported with ``cost_quality='estimated'`` and ``cost_usd`` still None --
        the cap became enforceable, the total did not become known.  A known
        dollar breach still outranks it, so nothing that was already exact
        changes meaning.
        """
        budget_raw = run.get("budget_usd_max")
        if budget_raw is not None:
            budget = float(budget_raw)
            observed = spend or self._dal.budget_spend(str(run.get("id") or ""))
            known_breach = observed.known_cost_usd >= budget
            accrued_breach = observed.accrued_cost_usd >= budget
            if (
                known_breach
                or accrued_breach
                or observed.unknown_cost_sessions
                or observed.unknown_call_count
            ):
                return {
                    "reason": (
                        "budget_cap_reached" if known_breach or accrued_breach else "cost_unknown"
                    ),
                    "cap_scope": "run",
                    "budget_usd_max": budget,
                    "cost_usd": observed.cost_usd,
                    "known_cost_usd": observed.known_cost_usd,
                    "estimated_cost_usd": observed.estimated_cost_usd,
                    "accrued_cost_usd": observed.accrued_cost_usd,
                    "cost_quality": _cap_cost_quality(known_breach, accrued_breach),
                    "unknown_cost_sessions": observed.unknown_cost_sessions,
                    "unknown_call_count": observed.unknown_call_count,
                    "enforcement": "block" if budget_blocks() else "advisory",
                }

        project_id = str(run.get("project_id") or "").strip()
        if not project_id:
            return None
        observed_project = project_spend or self._dal.project_budget_spend(project_id)
        project_budget = observed_project.budget_usd
        if project_budget is None:
            return None
        known_breach = observed_project.known_cost_usd >= project_budget
        accrued_breach = observed_project.accrued_cost_usd >= project_budget
        if not known_breach and not accrued_breach and observed_project.safe_to_compare:
            return None
        issue: dict[str, Any] = {
            "reason": (
                "project_budget_cap_reached" if known_breach or accrued_breach else "cost_unknown"
            ),
            "cap_scope": "project",
            "project_id": project_id,
            # Preserve this field for existing metric and notification consumers.
            "budget_usd_max": project_budget,
            "project_budget_usd": project_budget,
            "cost_usd": observed_project.cost_usd,
            "known_cost_usd": observed_project.known_cost_usd,
            "estimated_cost_usd": observed_project.estimated_cost_usd,
            "accrued_cost_usd": observed_project.accrued_cost_usd,
            "cost_quality": _cap_cost_quality(known_breach, accrued_breach),
            "unknown_cost_sessions": observed_project.unknown_cost_sessions,
            "unknown_call_count": observed_project.unknown_call_count,
            "enforcement": "block" if budget_blocks() else "advisory",
        }
        allocation = self._dal.fair_share_allocation()
        if project_id in allocation:
            issue["fair_share_usd"] = allocation[project_id]
        return issue

    @staticmethod
    def _budget_issue_text(issue: Mapping[str, Any]) -> str:
        unknown = int(issue.get("unknown_cost_sessions") or 0)
        if str(issue.get("reason") or "") == "cost_unknown":
            noun = "session" if unknown == 1 else "sessions"
            return f"budget unenforceable: cost unknown for {unknown} {noun}"
        # An estimate carried the breach: say so rather than implying the cap was
        # crossed in measured dollars.
        estimated = str(issue.get("cost_quality") or "") == QUALITY_ESTIMATED
        if str(issue.get("cap_scope") or "") == "project":
            if estimated:
                return "project budget cap reached (estimated)"
            return "project budget cap reached"
        if estimated:
            return "budget cap reached (estimated)"
        if unknown:
            noun = "session" if unknown == 1 else "sessions"
            return f"budget cap reached; cost also unknown for {unknown} {noun}"
        return "budget cap reached"

    def _block_remaining_for_budget(self, state: _RunState, issue: Mapping[str, Any]) -> None:
        run = self._dal.get_run(state.run_id)
        if run is None:
            return
        reason = self._budget_issue_text(issue)
        with state.cond:
            busy = set(state.active_claims) | set(state.parked) | state.attaching
        for task in self._member_tasks(state.run_id, run):
            task_id = str(task["id"])
            if str(task["status"]) in _TERMINAL_TASK_STATUSES or task_id in busy:
                continue
            self._collab.update_board_task(task_id, {"status": "blocked"})
            self._emit(
                state.run_id,
                ACTION_TASK_BLOCKED,
                {"task_id": task_id, "reason": reason},
            )
        state.plan_dirty = True
        self._notify_budget_issue(run, issue)

    def _notify_budget_issue(self, run: Mapping[str, Any], issue: Mapping[str, Any]) -> None:
        """Say which run breached its cap or cannot enforce it.

        Without this the only signal a budget breach produced was Claude Code's
        own "Budget limit reached ($X of $Y)" banner from inside a worker — no run
        id, no goal, no way to tell which of several concurrent swarms stopped.
        Best-effort: a notification failure never affects scheduling.
        """
        try:
            from omniagentos.notifications.service import record_notification

            run_id = str(run.get("id") or "")
            budget = float(issue.get("budget_usd_max") or 0.0)
            known = float(issue.get("known_cost_usd") or 0.0)
            unknown = int(issue.get("unknown_cost_sessions") or 0)
            goal = str(run.get("goal") or "").strip()
            blocking = budget_blocks()
            outcome = (
                "Remaining tasks are blocked; raise the run budget to continue."
                if blocking
                else "The run is CONTINUING — this is a heads-up, not a stop."
            )
            cost_unknown = unknown > 0
            if cost_unknown:
                noun = "session" if unknown == 1 else "sessions"
                title = f"Swarm budget unenforceable: cost unknown for {unknown} {noun}"
                spend_text = (
                    f"at least ${known:,.2f} is known, but {unknown} {noun} have no dollar price"
                )
            else:
                scope = "project" if issue.get("cap_scope") == "project" else "run"
                title = f"Swarm {scope} passed its ${budget:,.2f} budget"
                spend_text = f"spent ${known:,.2f} of ${budget:,.2f}"
            record_notification(
                kind="blocked" if blocking else "info",
                title=title,
                body=f"{goal or run_id} — {spend_text}. {outcome}",
                severity="warning",
                ref_type="run",
                ref_id=run_id,
                payload={"run_id": run_id, **dict(issue)},
            )
        except Exception:  # noqa: BLE001 - notification must never affect scheduling
            LOG.debug("budget-exhaustion notification failed", exc_info=True)

    def _run_cap(self, run: Mapping[str, Any]) -> int:
        return max(1, min(int(run.get("max_concurrency") or MAX_SLOTS), MAX_SLOTS))

    def _recompute_target(self, state: _RunState, run: Mapping[str, Any]) -> None:
        """``target_n = clamp(min(run_cap, eligible+running, fair_share), 1, MAX_SLOTS)``.

        fair_share divides the swarm fleet budget across active swarms:
        ``(signed slots remaining + this run's live attempts) / active swarms``.
        The add-back exists because the ledger counts our own live sessions as
        consumed (without it a fully-utilized steady state would shrink itself
        to 1); the SIGNED remaining is what lets a ceiling drop shrink a run
        below its current width. Grow eager (threads started immediately),
        shrink lazy (workers exit at their next pull; running attempts are
        never killed by a shrink).
        """
        eligible_rows = self._dal.eligible_tasks(state.run_id)
        integration_extra = 1 if self._integration_ready(state, run) else 0
        with state.cond:
            running = len(state.active_claims) + len(state.attaching)
            demand = (
                len(eligible_rows)
                + integration_extra
                + len(state.resume_items)
                + len(state.parked)
                + running
            )
            own_live = len(state.running_attempts)
        try:
            available = self._limits.swarm_slots_remaining()
        except Exception:  # noqa: BLE001
            available = own_live or 1
        active_swarms = max(1, self._dal.active_run_count())
        fair_share = max(1, (available + own_live) // active_swarms)
        run_cap = self._run_cap(run)
        new_target = max(1, min(run_cap, max(1, demand), fair_share, MAX_SLOTS))
        with state.cond:
            old_target = state.target_n
            if new_target == old_target:
                return
            state.target_n = new_target
            state.plan_dirty = True
            state.cond.notify_all()
        self._emit(
            state.run_id,
            ACTION_RESIZE,
            {
                "from": old_target,
                "to": new_target,
                "reason": (
                    f"demand={demand} fair_share={fair_share} run_cap={run_cap} "
                    f"available_for_swarm={available} active_swarms={active_swarms}"
                ),
            },
        )
        if new_target > old_target:
            self._ensure_workers(state)

    def _ensure_workers(self, state: _RunState) -> None:
        with state.cond:
            if state.stopping:
                return
            for index in range(state.target_n):
                existing = state.workers.get(index)
                if existing is not None and existing.is_alive():
                    continue
                thread = threading.Thread(
                    target=self._worker,
                    args=(state, index),
                    name=f"swarm-{state.run_id}-w{index}",
                    daemon=True,
                )
                state.workers[index] = thread
                thread.start()
                self._emit(state.run_id, ACTION_SLOT_OPENED, {"slot": index})

    def _recover_worker_capacity(
        self,
        state: _RunState,
        run: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> None:
        """M-34: respawn dead worker threads; fail promptly if the pool is gone.

        ``_ensure_workers`` already replaces dead slots, but it was only called
        on launch and when ``target_n`` grew. A crashed worker left a dead
        ``threading.Thread`` in ``state.workers`` forever, so capacity degraded
        permanently and a fully dead pool waited for the stall guard.

        Every reconcile now re-checks liveness. **Only a thread that existed and
        then died is capacity loss** — missing slots (``None``) are normal at
        clean start (``_reconcile`` runs before ``_ensure_workers``) and on
        grow, and must never emit a false ``worker_capacity_loss`` or trip the
        fully-dead fail path (startup/budget false-positive).

        After a real death: emit a visible capacity-loss signal, respawn, and
        if demand remains with zero live workers after the respawn attempt,
        fail the run immediately with an operator-visible diagnosis (not the
        stall-guard "router/provider" misdirection).
        """
        del run  # reserved for future diagnostics; tasks carry demand signal
        if (
            state.stopping
            or state.finished
            or state.cancelled
            or state.displaced
            or state.terminalizing
            or state.budget_exhausted
        ):
            return
        with state.cond:
            target = state.target_n
            # CRASHED slots only — had a Thread that is no longer alive.
            dead_slots = [
                index
                for index in range(target)
                if (existing := state.workers.get(index)) is not None and not existing.is_alive()
            ]
            # Never-started slots (startup / grow) — fill quietly, no alarm.
            missing_slots = [index for index in range(target) if state.workers.get(index) is None]
            live_before = sum(
                1
                for index in range(target)
                if (existing := state.workers.get(index)) is not None and existing.is_alive()
            )
        if not dead_slots and not missing_slots:
            return
        if dead_slots:
            LOG.error(
                "run %s: worker capacity loss — %d/%d slot(s) dead; respawning",
                state.run_id,
                len(dead_slots),
                target,
            )
            self._emit(
                state.run_id,
                ACTION_RESIZE,
                {
                    "from": target,
                    "to": live_before,
                    "reason": (
                        f"worker_capacity_loss: {len(dead_slots)} dead slot(s) "
                        f"of {target}; respawning"
                    ),
                    "dead_slots": list(dead_slots),
                },
            )
        # Always fill missing/dead slots (idempotent with _ensure_workers).
        self._ensure_workers(state)
        # Fully-dead fail only after a REAL death + respawn attempt failed.
        # Clean-start missing slots must never take this path.
        if not dead_slots:
            return
        with state.cond:
            still_dead = [
                index
                for index in range(state.target_n)
                if (existing := state.workers.get(index)) is None or not existing.is_alive()
            ]
            live_after = state.target_n - len(still_dead)
        if live_after > 0:
            return
        openish = [t for t in tasks if str(t["status"]) in ("open", "claimed", "in_progress")]
        demand = bool(openish) or bool(state.resume_items) or bool(state.parked)
        if not demand:
            return
        self._fail_run(
            state,
            "worker pool capacity lost: all slots dead after respawn attempt "
            f"({len(openish)} open/claimed/in_progress task(s); "
            "check worker crash logs — not router/provider)",
        )

    def _join_workers(self, state: _RunState) -> None:
        with state.cond:
            state.stopping = True
            state.cond.notify_all()
            workers = list(state.workers.values())
        for thread in workers:
            thread.join(timeout=30)

    # ------------------------------------------------------------------
    # Completion + failure
    # ------------------------------------------------------------------

    def _member_tasks(
        self, run_id: str, run: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        run = run or self._dal.get_run(run_id) or {}
        root_id = str(run.get("board_task_id") or "")
        return [task for task in self._dal.tasks_for_run(run_id) if str(task["id"]) != root_id]

    def _integration_task(self, tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        for task in tasks:
            if self._swarm_json_of(task).get("integration"):
                return dict(task)
        return None

    def _integration_ready(self, state: _RunState, run: Mapping[str, Any]) -> bool:
        """The integration task is exempt from blocked propagation: it becomes
        ready when every prerequisite is TERMINAL (done OR blocked/cancelled),
        running over whatever completed — the plain eligibility query only
        covers the all-done case."""
        tasks = self._member_tasks(state.run_id, run)
        integration = self._integration_task(tasks)
        if integration is None:
            return False
        if (
            str(integration["status"]) != "open"
            or integration.get("claimed_by") is not None
            or self._dal.current_attempt(str(integration["id"])) is not None
        ):
            return False
        status_by_id = {str(t["id"]): str(t["status"]) for t in tasks}
        deps = [
            str(edge["depends_on_task_id"])
            for edge in self._dal.deps_for_run(state.run_id)
            if str(edge["task_id"]) == str(integration["id"])
        ]
        if not deps:
            return True
        statuses = [status_by_id.get(dep, "unknown") for dep in deps]
        if "unknown" in statuses:
            return False
        if all(s == "done" for s in statuses):
            return False  # plain eligibility already covers this
        return all(s in _TERMINAL_TASK_STATUSES for s in statuses)

    def _unknown_dependency_ids(
        self,
        run_id: str,
    ) -> tuple[str, ...]:
        """Return prerequisites whose task status is explicitly unknown.

        The DAL has its own eligibility query and must independently reject a
        dangling dependency row. This scheduler guard is still required: a
        missing prerequisite must neither release work nor wait for the generic
        stall timeout.
        """
        # Read edges first, then member rows. An atomic split adds new task
        # rows and rewires edges in one transaction while retaining its parent
        # row. This order therefore observes a self-consistent before/after
        # dependency set instead of mistaking a fresh subtask for a dangling
        # prerequisite when a split lands between scheduler reads.
        edges = self._dal.deps_for_run(run_id)
        status_by_id = {str(task["id"]): str(task["status"]) for task in self._member_tasks(run_id)}
        dependency_ids = {
            str(edge["depends_on_task_id"])
            for edge in edges
            if str(edge["task_id"]) in status_by_id
        }
        statuses = {
            dependency_id: status_by_id.get(dependency_id, "unknown")
            for dependency_id in dependency_ids
        }
        return tuple(
            sorted(
                dependency_id for dependency_id, status in statuses.items() if status == "unknown"
            )
        )

    @staticmethod
    def _unknown_dependency_diagnosis(dependency_ids: Sequence[str]) -> str:
        rendered = ", ".join(str(dependency_id) for dependency_id in dependency_ids[:20])
        return (
            f"unknown dependency status: prerequisite task row(s) missing from this run: {rendered}"
        )

    def _check_completion(self, state: _RunState, run: Mapping[str, Any]) -> None:
        if state.finished:
            return
        tasks = self._member_tasks(state.run_id, run)
        if not tasks:
            return
        if any(str(t["status"]) not in _TERMINAL_TASK_STATUSES for t in tasks):
            return
        with state.cond:
            if state.active_claims or state.running_attempts or state.parked or state.attaching:
                return
            state.finished = True
            state.stopping = True
            state.cond.notify_all()

        done = [t for t in tasks if str(t["status"]) == "done"]
        blocked = [t for t in tasks if str(t["status"]) == "blocked"]
        cancelled_real = [
            t
            for t in tasks
            if str(t["status"]) == "cancelled" and not self._swarm_json_of(t).get("split")
        ]
        partial = bool(blocked or cancelled_real) or state.budget_exhausted
        spend = self._dal.budget_spend(state.run_id)
        metrics = {
            "partial": partial,
            "tasks_done": len(done),
            "tasks_blocked": len(blocked),
            "cost_usd": spend.cost_usd,
            "known_cost_usd": spend.known_cost_usd,
            # Cap-accrual view (upper bound over unpriced-but-measured tokens).
            # Never a spend claim: cost_usd above stays the honest total.
            "estimated_cost_usd": spend.estimated_cost_usd,
            "accrued_cost_usd": spend.accrued_cost_usd,
            "unknown_cost_sessions": spend.unknown_cost_sessions,
            "budget_exhausted": state.budget_exhausted,
            "budget_overshot": state.budget_overshot,
            "budget_unenforceable": state.budget_unenforceable,
            # Documented overshoot bound: at most one in-flight attempt per
            # slot could pass the budget gate before the breach was observable.
            "budget_overshoot_bound_slots": state.target_n,
        }
        self._dal.set_metrics(state.run_id, metrics)
        if not done:
            self._fail_run(state, "no task completed", metrics=metrics)
            return
        self._dal.set_run_status(state.run_id, "completed")
        root_id = str(run.get("board_task_id") or "")
        if root_id:
            try:
                self._collab.update_board_task(root_id, {"status": "done"})
            except Exception:  # noqa: BLE001
                LOG.debug("could not close root card %s", root_id, exc_info=True)
        self._emit(state.run_id, ACTION_RUN_COMPLETED, metrics)
        self._write_plan_doc(state)

    def _fail_run(
        self, state: _RunState, diagnosis: str, metrics: dict[str, Any] | None = None
    ) -> None:
        run = self._dal.get_run(state.run_id)
        if run is not None and str(run["status"]) not in {str(s) for s in TERMINAL_RUN_STATUSES}:
            self._dal.set_run_status(state.run_id, "failed", error=diagnosis)
        self._emit(
            state.run_id,
            ACTION_RUN_FAILED,
            {"reason": "failed", "diagnosis": diagnosis, **(metrics or {})},
        )
        with state.cond:
            state.finished = True
            state.stopping = True
            state.cond.notify_all()

    # ------------------------------------------------------------------
    # Resume rebuild
    # ------------------------------------------------------------------

    def _rebuild_from_db(self, state: _RunState) -> None:
        """Crash-resume: rebuild coordinator state ENTIRELY from the database.

        - claimed/in_progress tasks with NO live attempt → claim swept (the
          lease died with the worker);
        - live attempts whose session is awaiting approval → parked;
        - every other live attempt → attach item (a worker re-awaits the
          existing session; ``open_attempt`` is never called for these, so a
          duplicate attempt is impossible — the partial unique index is the
          DB-level backstop).
        """
        run = self._dal.get_run(state.run_id) or {}
        for task in self._member_tasks(state.run_id, run):
            task_id = str(task["id"])
            if str(task["status"]) not in ("claimed", "in_progress"):
                continue
            attempt = self._dal.current_attempt(task_id)
            if attempt is None:
                if self._collab.release_claim(task_id):
                    LOG.info("resume: swept stale claim on %s", task_id)
                continue
            # Phase-2: a live attempt whose worktree vanished from disk cannot
            # settle (its session wrote into a directory that no longer
            # exists) — close it crashed and requeue mechanically; the
            # successor attempt re-creates the worktree AT THE BRANCH TIP, so
            # salvage-committed partial work relays forward. Gated on the
            # RECORDED registration (m6) so a resume without the ambient flag
            # still runs the check.
            if state.worktree_mode or state.worktree_recorded:
                swarm_json = self._dal.get_swarm_json(task_id) or {}
                worktree_path = str(swarm_json.get("worktree_path") or "")
                if worktree_path and not os.path.isdir(worktree_path):
                    # m7: the attempt's session may STILL be alive, writing
                    # into the vanished directory — kill it before requeueing
                    # (aligned with the timeout path) or the old session and
                    # the successor run concurrently.
                    stale_session = str(attempt.get("session_id") or "")
                    if stale_session:
                        try:
                            self._sessions().request_kill(
                                stale_session, killed_by="swarm-worktree-missing"
                            )
                        except Exception:  # noqa: BLE001
                            LOG.warning("request_kill failed for %s", stale_session, exc_info=True)
                    self._dal.close_attempt(
                        str(attempt["id"]), "crashed", "worktree missing on resume"
                    )
                    if self._collab.release_claim(task_id):
                        LOG.info("resume: task %s worktree missing — requeued", task_id)
                    self._mechanical_failure(
                        state,
                        task_id,
                        str(attempt.get("tier") or "standard"),
                        f"worktree missing on resume: {worktree_path}",
                    )
                    continue
            session_id = attempt.get("session_id")
            row = self._sessions().get_session(str(session_id)) if session_id else None
            if row is not None and str(row.get("state") or "") == _AWAITING_APPROVAL:
                resumed_tier = str(attempt.get("tier") or "standard")
                with state.cond:
                    state.parked[task_id] = {
                        "task_id": task_id,
                        "attempt_id": str(attempt["id"]),
                        "session_id": str(session_id),
                        "tier": resumed_tier,
                        # A resumed coordinator has no memory of the original
                        # deadline (monotonic time died with the old process),
                        # so the adopted park gets one fresh tier budget. It is
                        # still BOUNDED, which is the property that was missing.
                        "deadline": self._clock.monotonic() + self._timeout_seconds(resumed_tier),
                    }
                continue
            self._enqueue_attach(state, task, attempt)

        # Phase-2 orphan GC: worktrees whose task is already terminal are
        # pruned (salvaging) — a crashed coordinator can die between a task
        # going terminal and its worktree removal. Gated on the RECORDED
        # registration (m6), not the ambient mode resolution.
        if state.worktree_mode or state.worktree_recorded:
            live_keys = []
            for task in self._member_tasks(state.run_id, run):
                task_swarm_json = self._swarm_json_of(task)
                task_key = str(task_swarm_json.get("task_key") or task["id"])
                if str(task["status"]) not in _TERMINAL_TASK_STATUSES:
                    live_keys.append(task_key)
                elif task_swarm_json.get("worktree_kept"):
                    # R2a: a kept worktree (unsalvageable work) is treated as
                    # live for prune purposes in EVERY coordinator process —
                    # rebuild the in-memory set from the durable marker too.
                    live_keys.append(task_key)
                    if task_swarm_json.get("worktree_path"):
                        with state.cond:
                            state.kept_worktrees.add(str(task_swarm_json["worktree_path"]))
            try:
                with self._git_guard(state, state.working_dir, "orphan-worktree-prune"):
                    removed = self._worktrees().prune_orphans(
                        state.working_dir, state.run_id, live_keys
                    )
                if removed:
                    LOG.info(
                        "resume: pruned %d orphan worktree(s) for %s",
                        len(removed),
                        state.run_id,
                    )
            except Exception:  # noqa: BLE001 -- GC best-effort; terminal cleanup is the backstop.
                LOG.warning("worktree orphan prune failed for %s", state.run_id, exc_info=True)

    def _enqueue_attach(
        self, state: _RunState, task: Mapping[str, Any], attempt: Mapping[str, Any]
    ) -> None:
        task_id = str(task["id"])
        with state.cond:
            if (
                task_id in state.attaching
                or task_id in state.active_claims
                or task_id in state.parked
            ):
                return
            state.attaching.add(task_id)
            state.resume_items.append(
                (
                    "attach",
                    {
                        "task_id": task_id,
                        "attempt_id": str(attempt["id"]),
                        "session_id": str(attempt.get("session_id") or ""),
                        "tier": str(attempt.get("tier") or "standard"),
                    },
                )
            )
            state.cond.notify_all()

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _worker(self, state: _RunState, index: int) -> None:
        worker_id = f"swarm-{state.run_id}-w{index}"
        try:
            while True:
                item = self._next_work(state, index, worker_id)
                if item is None:
                    return
                self._execute_item(state, index, item)
        except Exception:  # noqa: BLE001 -- a worker must never die silently.
            LOG.exception("swarm worker %s crashed", worker_id)

    def _next_work(self, state: _RunState, index: int, worker_id: str) -> tuple[str, Any] | None:
        """Pull the next work item, honoring the slot counter, the rate-limit
        park, budget exhaustion, and cancel — re-checked before EVERY pull."""
        while True:
            with state.cond:
                if state.stopping or state.cancelled:
                    return None
                if index >= state.target_n:
                    return None  # lazy shrink: exit, never kill a sibling
                if state.budget_exhausted:
                    return None  # budget stops NEW spawns
                parked = state.park_until is not None and self._clock.now() < state.park_until
                if not parked and state.resume_items:
                    kind, payload = state.resume_items.popleft()
                    task_id = str(payload["task_id"])
                    state.attaching.discard(task_id)
                    state.active_claims[task_id] = index
                    return (kind, payload)
            if not parked:
                claimed = self._try_claim(state, index, worker_id)
                if claimed is not None:
                    # The rate-limit park can open between the pre-claim check
                    # above and the DB CAS `_try_claim` just completed -- both
                    # of which run outside `state.cond`, and `_try_claim` in
                    # particular does real DB work that widens the window
                    # under contention. That is a genuine claim (not merely an
                    # in-memory reservation), so it is RELEASED rather than
                    # raced into the router: re-validate `parked` under the
                    # same lock one more time before ever dispatching to a
                    # worker, closing the race that let a stray attempt slip
                    # a `router.route()` call in after cooling was already
                    # observed (measured as a rare hot-loop flake under
                    # heavy parallel load — WP5a all-cooling drill).
                    task_id = str(claimed["id"])
                    with state.cond:
                        now_parked = (
                            state.park_until is not None and self._clock.now() < state.park_until
                        )
                        if now_parked:
                            state.active_claims.pop(task_id, None)
                            state.requeue_after_pass[task_id] = state.worker_poll_pass + 1
                    if now_parked:
                        try:
                            self._collab.release_claim(task_id)
                        except Exception:  # noqa: BLE001
                            LOG.exception("could not release claim on %s (cooling race)", task_id)
                    else:
                        return ("task", claimed)
            self._wait_for_worker_poll(state, self._worker_poll_seconds)

    @staticmethod
    def _wait_for_worker_poll(state: _RunState, timeout: float) -> None:
        """Wait between idle passes and advance only after a full poll interval."""
        with state.cond:
            notified = state.cond.wait(timeout=timeout)
            if not notified:
                state.worker_poll_pass += 1

    def _try_claim(self, state: _RunState, index: int, worker_id: str) -> dict[str, Any] | None:
        for row in self._ordered_candidates(state):
            task_id = str(row["id"])
            with state.cond:
                if (
                    task_id in state.active_claims
                    or task_id in state.parked
                    or task_id in state.attaching
                ):
                    continue
                retry_after_pass = state.requeue_after_pass.get(task_id)
                if retry_after_pass is not None:
                    if state.worker_poll_pass < retry_after_pass:
                        continue
                    del state.requeue_after_pass[task_id]
                # Scope skip window: this task's declared paths were refused a
                # moment ago and nothing has changed since. Claiming it again
                # now would release the claim milliseconds later and spin. The
                # dict is empty unless scope locking is enforcing, so this costs
                # one truth test on the default path.
                if state.scope_backoff:
                    until = state.scope_backoff.get(task_id)
                    if until is not None:
                        if self._clock.monotonic() < until:
                            continue
                        del state.scope_backoff[task_id]
                # Pre-register BEFORE the DB CAS: the coordinator's claim
                # sweep reads this set, so there is no window where the task
                # is claimed in the DB but looks worker-less and gets swept.
                state.active_claims[task_id] = index
            if self._collab.claim_task(task_id, worker_id, int(row.get("claim_version") or 0)):
                with state.cond:
                    state.last_progress = self._clock.monotonic()
                return dict(row)
            with state.cond:
                if state.active_claims.get(task_id) == index:
                    del state.active_claims[task_id]
        return None

    def _ordered_candidates(self, state: _RunState) -> list[dict[str, Any]]:
        """Critical-path list scheduling over the eligible set.

        Order: longest remaining critical path (sum of est_agent_minutes down
        the dependent chain — the integration task closes every chain), then
        most direct dependents still unblocked, then longest own estimate.
        """
        rows = self._dal.eligible_tasks(state.run_id)
        run = self._dal.get_run(state.run_id) or {}
        tasks = self._member_tasks(state.run_id, run)
        unknown_dependencies = self._unknown_dependency_ids(state.run_id)
        if unknown_dependencies:
            # The coordinator owns run-level terminal decisions. Keep every
            # candidate out of worker hands and wake it with the explicit
            # diagnosis in case the dangling row appeared between reconciles.
            state.signals.put(("unknown_dependency", unknown_dependencies))
            return []
        if self._integration_ready(state, run):
            integration = self._integration_task(tasks)
            if integration is not None and all(
                str(r["id"]) != str(integration["id"]) for r in rows
            ):
                rows = [*rows, integration]
        if len(rows) <= 1:
            return [dict(r) for r in rows]

        dependents: dict[str, list[str]] = {}
        for edge in self._dal.deps_for_run(state.run_id):
            dependents.setdefault(str(edge["depends_on_task_id"]), []).append(str(edge["task_id"]))
        est: dict[str, int] = {}
        status: dict[str, str] = {}
        for task in tasks:
            task_id = str(task["id"])
            swarm_json = self._swarm_json_of(task)
            est[task_id] = max(0, int(swarm_json.get("est_agent_minutes") or 10))
            status[task_id] = str(task["status"])

        memo: dict[str, int] = {}

        def critical(task_id: str, trail: frozenset[str] = frozenset()) -> int:
            if task_id in memo:
                return memo[task_id]
            if task_id in trail:  # defensive: provisioned DAGs are acyclic
                return est.get(task_id, 0)
            downstream = max(
                (
                    critical(child, trail | {task_id})
                    for child in dependents.get(task_id, [])
                    if status.get(child) not in _TERMINAL_TASK_STATUSES
                ),
                default=0,
            )
            memo[task_id] = est.get(task_id, 0) + downstream
            return memo[task_id]

        def unblocked_dependents(task_id: str) -> int:
            return sum(
                1
                for child in dependents.get(task_id, [])
                if status.get(child) not in _TERMINAL_TASK_STATUSES
            )

        return sorted(
            (dict(r) for r in rows),
            key=lambda r: (
                -critical(str(r["id"])),
                -unblocked_dependents(str(r["id"])),
                -est.get(str(r["id"]), 0),
                str(r.get("created_at") or ""),
                str(r["id"]),
            ),
        )

    # ------------------------------------------------------------------
    # Attempt lifecycle
    # ------------------------------------------------------------------

    def _swarm_json_of(self, task: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = json.loads(task.get("swarm_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _canonical_exec_dir(path: str) -> str:
        """M-44: one normalized absolute path for claim + spawn binding.

        expanduser + abspath (not strict resolve) so a missing directory still
        has a stable identity for mismatch detection; realpath is used when the
        path exists so symlinked worktrees compare equal.
        """
        text = str(path or "").strip()
        if not text:
            return ""
        expanded = os.path.abspath(os.path.expanduser(text))
        try:
            if os.path.exists(expanded):
                return os.path.realpath(expanded)
        except OSError:
            pass
        return expanded

    def _execution_dir_mismatch(
        self,
        claimed_exec_dir: str,
        session_id: str,
        spawn_working_dir: str,
    ) -> str | None:
        """Return an error detail when spawn/session dir diverges from the claim.

        M-44: the scope claim and the executor's actual working directory must
        be the same canonical path. ``None`` means bound correctly.
        """
        claimed = self._canonical_exec_dir(claimed_exec_dir)
        spawned = self._canonical_exec_dir(spawn_working_dir)
        # Safety (`is not True`): unknown identity is an execution-realm mismatch.
        if claimed and spawned and inode_paths_equal(claimed, spawned) is not True:
            return (
                f"execution directory mismatch: claimed scope realm {claimed!r} "
                f"but spawn working_dir is {spawned!r}"
            )
        try:
            row = self._sessions().get_session(session_id)
        except Exception:  # noqa: BLE001 -- missing project_dir is not a hard fail.
            return None
        if row is None:
            return None
        project_dir = str(row.get("project_dir") or "").strip()
        if not project_dir:
            # Fakes and some providers omit project_dir; spawn working_dir was
            # already checked. Do not fail open on a missing field alone.
            return None
        session_dir = self._canonical_exec_dir(project_dir)
        # Safety (`is not True`): unknown identity is an execution-realm mismatch.
        if claimed and session_dir and inode_paths_equal(claimed, session_dir) is not True:
            return (
                f"execution directory mismatch: claimed scope realm {claimed!r} "
                f"but session project_dir is {session_dir!r}"
            )
        return None

    def _merge_swarm_json(self, task_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        current = self._dal.get_swarm_json(task_id) or {}
        current.update(updates)
        self._dal.set_swarm_json(task_id, current)
        return current

    def _start_tier(self, swarm_json: Mapping[str, Any]) -> str:
        hint = str(swarm_json.get("tier_hint") or "").strip().lower()
        if hint in TIER_LADDER:
            return hint
        complexity = str(swarm_json.get("complexity") or "standard").strip().lower()
        return complexity if complexity in TIER_LADDER else "standard"

    @staticmethod
    def _escalate(tier: str) -> str:
        try:
            position = TIER_LADDER.index(tier)
        except ValueError:
            return TIER_LADDER[-1]
        return TIER_LADDER[min(position + 1, len(TIER_LADDER) - 1)]

    def _timeout_seconds(self, tier: str) -> float:
        return float(self._timeouts.get(tier, self._timeouts.get("standard", 30.0))) * 60.0

    def _execute_item(self, state: _RunState, index: int, item: tuple[str, Any]) -> str:
        kind, payload = item
        task_id = str(payload["task_id"] if kind == "attach" else payload["id"])
        disposition = "requeue"
        try:
            if kind == "attach":
                disposition = self._execute_attach(state, payload)
            else:
                disposition = self._execute_task(state, index, payload)
        except Exception as exc:  # noqa: BLE001 -- every failure path must release the claim.
            LOG.exception("swarm worker failed on task %s", task_id)
            attempt = self._dal.current_attempt(task_id)
            if attempt is not None:
                self._dal.close_attempt(str(attempt["id"]), "crashed", f"worker exception: {exc}")
            disposition = "requeue"
        finally:
            with state.cond:
                state.active_claims.pop(task_id, None)
                state.running_attempts.pop(task_id, None)
                state.last_progress = self._clock.monotonic()
            # THE scope release point. Every terminal path of an attempt —
            # done, blocked, cancelled, requeue, and the `except` above that
            # catches a crash — funnels through this `finally`, which is why the
            # release lives here rather than being repeated down each branch of
            # _settle_terminal / _confirm_merge and forgotten on the next one.
            #
            # Two dispositions deliberately KEEP the locks:
            #  * "parked" — the attempt is awaiting approval. Its slot is freed
            #    and this worker returns, but the session is still alive and may
            #    still write; the coordinator keeps renewing the lease and the
            #    resolution path re-attaches to it.
            #  * "displaced" — another process adopted this run. Its locks were
            #    re-keyed to the adopter's generation inside adopt_run's
            #    transaction, so this release would match zero rows anyway; not
            #    calling it at all keeps the m10 rule ("a displaced coordinator
            #    writes nothing more") literally true.
            if disposition not in ("parked", "displaced"):
                self._release_task_scope(state, task_id)
            if disposition == "requeue":
                with state.cond:
                    state.requeue_after_pass[task_id] = state.worker_poll_pass + 1
                try:
                    self._collab.release_claim(task_id)
                except Exception:  # noqa: BLE001
                    LOG.exception("could not release claim on %s", task_id)
            state.signals.put(("wake", task_id))
        return disposition

    def _execute_task(self, state: _RunState, index: int, task_row: dict[str, Any]) -> str:
        task_id = str(task_row["id"])
        run = self._dal.get_run(state.run_id)
        # Cancel / external-terminal re-check #1: after claim.
        if (
            run is None
            or str(run["status"]) == "cancelled"
            or state.cancelled
            or state.terminalizing
            or str(run.get("status") or "") in {str(s) for s in TERMINAL_RUN_STATUSES}
        ):
            return "requeue"
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        tier = str(swarm_json.get("current_tier") or self._start_tier(swarm_json))

        decision = self._router.route(task_row, tier)
        if decision is None:
            # All providers cooling: hand the park decision to the coordinator.
            state.signals.put(("all_cooling", task_id))
            return "requeue"
        # WP5b: the router may override the START tier (learned start-tier)
        # on a first attempt; mid-ladder routes echo the scheduler's tier, so
        # the escalation ladder stays scheduler-owned.
        if decision.tier in TIER_LADDER:
            tier = decision.tier

        # D10 UltraCode fable cap: an ultra run holds ≤ ULTRA_DISTINCT_ACCOUNT_CAP
        # concurrent claude (fable-tier) sessions, spread across DISTINCT
        # accounts when capacity allows. Over-cap claude routes requeue (the
        # slot returns to the pool and picks up non-claude work); same-account
        # routes are re-reserved onto an untouched account, degrading to the
        # router's original pick — never blocking — when no distinct account
        # has headroom. Non-claude MAX-tier workers never count against the 3.
        if (
            decision.provider == "claude"
            and str(swarm_json.get("speed") or "").strip().lower() == "ultra"
        ):
            adjusted = self._apply_ultra_fable_cap(state, task_id, decision)
            if adjusted is None:
                return "requeue"
            decision = adjusted

        # The pre-spawn budget gate. In BLOCK mode this is what actually stops a
        # new attempt (and bounds the overshoot to one in-flight spawn per slot).
        # ADVISORY (default) still signals the reconcile pass — so the overshoot is
        # recorded and notified once — but lets the attempt proceed: refusing to
        # spawn here would requeue the task forever and hang the run.
        budget = run.get("budget_usd_max")
        budget_spend = self._dal.budget_spend(state.run_id) if budget is not None else None
        # This card's own unspent slice, when a split reserved one for it — the
        # ceiling below is capped by it so concurrent leaves of a split tree can
        # never each carry the whole root budget. None for every unreserved card.
        budget_reserved = self._dal.task_budget_reservation(task_id) if budget is not None else None
        budget_issue = self._budget_issue(run, budget_spend)
        if budget_issue is not None:
            state.signals.put(("budget", budget_issue))
            if budget_blocks():
                return "requeue"

        # Coordinator-owned pre-attempt snapshot (shared-index race: serialized).
        # In worktree mode the snapshot doubles as the BRANCH BASE commit: it
        # commits PLAN.md + any coordinator writes so fresh worktrees see them
        # (Phase-1 recovery semantics preserved).
        try:
            with self._git_guard(state, state.working_dir, "snapshot"):
                pre_attempt_dirty_paths = self._git.pre_attempt_dirty_paths(state.working_dir)
                coordinator_delta = self._pending_coordinator_delta(state)
                # Bind approved digests + modes into the snapshot so git cannot
                # stage a later operator rewrite of the same pathname (TOCTOU
                # between eligibility and ``git add``), and cannot pick up an
                # operator-only mode change via live path bits.
                expected_digests = {
                    path: digest
                    for path, (_exists, digest, _mode) in coordinator_delta.items()
                    if digest is not None
                }
                expected_modes = {
                    path: mode
                    for path, (_exists, digest, mode) in coordinator_delta.items()
                    if digest is not None
                }
                snapshot_sha = self._git.snapshot(
                    state.working_dir,
                    f"swarm {state.run_id}: pre-attempt snapshot for task {task_id}",
                    sorted(coordinator_delta),
                    expected_digests=expected_digests or None,
                    expected_modes=expected_modes or None,
                )
                # Only settle paths still matching the approved content — a
                # skipped mismatch must remain pending (or drop on next rewrite).
                # Mode-only working-tree drift does not block settle: the commit
                # already used the approved mode.
                current_after = self._coordinator_file_states(state.working_dir)
                settled = {
                    path: expected
                    for path, expected in coordinator_delta.items()
                    if self._coordinator_content_matches(current_after.get(path), expected)
                }
                self._settle_coordinator_delta(state, settled)
        except ScopeCommitBusy:
            # Another PROCESS is mid-commit in this workspace and outlasted the
            # wait. Requeue: no attempt was opened, no retry is consumed, and the
            # task is picked up again once the workspace is quiet.
            LOG.warning(
                "run %s: pre-attempt snapshot blocked by another process's commit "
                "lock — requeueing task %s",
                state.run_id,
                task_id,
                exc_info=True,
            )
            return "requeue"
        task_key = str(swarm_json.get("task_key") or task_id)
        use_worktree = (
            state.worktree_mode
            and not swarm_json.get("integration")
            and not swarm_json.get("bootstrap")
        )
        worktree_info = None
        worktree_git_dir: str | None = None
        if use_worktree:
            # Integration/bootstrap are exempt above: integration owns '.' and
            # must see the merged tree; bootstrap installs into the main
            # workspace before any worktree exists (it serializes every plan).
            try:
                with state.git_lock:
                    worktree_info = self._worktrees().create(
                        state.working_dir, state.run_id, task_key, snapshot_sha
                    )
                    # R1: the worker's own linked-worktree gitdir
                    # (<common>/worktrees/<id>) must be re-opened by the
                    # profile after the worktrees-subtree deny.
                    worktree_git_dir = self._worktrees().worktree_git_dir(worktree_info.path)
            except Exception as exc:  # noqa: BLE001 -- infra failure, not the worker's fault.
                LOG.exception("worktree create failed for task %s", task_id)
                return self._mechanical_failure(
                    state, task_id, tier, f"worktree create failed: {exc}"
                )
        updates: dict[str, Any] = {
            "snapshot_sha": snapshot_sha,
            "current_tier": tier,
            # Durable across approval parking and coordinator adoption: a
            # shared-directory settle must never attribute ambient operator
            # dirt to this worker and restore it from the scoped snapshot.
            "pre_attempt_dirty_paths": sorted(set(pre_attempt_dirty_paths)),
        }
        if worktree_info is not None:
            # Successor attempts reuse the branch AT TIP; the ownership-diff
            # base stays the ORIGINAL fork point so committed partial work
            # from prior attempts is still diffed (and merged) as task output.
            if worktree_info.reused and swarm_json.get("worktree_base_sha"):
                worktree_base = str(swarm_json["worktree_base_sha"])
            else:
                worktree_base = worktree_info.base_sha
            updates.update(
                {
                    "worktree_path": worktree_info.path,
                    "worktree_branch": worktree_info.branch,
                    "worktree_base_sha": worktree_base,
                }
            )
        elif swarm_json.get("worktree_branch"):
            # A run that fell back to Phase 1 (resume with the flag off / probe
            # failure) must not brief workers with stale worktree rules.
            updates.update({"worktree_path": None, "worktree_branch": None})
        # M1: the integration task of a worktree-mode run gets the merge-
        # permitting brief variant (it must manually merge routed conflict
        # branches in the main workspace — the Phase-1 "NEVER run git" rule
        # would contradict that feedback). Cleared on Phase-1 fallback —
        # UNLESS conflict feedback was already routed (R6): a fallback run
        # whose integration still carries "merge branch X manually" entries
        # must keep the permission or its brief contradicts itself and the
        # routed branches' confirmed work is silently dropped.
        has_routed_conflicts = any(
            isinstance(entry, Mapping) and str(entry.get("source") or "") == "merge_conflict"
            for entry in (swarm_json.get("feedback") or [])
        )
        if swarm_json.get("integration") and (state.worktree_mode or has_routed_conflicts):
            updates["worktree_integration"] = True
        elif swarm_json.get("worktree_integration"):
            updates["worktree_integration"] = None
        swarm_json = self._merge_swarm_json(task_id, updates)
        if worktree_info is not None:
            self._emit(
                state.run_id,
                ACTION_WORKTREE_CREATED,
                {
                    "task_id": task_id,
                    "path": worktree_info.path,
                    "branch": worktree_info.branch,
                    "base_sha": updates["worktree_base_sha"],
                    "reused": worktree_info.reused,
                },
            )

        # Scope claim, FUSED with the attempt open: the last thing before this
        # task becomes a live attempt is taking the paths it declared. Ordered
        # here — after the worktree exists, before open_attempt — for two
        # reasons: the realm is the directory the worker will actually write in
        # (its worktree, or the workspace in Phase-1 mode), which is only known
        # now; and a refusal must leave NO trace in the attempt ledger. A refused
        # task has opened nothing, spawned nothing and consumed no retry.
        #
        # M-44: claim realm and spawn cwd MUST be the same canonical directory.
        # Resolve once, persist on swarm_json, and pass that single value to
        # claim + SpawnRequest.working_dir. A later divergence is a hard failure.
        exec_dir = self._canonical_exec_dir(
            worktree_info.path if worktree_info is not None else state.working_dir
        )
        swarm_json = self._merge_swarm_json(task_id, {"execution_dir": exec_dir})
        if not self._claim_task_scope(
            state, task_id, exec_dir, [str(p) for p in (swarm_json.get("owned_paths") or [])]
        ):
            return "requeue"

        attempt = self._dal.open_attempt(
            state.run_id,
            task_id,
            provider=decision.provider,
            model=decision.model,
            tier=tier,
            account_id=decision.account_id,
            effort=decision.effort,
        )
        self._collab.update_board_task(task_id, {"status": "in_progress"})
        try:
            from omniagentos.brandpacks.pack import project_contract_mode

            contract_mode = project_contract_mode()
            if contract_mode != "off":
                from omniagentos.workmodes import reconcile_task_mode

                task_text = f"{task_row.get('title') or ''}\n{task_row.get('description') or ''}"
                mode_decision = reconcile_task_mode(
                    task_row.get("task_mode") or swarm_json.get("task_mode"),
                    task_text,
                )
                if contract_mode == "shadow":
                    LOG.info(
                        "project contract task_mode shadow diff task=%s mode=%s source=%s",
                        task_id,
                        mode_decision.mode.value,
                        mode_decision.source,
                    )
                else:
                    # migration 058 already owns this column. Keep the write at
                    # the scheduler spawn boundary and do not create schema.
                    self._collab._store._write(
                        "UPDATE board_tasks SET task_mode = ? WHERE id = ?",
                        (mode_decision.mode.value, task_id),
                    )
        except Exception:  # noqa: BLE001 -- a context stamp must never block spawning.
            LOG.debug("could not stamp project contract task_mode", exc_info=True)

        # Cancel / external-terminal re-check #2: before spawn.
        run = self._dal.get_run(state.run_id)
        if (
            run is None
            or str(run["status"]) == "cancelled"
            or state.cancelled
            or state.terminalizing
            or str((run or {}).get("status") or "") in {str(s) for s in TERMINAL_RUN_STATUSES}
        ):
            self._dal.close_attempt(
                str(attempt["id"]),
                "killed",
                "run terminalized before spawn — external terminalization",
            )
            return "cancelled"

        if swarm_json.get("integration") and not state.merge_started:
            state.merge_started = True
            self._dal.set_run_status(state.run_id, "merging")
            self._emit(state.run_id, ACTION_MERGE_STARTED, {"task_id": task_id})

        neighbor_statuses = {
            str(self._swarm_json_of(t).get("task_key") or t["id"]): str(t["status"])
            for t in self._member_tasks(state.run_id, run)
            if str(t["id"]) != task_id
        }
        # PKG-REQUEST-SUBTASKS: when the feature is on, tell the worker the EXACT
        # attempt-bound path (beside its workbook, already an extra write root) at
        # which a subtasks_request.json is honored; off → no section, and the
        # coordinator ignores any such file at completion. The path is derived
        # from the SAME spawner/var_root the attempt runs under (single source of
        # truth) so the settle-time read cannot diverge from the spawn-time write.
        subtasks_request_path: str | None = None
        insession_on = False
        if self._subtask_requests_enabled():
            path = self._subtasks_request_path(state.run_id, task_id, str(attempt["id"]))
            subtasks_request_path = str(path) if path is not None else None
            # PKG-INSESSION-FANOUT: the grant-wait protocol variant (and the
            # Task-tool unlock downstream) apply only to claude workers while
            # the feature flag is on.
            insession_on = (
                subtasks_request_path is not None
                and decision.provider == "claude"
                and self._insession_enabled()
            )
        request = SpawnRequest(
            run_id=state.run_id,
            task_id=task_id,
            task_key=task_key,
            attempt_id=str(attempt["id"]),
            working_dir=exec_dir,
            subtasks_request_path=subtasks_request_path,
            prompt=build_worker_brief(
                run,
                task_row,
                swarm_json,
                neighbor_statuses,
                subtasks_request_path,
                insession=insession_on,
                project_store=self._collab._store,
            ),
            provider=decision.provider,
            model=decision.model,
            tier=tier,
            account_id=decision.account_id,
            reservation_id=decision.reservation_id,
            effort=decision.effort,
            idle_minutes=self._timeout_seconds(tier) / 60.0 * IDLE_TIMEOUT_FRACTION,
            budget_usd_max=self._attempt_budget_ceiling(budget, budget_spend, budget_reserved),
            # Worktree mode: the main repo's git COMMON dir must be a Seatbelt
            # write root or every worker `git commit` dies EPERM (the profile
            # still denies .git/hooks + .git/config inside it, plus — M3 —
            # refs/heads and packed-refs, re-opening ONLY the run's own
            # branch namespace, which rides along as the second root here).
            # R1: the whole <common>/worktrees subtree is denied (sibling
            # gitdir HEAD hijack), so the worker's OWN linked-worktree gitdir
            # rides along as the third root — the profile re-opens exactly it.
            extra_write_roots=(
                tuple(
                    root
                    for root in (
                        state.git_common_dir,
                        state.git_ref_namespace,
                        worktree_git_dir,
                    )
                    if root
                )
                if worktree_info is not None and state.git_common_dir
                else ()
            ),
        )
        try:
            session_id = self._spawner.spawn(request)
        except Exception as exc:  # noqa: BLE001 -- spawn failure = mechanical failure.
            self._dal.close_attempt(str(attempt["id"]), "crashed", f"spawn failed: {exc}")
            return self._mechanical_failure(state, task_id, tier, f"spawn failed: {exc}")
        # M-44: reject if the spawner/session bound a different directory than
        # the scope claim (claim would be a lie under enforce).
        mismatch = self._execution_dir_mismatch(exec_dir, session_id, request.working_dir)
        if mismatch is not None:
            try:
                self._sessions().request_kill(session_id, killed_by="swarm-exec-dir-mismatch")
            except Exception:  # noqa: BLE001
                LOG.warning("request_kill failed after exec_dir mismatch", exc_info=True)
            self._dal.close_attempt(str(attempt["id"]), "crashed", mismatch[:500])
            return self._mechanical_failure(state, task_id, tier, mismatch)
        # Persist the session onto the attempt row — resume_swarm re-attaches
        # through exactly this binding.
        self._dal.bind_attempt_session(str(attempt["id"]), session_id)

        with state.cond:
            state.running_attempts[task_id] = {
                "task_id": task_id,
                "attempt_id": str(attempt["id"]),
                "session_id": session_id,
                "slot": index,
            }
            state.last_progress = self._clock.monotonic()
        self._emit(
            state.run_id,
            ACTION_TASK_ASSIGNED,
            {
                "task_id": task_id,
                "attempt_id": str(attempt["id"]),
                "session_id": session_id,
                "provider": decision.provider,
                "model": decision.model,
                "tier": tier,
                "effort": decision.effort,
                "slot": index,
            },
        )
        # Fleet panel: explicit spawn edge (cheap observability, not a token stream).
        try:
            from omniagentos.swarm.contracts import ACTION_WORKER_SPAWNED

            title = str(task_row.get("title") or task_id)
            swarm_json = {}
            try:
                raw = task_row.get("swarm_json")
                if isinstance(raw, str) and raw.strip():
                    import json as _json

                    swarm_json = _json.loads(raw)
                elif isinstance(raw, dict):
                    swarm_json = raw
            except Exception:  # noqa: BLE001
                swarm_json = {}
            role = str(job_role_from_swarm_json(swarm_json))
            self._emit(
                state.run_id,
                ACTION_WORKER_SPAWNED,
                {
                    "task_id": task_id,
                    "task_title": title[:200],
                    "attempt_id": str(attempt["id"]),
                    "session_id": session_id,
                    "provider": decision.provider,
                    "model": decision.model,
                    "tier": tier,
                    "role": role,
                    "slot": index,
                    "running_count": len(state.running_attempts),
                },
            )
        except Exception:  # noqa: BLE001 — observability only
            pass
        return self._await_and_settle(
            state, task_row, str(attempt["id"]), session_id, tier, snapshot_sha
        )

    def _execute_attach(self, state: _RunState, info: Mapping[str, Any]) -> str:
        """Re-await an EXISTING attempt session (approval resolution or
        crash-resume adoption) — never a new attempt, never a new spawn."""
        task_id = str(info["task_id"])
        task_row = next(
            (t for t in self._dal.tasks_for_run(state.run_id) if str(t["id"]) == task_id),
            None,
        )
        if task_row is None:
            return "requeue"
        attempt = self._dal.current_attempt(task_id)
        if attempt is None or str(attempt["id"]) != str(info["attempt_id"]):
            return "requeue"
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        tier = str(info.get("tier") or swarm_json.get("current_tier") or "standard")
        snapshot_sha = str(swarm_json.get("snapshot_sha") or "")
        session_id = str(info.get("session_id") or "")
        if not session_id:
            self._dal.close_attempt(str(attempt["id"]), "crashed", "attempt has no session")
            return self._mechanical_failure(state, task_id, tier, "attempt lost its session")
        # Re-take the scope this attempt is already writing under. Never refuses
        # (see _reclaim_task_scope): the session is live, and the point here is
        # to put the locks on THIS coordinator's generation so its heartbeat
        # keeps renewing them.
        #
        # M-44: prefer the recorded execution_dir (what was claimed + spawned)
        # over a reconstructed worktree/workspace path so reclaim cannot lie.
        reclaim_dir = self._canonical_exec_dir(
            str(swarm_json.get("execution_dir") or "")
            or str(swarm_json.get("worktree_path") or "")
            or state.working_dir
        )
        self._reclaim_task_scope(
            state,
            task_id,
            reclaim_dir,
            [str(p) for p in (swarm_json.get("owned_paths") or [])],
        )
        with state.cond:
            state.running_attempts[task_id] = {
                "task_id": task_id,
                "attempt_id": str(attempt["id"]),
                "session_id": session_id,
                "slot": None,
            }
        return self._await_and_settle(
            state,
            dict(task_row),
            str(attempt["id"]),
            session_id,
            tier,
            snapshot_sha,
            # Approval-park resolution re-attaches to the SAME attempt: it
            # inherits the original deadline instead of minting a fresh full
            # budget, so parking cannot launder an attempt past its tier
            # timeout one park at a time.
            deadline=info.get("deadline"),
        )

    def _await_and_settle(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session_id: str,
        tier: str,
        snapshot_sha: str,
        deadline: float | None = None,
    ) -> str:
        """The await-terminal contract (executor ``_await_terminal`` shape,
        swarm-tiered): poll the session store until terminal, approval-park,
        tiered timeout, or cancel.

        ``deadline`` (monotonic) carries an in-flight attempt's ORIGINAL tier
        deadline across an approval park; None starts a fresh tier budget.

        Liveness is ALSO classified on a fixed cadence while the attempt still
        has budget (``LIVENESS_POLL_SECONDS``), observe-only — see
        ``_liveness_tick``. The deadline branch below is unchanged: it remains
        the only place that decides a kill."""
        task_id = str(task_row["id"])
        if deadline is None:
            deadline = self._clock.monotonic() + self._timeout_seconds(tier)
        else:
            deadline = float(deadline)
        # First tick is one whole interval in, not immediately: an attempt that
        # has just spawned has nothing to say yet.
        liveness = _LivenessTracker(
            next_check=self._clock.monotonic() + self._liveness_poll_seconds
        )
        try:
            return self._await_loop(
                state,
                task_row,
                attempt_id,
                session_id,
                tier,
                snapshot_sha,
                deadline,
                liveness,
            )
        finally:
            # Emitted on EVERY exit (terminal, timeout, park, cancel, displace)
            # so the false-reap denominator is the set of observed attempts, not
            # only the ones that ended well.
            self._emit_liveness_summary(
                state,
                liveness,
                task_id=task_id,
                attempt_id=attempt_id,
                session_id=session_id,
                tier=tier,
            )

    def _await_loop(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session_id: str,
        tier: str,
        snapshot_sha: str,
        deadline: float,
        liveness: _LivenessTracker,
    ) -> str:
        """The await poll loop itself (see :meth:`_await_and_settle`).

        Split out ONLY so the liveness summary can be emitted from a ``finally``
        on every one of this loop's exits; the loop body is unchanged."""
        task_id = str(task_row["id"])
        # PKG-INSESSION-FANOUT: whether this attempt's live grant scan has
        # reached a decision (granted, denied, or not applicable). At most one
        # decision per attempt.
        insession_decided = False
        while True:
            if state.displaced:
                # m10: displaced coordinator — abandon the await WITHOUT
                # closing the attempt or releasing the claim (the adopting
                # coordinator re-attaches through exactly this DB state).
                return "displaced"
            # H-19: honor ALL external terminals (cancel/failed/completed), not
            # only cancel — state.terminalizing is set by the reconcile path for
            # every stamped TERMINAL_RUN_STATUSES row.
            if state.cancelled or state.terminalizing:
                reason = state.terminal_reason or (
                    "cancelled" if state.cancelled else "terminalized"
                )
                killed_by = f"swarm-{reason}"
                try:
                    self._sessions().request_kill(session_id, killed_by=killed_by)
                except Exception:  # noqa: BLE001
                    LOG.debug("request_kill failed for %s", session_id, exc_info=True)
                self._dal.close_attempt(
                    attempt_id,
                    "killed",
                    f"run {reason} mid-attempt — external terminalization",
                )
                return "cancelled"

            row = self._sessions().get_session(session_id)
            if row is None:
                self._dal.close_attempt(attempt_id, "crashed", "session row disappeared")
                return self._mechanical_failure(
                    state, task_id, tier, f"session {session_id} disappeared"
                )
            session_state = str(row.get("state") or "")
            if session_state == _AWAITING_APPROVAL:
                # AWAITING_APPROVAL releases its slot: park out-of-slot, the
                # slot returns to the pool, resolution re-enqueues (polled in
                # the coordinator's fallback pass).
                #
                # The tier deadline PARKS WITH IT. Before this, leaving the
                # await loop left the attempt with no clock at all: an approval
                # that was never delivered held the attempt (and the run) open
                # forever, which is exactly what bench swr_8474e958870543388267
                # measured — 47.6 minutes against a 30-minute standard tier.
                # The fallback pass enforces this deadline on parked entries.
                with state.cond:
                    state.parked[task_id] = {
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "tier": tier,
                        "deadline": deadline,
                    }
                    state.running_attempts.pop(task_id, None)
                    state.last_progress = self._clock.monotonic()
                self._emit(
                    state.run_id,
                    ACTION_APPROVAL_PARKED,
                    {"task_id": task_id, "session_id": session_id},
                )
                return "parked"
            if session_state in _TERMINAL_SESSION_STATES:
                return self._settle_terminal(
                    state, task_row, attempt_id, dict(row), tier, snapshot_sha
                )

            if self._clock.monotonic() >= deadline:
                idle_threshold_seconds = self._timeout_seconds(tier) * IDLE_TIMEOUT_FRACTION
                # Deliberately the RAW binary check, not the periodic
                # classifier: this is the kill decision, and it must keep
                # answering exactly the question it answered before.
                deadline_liveness = is_making_progress(
                    session_id,
                    idle_threshold_seconds,
                    dal=self._sessions(),  # type: ignore[arg-type]
                )
                if deadline_liveness.get("status") == "slow":
                    self._clock.sleep(self._await_poll_seconds)
                    continue
                return self._handle_timeout(state, task_row, attempt_id, session_id, tier)
            # PKG-INSESSION-FANOUT: answer a RUNNING attempt's subtasks request
            # with a live grant when guards + agent capacity allow. Placed
            # after the terminal/deadline checks so a session on its way out
            # is never granted.
            if not insession_decided:
                insession_decided = self._maybe_grant_insession(
                    state, task_row, attempt_id, dict(row)
                )
            # Pre-deadline liveness: reached only while the attempt still has
            # budget (the deadline branch above returns or continues), so this
            # is strictly the question "what is it doing?", never a kill.
            self._liveness_tick(
                state,
                liveness,
                task_id=task_id,
                attempt_id=attempt_id,
                session_id=session_id,
                tier=tier,
                session_state=session_state,
            )
            self._clock.sleep(self._await_poll_seconds)

    def _liveness_tick(
        self,
        state: _RunState,
        liveness: _LivenessTracker,
        *,
        task_id: str,
        attempt_id: str,
        session_id: str,
        tier: str,
        session_state: str | None = None,
    ) -> str | None:
        """Classify an in-flight attempt on the liveness cadence. Observe-only.

        Returns the status when a tick actually ran (None when it was not yet
        due). Emits ``liveness_changed`` ONLY on a transition, so a healthy
        attempt that runs for an hour costs one event, not sixty.

        Never raises: an observation fault must not terminate an await that is
        otherwise healthy, and must not be mistaken for a stall either — the
        classifier's own fail-safe reports "unknown" for that.
        """
        now = self._clock.monotonic()
        if not liveness.due(now):
            return None
        liveness.schedule_next(now, self._liveness_poll_seconds)
        idle_threshold_seconds = self._timeout_seconds(tier) * IDLE_TIMEOUT_FRACTION
        try:
            result = classify_liveness(
                session_id,
                idle_threshold_seconds,
                dal=self._sessions(),  # type: ignore[arg-type]
                session_state=session_state,
            )
            status = str(result.get("status") or "unknown")
        except Exception:  # noqa: BLE001 -- observation is never control flow
            LOG.debug("liveness tick failed for session %s", session_id, exc_info=True)
            result = {}
            status = "unknown"
        if status not in LIVENESS_STATUSES:
            status = "unknown"
        previous = liveness.status
        if not liveness.record(status):
            return status
        payload = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "tier": tier,
            "status": status,
            "previous": previous,
            "last_activity_seconds_ago": result.get("last_activity_seconds_ago"),
            "idle_threshold_seconds": idle_threshold_seconds,
            "tick": liveness.ticks,
            # What a pre-deadline REAPER would have done here. Recorded, not
            # acted on: until the false-positive rate of this classifier has
            # been measured on real runs, the tick observes and the wall
            # deadline still owns every kill.
            "would_reap": status == "stalled",
        }
        self._emit(state.run_id, ACTION_LIVENESS_CHANGED, payload)
        if status == "stalled":
            LOG.info("swarm liveness stalled %s", json.dumps(payload, sort_keys=True, default=str))
        return status

    def _emit_liveness_summary(
        self,
        state: _RunState,
        liveness: _LivenessTracker,
        *,
        task_id: str,
        attempt_id: str,
        session_id: str,
        tier: str,
    ) -> None:
        """One per observed AWAIT SEGMENT: the false-reap instrument, day one.

        A segment is one pass through the await loop, so an attempt that parks
        on an approval and re-attaches reports twice — the counts are additive
        per ``attempt_id``, which is why the id is carried.

        ``stalled_ticks`` is the numerator a pre-deadline reaper would have
        acted on and ``ticks`` is the denominator; joining this event to the
        attempt's recorded ``end_reason`` makes "would have reaped an attempt
        that then succeeded" countable BEFORE anything is allowed to reap on it.
        """
        if liveness.ticks <= 0:
            return  # never observed (short attempt) — nothing to report
        payload = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "tier": tier,
            "ticks": liveness.ticks,
            "stalled_ticks": liveness.counts.get("stalled", 0),
            "counts": dict(liveness.counts),
            "final_status": liveness.status,
        }
        self._emit(state.run_id, ACTION_LIVENESS_SUMMARY, payload)
        if payload["stalled_ticks"]:
            LOG.info("swarm liveness summary %s", json.dumps(payload, sort_keys=True, default=str))

    def _await_session_terminal(
        self, session_id: str, timeout_seconds: float = 10.0, poll_interval: float = 0.1
    ) -> bool:
        """Poll session store until terminal state or timeout.

        Returns True if the session reached a terminal state (killed, failed, completed),
        False if the timeout was exceeded and the session is still running.

        This prevents the double-spend window where a killed session request was
        issued but never confirmed, so the task gets requeued while the old session
        is still alive and billing.
        """
        from omniagentos.sessions.dal import TERMINAL_SESSION_STATES

        deadline = self._clock.monotonic() + timeout_seconds
        while self._clock.monotonic() < deadline:
            row = self._sessions().get_session(session_id)
            if row is None:
                # Session doesn't exist anymore (successfully cleaned up)
                return True

            state_str = str(row.get("state") or "")
            if state_str in TERMINAL_SESSION_STATES:
                # Session reached terminal state
                return True

            # Still running, sleep before next poll
            self._clock.sleep(poll_interval)

        # Timeout exceeded, session still running - mark as orphaned
        swarm_json = self._dal.get_swarm_json(session_id) or {}
        orphaned_count = int(swarm_json.get("orphaned_session_count") or 0) + 1
        try:
            self._merge_swarm_json(
                session_id,
                {"orphaned_session_count": orphaned_count},
            )
        except Exception:  # noqa: BLE001
            LOG.warning(
                "Could not mark session %s as orphaned (swarm_json update failed)",
                session_id,
                exc_info=True,
            )

        return False

    def _handle_timeout(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session_id: str,
        tier: str,
        *,
        parked: bool = False,
    ) -> str:
        """Tiered timeouts: first timeout kills + escalates a tier; the second
        triggers a bounded task split (≤4 subtasks, atomic dep rewiring).

        ``parked`` only sharpens the recorded reason text (the attempt was
        awaiting an approval that was never delivered or never decided). The
        ladder itself is identical: end_reason stays ``timeout``/``split``,
        the escalation and split semantics are the running path's."""
        task_id = str(task_row["id"])
        where = " while parked awaiting an undecided approval" if parked else ""
        try:
            self._sessions().request_kill(session_id, killed_by="swarm-timeout")
        except Exception:  # noqa: BLE001
            LOG.debug("request_kill failed for %s", session_id, exc_info=True)
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        timeout_count = int(swarm_json.get("timeout_count") or 0) + 1
        if timeout_count <= 1:
            self._dal.close_attempt(
                attempt_id, "timeout", f"first timeout at tier {tier}{where}; escalating"
            )
            self._merge_swarm_json(
                task_id,
                {
                    "timeout_count": timeout_count,
                    "current_tier": self._escalate(tier),
                    "feedback": [
                        *list(swarm_json.get("feedback") or []),
                        {
                            "source": "timeout",
                            "text": f"attempt timed out at tier {tier}{where}",
                        },
                    ],
                },
            )
            # Confirm termination before requeuing to prevent double-spend window
            # where the previous session is still alive and billing.
            self._await_session_terminal(session_id, timeout_seconds=10.0)

            return "requeue"
        self._dal.close_attempt(
            attempt_id, "split", f"second timeout at tier {tier}{where}; splitting task"
        )
        self._merge_swarm_json(task_id, {"timeout_count": timeout_count})
        return self._split_task(state, task_row, swarm_json)

    def _timeout_parked_attempt(
        self, state: _RunState, tasks: Sequence[Mapping[str, Any]], info: Mapping[str, Any]
    ) -> None:
        """Fire the tier ladder on an approval-parked attempt past its deadline.

        A parked attempt has no worker thread, so this reproduces exactly what
        ``_execute_item``'s ``finally`` does for a non-parked disposition: the
        task's scope locks are released (the park deliberately KEPT them), a
        ``requeue`` also releases the claim, and the run is woken so the task
        can be re-scheduled at its escalated tier.
        """
        task_id = str(info["task_id"])
        with state.cond:
            if state.parked.pop(task_id, None) is None:
                return  # resolved concurrently — the resolution path owns it
        task_row = next((dict(t) for t in tasks if str(t["id"]) == task_id), None)
        tier = str(info.get("tier") or "standard")
        disposition = "requeue"
        try:
            if task_row is None:
                # The task row vanished from the run (split/rewire): close the
                # attempt honestly and let the claim sweep pick up the rest.
                self._dal.close_attempt(
                    str(info["attempt_id"]),
                    "timeout",
                    f"attempt timed out at tier {tier} while parked; task no longer in run",
                )
            else:
                LOG.warning(
                    "run %s: parked attempt %s on task %s exceeded the %s tier timeout "
                    "with its approval undecided — firing the escalation ladder",
                    state.run_id,
                    info.get("attempt_id"),
                    task_id,
                    tier,
                )
                disposition = self._handle_timeout(
                    state,
                    task_row,
                    str(info["attempt_id"]),
                    str(info.get("session_id") or ""),
                    tier,
                    parked=True,
                )
        except Exception:  # noqa: BLE001 - the fallback pass must never die on one task
            LOG.exception("parked-attempt timeout failed for task %s", task_id)
        finally:
            with state.cond:
                state.last_progress = self._clock.monotonic()
            self._release_task_scope(state, task_id)
            if disposition == "requeue":
                try:
                    self._collab.release_claim(task_id)
                except Exception:  # noqa: BLE001
                    LOG.exception("could not release claim on %s", task_id)
            state.signals.put(("wake", task_id))

    # ------------------------------------------------------------------
    # Split budget reservation (plan-08 defect 4)
    # ------------------------------------------------------------------
    #
    # A split used to admit its children against the run cap with no
    # reservation whatsoever: GUARD 5 compared only ACCRUED spend, and every
    # spawned worker was handed the whole remaining run budget as its ceiling.
    # Neither number depends on how many live siblings a split has already put
    # in the field, so depth MULTIPLIED the effective budget -- a depth-2 split
    # of width 2 left four leaves each holding the entire root ceiling, and the
    # 4x overrun surfaced only after the money was gone.
    #
    # The rule below is deliberately narrow: a split may DIVIDE a ceiling but
    # never multiply one. Children collectively inherit exactly the entitlement
    # their parent held, which is the parent's own reservation when it carries
    # one and otherwise the run's remaining headroom -- precisely the ceiling
    # that parent would have been handed had it spawned instead of splitting.
    # A run-wide "sum of every live reservation vs the cap" test was rejected:
    # reservations are promises, not spend, so that form refuses an unrelated
    # sibling card's first legitimate split the moment any other card splits.

    def _split_entitlement(self, run: Mapping[str, Any], run_id: str, task_id: str) -> float | None:
        """The dollars a split of ``task_id`` may hand its children, or ``None``
        when the run carries no dollar cap (nothing to divide, nothing to
        refuse).

        A reservation is capped by the RUN's live headroom as well as by its own
        remainder. The two can diverge — sibling cards may overshoot their own
        slices, since a cap bounds an attempt rather than stopping it mid-flight
        — and when they do, a promise made when the money existed must not
        outlive the money. Taking the lower of the two means this guard is never
        weaker than the accrued-only policy it replaces, at any card, ever.

        May be NEGATIVE for an unreserved card on an over-spent run: that is the
        old ``accrued >= budget`` denial, preserved to the dollar.
        """
        budget_raw = run.get("budget_usd_max")
        if budget_raw is None:
            return None
        try:
            budget = float(budget_raw)
        except (TypeError, ValueError):  # pragma: no cover -- malformed run row
            return None
        headroom = budget - float(self._dal.budget_spend(run_id).accrued_cost_usd)
        reserved = self._dal.task_budget_reservation(task_id)
        if reserved is not None:
            return min(reserved, headroom)
        return headroom

    def _child_reservation(self, run_id: str, task_id: str, child_count: int) -> float | None:
        """Each admitted child's equal slice of its parent's entitlement, or
        ``None`` for an uncapped run (children are then stamped with nothing, so
        they keep today's unbounded behavior exactly)."""
        run = self._dal.get_run(run_id)
        if run is None:  # pragma: no cover -- the split path already read the run
            return None
        entitlement = self._split_entitlement(run, run_id, task_id)
        if entitlement is None:
            return None
        return max(0.0, entitlement) / max(1, int(child_count))

    @staticmethod
    def _attempt_budget_ceiling(
        budget: Any, spend: RunBudgetSpend | None, reserved: float | None
    ) -> float | None:
        """The dollar ceiling handed to ONE worker on its SpawnRequest.

        The run's remaining headroom — accrued, not known: tokens already
        measured on unpriced sessions are already spent, so handing the next
        worker headroom against them would re-issue money the fleet has burned.
        That headroom is then capped by the card's OWN unspent reservation when
        it holds one; without that cap N concurrent leaves of a split tree each
        carry the full root ceiling. An unreserved card (every root plan card,
        and every card in an uncapped run) is unaffected.
        """
        if budget is None:
            return None
        accrued = float(spend.accrued_cost_usd) if spend is not None else 0.0
        remaining = max(0.0, float(budget) - accrued)
        if reserved is None:
            return remaining
        return min(remaining, max(0.0, float(reserved)))

    def _split_task(
        self, state: _RunState, task_row: dict[str, Any], swarm_json: Mapping[str, Any]
    ) -> str:
        task_id = str(task_row["id"])
        specs = None
        try:
            specs = self._splitter(task_row, swarm_json)
        except Exception:  # noqa: BLE001
            LOG.exception("task splitter raised for %s", task_id)
        if not specs or len(specs) > 4:
            self._block_task(
                state, task_id, "split_failed", "task split unavailable after two timeouts"
            )
            return "blocked"
        result = self._provision_split(state, task_row, swarm_json, specs)
        # M6: the timeout-split TASK_SPLIT payload stays byte-identical — NO
        # "source" key (that key is added only on worker_request emits below).
        self._emit(
            state.run_id,
            ACTION_TASK_SPLIT,
            {
                "task_id": task_id,
                "subtask_ids": result["subtask_ids"],
                "rewired_dependents": result["rewired_dependents"],
            },
        )
        return "split"

    def _provision_split(
        self,
        state: _RunState,
        task_row: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        specs: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """The DURABLE, separable half of a split: build the child cards (each
        inheriting the parent's risk_class and a ``<parent_key>.<n>`` task_key)
        and commit them + inherit prerequisites + rewire dependents + close the
        parent + refresh the derived plan in the ONE atomic ``split_task``
        transaction. Emits NOTHING and closes no attempt — the caller owns event
        emission and attempt terminalization so the ordering can differ per
        source (the Fable splitter vs. a worker request). Returns
        ``split_task``'s result dict. Raises if the split transaction fails
        (callers decide the fallback).

        A split BUMPS the derived plan's version, so the projection is rebuilt
        BEFORE the transaction and the children are stamped with the post-bump
        ``plan_version``/``plan_hash`` — the lineage they were actually minted
        under. Stamping them from the parent's (pre-bump) swarm_json and bumping
        afterwards left every split child advertising a plan revision that no
        longer existed. When the projection cannot be rebuilt there is no bump
        to inherit, so the children keep the parent's lineage and plan_json is
        left alone."""
        task_id = str(task_row["id"])
        parent_key = str(swarm_json.get("task_key") or task_id)
        # Reserve BEFORE the transaction: the parent is still live here, so its
        # entitlement is readable, and the same transaction that commits these
        # children closes the parent ``cancelled`` — which releases the slice to
        # exactly the cards it was just divided among. No separate release write
        # exists to be lost in a crash.
        reservation = self._child_reservation(state.run_id, task_id, len(specs))
        cards: list[dict[str, Any]] = []
        for position, spec in enumerate(specs, start=1):
            sub_key = f"{parent_key}.{position}"
            child_json: dict[str, Any] = {
                "task_key": sub_key,
                "plan_version": swarm_json.get("plan_version"),
                "plan_hash": swarm_json.get("plan_hash"),
                "complexity": str(spec.get("complexity") or "simple"),
                "risk_class": swarm_json.get("risk_class", "none"),
                "est_agent_minutes": int(spec.get("est_agent_minutes") or 10),
                "owned_paths": list(spec.get("owned_paths") or []),
                "acceptance": str(spec.get("acceptance") or ""),
                "verify_command": str(spec.get("verify_command") or ""),
                "split_from": task_id,
            }
            if reservation is not None:
                child_json[BUDGET_RESERVED_KEY] = reservation
            cards.append(
                {
                    "title": str(spec.get("title") or f"{parent_key} part {position}"),
                    "description": str(spec.get("description") or ""),
                    "swarm_json": child_json,
                }
            )
        rebuilt = self._rebuild_plan_for_split(state, parent_key, cards)
        plan_json: str | None = None
        if rebuilt is not None:
            payload, plan_json = rebuilt
            for card in cards:
                card["swarm_json"]["plan_version"] = payload.get("version")
                card["swarm_json"]["plan_hash"] = payload.get("plan_hash")
        result = self._dal.split_task(state.run_id, task_id, cards, plan_json=plan_json)
        state.plan_dirty = True
        return result

    # ------------------------------------------------------------------
    # Worker-initiated fan-out: coordinator-validated subtasks_request.json
    # ------------------------------------------------------------------

    def _workbook_dir(self, run_id: str, task_id: str) -> Path | None:
        """The workbook directory for (run, task), derived from the SAME
        spawner/var_root the attempt actually ran under (single source of truth,
        B5) — the spawner exposes ``workbook_dir`` when it can; a spawner that
        does not (test fakes) falls back to the default var root."""
        getter = getattr(self._spawner, "workbook_dir", None)
        if callable(getter):
            try:
                return Path(getter(run_id, task_id))
            except Exception:  # noqa: BLE001 -- fall back to the default root.
                LOG.warning("spawner workbook_dir failed for %s", task_id, exc_info=True)
        try:
            from omniagentos.swarm.spawn import swarm_workbook_path

            return swarm_workbook_path(run_id, task_id).parent
        except Exception:  # noqa: BLE001 -- path resolution must never break a spawn/settle.
            LOG.warning("workbook dir resolution failed for %s", task_id, exc_info=True)
            return None

    def _subtasks_request_path(self, run_id: str, task_id: str, attempt_id: str) -> Path | None:
        """The ATTEMPT-BOUND request path (B1): ``subtasks_request.<attempt>.json``
        beside the workbook. Binding the filename to the attempt id means a
        SUCCESSOR attempt never consumes a predecessor's stale file — the gate
        reads only its own attempt's file and sweeps the rest."""
        workbook_dir = self._workbook_dir(run_id, task_id)
        if workbook_dir is None:
            return None
        return workbook_dir / f"subtasks_request.{attempt_id}.json"

    def _maybe_split_from_request(
        self,
        state: _RunState,
        task_row: Mapping[str, Any],
        attempt_id: str,
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> str | None:
        """Best-effort read + validation of THIS attempt's subtasks_request.json.

        Returns ``"split"`` only when the request passed every guard AND the
        children were durably registered — registration happens FIRST, and only
        on its success are the advisory events emitted and the attempt
        terminalized ``"split"`` (B2: an emit lost after a durable commit cannot
        corrupt state; a registration fault leaves no marker and no split end, so
        execution falls through to the normal review). Returns ``None`` in every
        other case (absent file → today's flow byte-identical; malformed / any
        guard denial → an ACTION_SUBTASKS_DENIED event, then review).
        """
        del session  # completion signal only; the request lives beside the workbook.
        task_id = str(task_row["id"])
        request_path = self._subtasks_request_path(state.run_id, task_id, attempt_id)
        if request_path is None:
            return None
        # B1: sweep any OTHER subtasks_request*.json (stale files from prior
        # attempts that never reached settlement) so nothing else is consumable.
        self._sweep_stale_requests(request_path)
        if not request_path.exists():
            return None  # No request for THIS attempt → today's flow is unchanged.

        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- unreadable/invalid JSON is a denial, not a crash.
            LOG.warning("subtasks_request.json unreadable for %s", task_id, exc_info=True)
            self._deny_subtasks(state, task_id, "malformed")
            return None

        run = self._dal.get_run(state.run_id) or {}
        deny_reason, specs, reason_head = self._validate_subtasks_payload(
            payload, swarm_json, str(task_id), run
        )
        if deny_reason is not None:
            self._deny_subtasks(state, task_id, deny_reason)
            return None

        # All guards passed. B2b: register DURABLY first (atomic split_task);
        # only on success emit the advisory events + terminalize the attempt.
        assert specs is not None  # exactly one of deny_reason/specs is non-None
        try:
            result = self._provision_split(state, task_row, swarm_json, specs)
        except Exception:  # noqa: BLE001 -- registration fault: no marker, no split end.
            LOG.exception("worker-requested split registration failed for %s", task_id)
            return None  # Durable state unchanged → fall through to normal review.
        self._emit(
            state.run_id,
            ACTION_SUBTASKS_REQUESTED,
            {"task_id": task_id, "count": len(specs), "reason": reason_head},
        )
        self._emit(
            state.run_id,
            ACTION_TASK_SPLIT,
            {
                "task_id": task_id,
                "subtask_ids": result["subtask_ids"],
                "rewired_dependents": result["rewired_dependents"],
                "source": "worker_request",
            },
        )
        self._dal.close_attempt(
            attempt_id, "split", f"worker-requested split into {len(specs)} subtasks"
        )
        return "split"

    def _maybe_grant_insession(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session: dict[str, Any],
    ) -> bool:
        """Live half of PKG-INSESSION-FANOUT: while the attempt is RUNNING,
        answer its subtasks_request.json with a durable, capacity-checked
        grant. Returns True when this attempt needs no further scanning
        (feature off / non-claude / decided), False to keep watching.

        Decides AT MOST once per attempt — a denial (guards or agent
        capacity) is final for this attempt; the worker's bounded grant wait
        times out and today's settle-time flow proceeds byte-identically.
        Every failure path fails CLOSED (no grant), never crashes the await
        loop."""
        task_id = str(task_row["id"])
        try:
            if not (self._subtask_requests_enabled() and self._insession_enabled()):
                return True
            if str(session.get("provider") or "") != "claude":
                return True
            request_path = self._subtasks_request_path(state.run_id, task_id, attempt_id)
            if request_path is None:
                return True
            if not request_path.exists():
                return False  # nothing yet — keep watching

            from omniagentos.swarm.insession import (
                create_grant,
                grant_path_for_request,
                void_grant_for_attempt,
            )

            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 -- unreadable/invalid JSON is a denial, not a crash.
                LOG.warning("subtasks_request.json unreadable for %s", task_id, exc_info=True)
                self._deny_subtasks(state, task_id, "malformed", mode="insession")
                return True
            swarm_json = self._dal.get_swarm_json(task_id) or {}
            run = self._dal.get_run(state.run_id) or {}
            deny_reason, specs, reason_head = self._validate_subtasks_payload(
                payload, swarm_json, task_id, run
            )
            if deny_reason is not None:
                self._deny_subtasks(state, task_id, deny_reason, mode="insession")
                return True
            assert specs is not None
            grant, grant_deny = create_grant(
                swarm_run_id=state.run_id,
                board_task_id=task_id,
                attempt_id=attempt_id,
                session_id=str(session.get("id") or ""),
                provider="claude",
                account_id=(str(session.get("account_id")) if session.get("account_id") else None),
                children=specs,
            )
            if grant is None:
                self._deny_subtasks(state, task_id, grant_deny or "grant_failed", mode="insession")
                return True
            # The grant row is durable and holding capacity BEFORE the worker
            # can see the file; a failed file write voids it (an authorization
            # the worker cannot read must not keep slots committed).
            grant_file = Path(grant_path_for_request(str(request_path)))
            try:
                tmp = grant_file.with_name(grant_file.name + ".tmp")
                tmp.write_text(
                    json.dumps(
                        {
                            "grant_id": grant.id,
                            "attempt_id": attempt_id,
                            "max_children": grant.max_children,
                            "expires_at": grant.expires_at,
                            "children": list(grant.children),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                os.replace(tmp, grant_file)
            except Exception:  # noqa: BLE001
                LOG.exception("grant file write failed for %s", task_id)
                void_grant_for_attempt(attempt_id, "grant_write_failed")
                self._deny_subtasks(state, task_id, "grant_write_failed", mode="insession")
                return True
            self._emit(
                state.run_id,
                ACTION_SUBTASKS_GRANTED,
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "grant_id": grant.id,
                    "count": len(grant.children),
                    "reason": reason_head,
                },
            )
            LOG.info(
                "in-session fan-out granted for %s (%d children)",
                task_id,
                len(grant.children),
            )
            return True
        except Exception:  # noqa: BLE001 -- the await loop must never die on the grant path.
            LOG.exception("insession grant scan failed for %s", task_id)
            return True

    def _insession_grant_consumed(self, attempt_id: str) -> bool:
        """True iff THIS attempt's grant spawned ≥1 in-session child — the
        settle path must then SKIP the card-split (the children's work already
        happened inside this attempt; registering them would schedule the same
        work twice). Voids the grant either way (settle is the accounting
        release point). Fails to False: a read failure falls back to today's
        flow rather than suppressing it."""
        try:
            from omniagentos.swarm.insession import (
                grant_for_attempt,
                void_grant_for_attempt,
            )

            grant = grant_for_attempt(attempt_id)
            if grant is None:
                return False
            consumed = grant.children_spawned > 0
            void_grant_for_attempt(
                attempt_id, "settled_consumed" if consumed else "settled_unconsumed"
            )
            return consumed
        except Exception:  # noqa: BLE001
            LOG.warning("insession grant read failed for %s", attempt_id, exc_info=True)
            return False

    def _validate_subtasks_payload(
        self,
        payload: Any,
        swarm_json: Mapping[str, Any],
        task_id: str,
        run: Mapping[str, Any],
    ) -> tuple[str | None, list[dict[str, Any]] | None, str]:
        """The six PKG-REQUEST-SUBTASKS guards, shared VERBATIM by the
        settle-time split and the live in-session grant path
        (PKG-INSESSION-FANOUT) — one validator, so the two admission decisions
        can never drift. Returns ``(deny_reason, specs, reason_head)``;
        exactly one of deny_reason/specs is non-None, and specs carry the
        NORMALIZED per-child owned paths (what gets registered or granted)."""
        subtasks = payload.get("subtasks") if isinstance(payload, Mapping) else None
        if not isinstance(payload, Mapping) or not self._subtasks_shape_ok(subtasks):
            return "malformed", None, ""
        assert isinstance(subtasks, list)  # _subtasks_shape_ok just proved it
        reason_head = str(payload.get("reason") or "")[:200]
        # GUARD 1 — count: a genuine fan-out is 2-4 independent subtasks.
        if not 2 <= len(subtasks) <= 4:
            return "count", None, reason_head
        # GUARD 2 — depth: ONE level of worker-initiated splitting only; a child
        # task_key already carries a "." (e.g. "parser.1") and may not re-split.
        parent_key = str(swarm_json.get("task_key") or task_id)
        if "." in parent_key:
            return "depth", None, reason_head
        # GUARD 3 — once: only a REGISTERED split is once-only, and split_task's
        # transaction is the durable record of that (it merges {"split": true}
        # into the parent). Reading THAT (not a separate best-effort marker)
        # closes the crash window (B2a). A denial writes no marker, so a later
        # attempt may legitimately re-request.
        if swarm_json.get("split"):
            return "repeat", None, reason_head
        # GUARD 4 — risk: reuse the dispatch gate's risk vocabulary in STRICT
        # mode over the CONCATENATED subtask titles+descriptions plus the request
        # reason; fail CLOSED (import/compile/scan failure denies, never lets
        # risky or evasive text fan out silently) (B4).
        try:
            from omniagentos.dispatch.gate import text_hits_risk

            scan_text = " ".join(
                [
                    reason_head,
                    *(
                        f"{item.get('title') or ''} {item.get('description') or ''}"
                        for item in subtasks
                    ),
                ]
            )
            risky = text_hits_risk(scan_text, strict=True)
        except Exception:  # noqa: BLE001 -- import/compile/scan failure → DENY, never open.
            LOG.warning("risk scan unavailable; denying subtasks for %s", task_id, exc_info=True)
            return "risk_check_unavailable", None, reason_head
        if risky:
            return "risk", None, reason_head
        # GUARD 5 — budget: admission must weigh the children's PROJECTED cost,
        # not merely observe money already burned. The entitlement below is what
        # THIS card may still hand out: its own unspent reservation when it
        # carries one (a split child re-splitting divides what is left of its
        # slice, never the run's whole remaining budget), and otherwise the live
        # run headroom — which reduces this guard EXACTLY to its former
        # ``accrued >= budget`` policy for every unreserved card.
        entitlement = self._split_entitlement(run, str(run.get("id") or ""), task_id)
        if entitlement is not None and entitlement <= 0.0:
            return "budget", None, reason_head
        # GUARD 6 — ownership (B3): every subtask owns a non-empty set of
        # normalized, workspace-relative paths CONTAINED (by path component
        # prefix) within the parent's owned paths, pairwise DISJOINT across all
        # children (no duplicates, no ancestor/descendant overlap). The
        # NORMALIZED paths are what gets registered.
        parent_owned = [str(p) for p in (swarm_json.get("owned_paths") or [])]
        normalized = self._validated_subtask_paths(subtasks, parent_owned)
        if normalized is None:
            return "ownership", None, reason_head
        specs = [
            {
                "title": str(item.get("title") or ""),
                "description": str(item.get("description") or ""),
                "owned_paths": owned,
                "est_agent_minutes": item.get("est_minutes"),
            }
            for item, owned in zip(subtasks, normalized, strict=True)
        ]
        return None, specs, reason_head

    def _sweep_stale_requests(self, current: Path) -> None:
        """Delete every ``subtasks_request*.json`` in the dir EXCEPT the current
        attempt's file (B1) — stale per-attempt files a predecessor wrote before
        timing out/rate-limiting must never be consumed by a successor."""
        try:
            for other in current.parent.glob("subtasks_request*.json"):
                if other != current:
                    other.unlink(missing_ok=True)
                    LOG.info("swept stale subtasks request %s", other)
        except Exception:  # noqa: BLE001 -- sweeping is best-effort hygiene.
            LOG.debug("stale subtasks-request sweep failed", exc_info=True)

    def _deny_subtasks(
        self, state: _RunState, task_id: str, reason: str, *, mode: str = "settle"
    ) -> None:
        """Emit ACTION_SUBTASKS_DENIED with the guard reason and return ``None``
        so the caller falls through to the normal review flow. A denial writes NO
        durable marker (B2c) — a later attempt may legitimately re-request, and
        the only once-only fact (a registered split) is the split_task
        transaction's own record.

        ``mode="insession"`` (PKG-INSESSION-FANOUT) marks a LIVE grant denial;
        the settle-path payload stays byte-identical (no ``mode`` key)."""
        payload: dict[str, Any] = {"task_id": task_id, "reason": reason}
        if mode != "settle":
            payload["mode"] = mode
        self._emit(state.run_id, ACTION_SUBTASKS_DENIED, payload)
        LOG.info("worker subtask request denied for %s (%s): %s", task_id, mode, reason)
        return None

    @staticmethod
    def _subtasks_shape_ok(subtasks: Any) -> bool:
        """Structural schema check (a failure denies as ``"malformed"``): a list
        of objects each with a non-empty string ``title``, a string
        ``description``, a list-of-strings ``owned_paths``, and — if present — an
        integer ``est_minutes``. Count and ownership are separate GUARDS."""
        if not isinstance(subtasks, list):
            return False
        for item in subtasks:
            if not isinstance(item, Mapping):
                return False
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                return False
            if not isinstance(item.get("description"), str):
                return False
            owned = item.get("owned_paths")
            if not isinstance(owned, list) or not all(isinstance(p, str) for p in owned):
                return False
            est = item.get("est_minutes")
            if est is not None and not isinstance(est, int):
                return False
        return True

    @staticmethod
    def _norm_rel_path(raw: str) -> str | None:
        """Normalize one owned path to a workspace-relative posix path, or
        ``None`` if it is unsafe (B3): backslashes folded, absolute rejected,
        ``posixpath.normpath`` applied, and any path that normalizes to ``""``,
        ``.``, ``..`` or escapes upward (``../...``) rejected. Proper
        normalization — never ``lstrip("./")``, which mangles dotfiles like
        ``.github/...``."""
        candidate = str(raw).strip().replace("\\", "/")
        if not candidate or posixpath.isabs(candidate):
            return None
        normalized = posixpath.normpath(candidate)
        if normalized in ("", ".", "..") or normalized.startswith("../"):
            return None
        return normalized

    @staticmethod
    def _path_components(normalized: str) -> list[str]:
        return normalized.split("/")

    @classmethod
    def _contained_in(cls, child: str, parents: Sequence[str], parent_all: bool) -> bool:
        """True iff ``child`` is inside one of ``parents`` by path-COMPONENT
        prefix (never string ops, so ``github/...`` is NOT inside
        ``.github/...``). ``parent_all`` means a parent owns the whole workspace
        (``.``)."""
        if parent_all:
            return True
        cc = cls._path_components(child)
        for parent in parents:
            pc = cls._path_components(parent)
            if cc[: len(pc)] == pc:
                return True
        return False

    @classmethod
    def _paths_overlap(cls, a: str, b: str) -> bool:
        """True iff ``a`` and ``b`` are equal or one is an ancestor of the other
        (component-prefix), i.e. NOT disjoint."""
        ac, bc = cls._path_components(a), cls._path_components(b)
        shorter = min(len(ac), len(bc))
        return ac[:shorter] == bc[:shorter]

    def _validated_subtask_paths(
        self, subtasks: Sequence[Mapping[str, Any]], parent_owned: Sequence[str]
    ) -> list[list[str]] | None:
        """Validate + NORMALIZE every subtask's owned_paths (B3). Returns the
        normalized per-subtask path lists (to be REGISTERED), or ``None`` if any
        rule fails: empty ownership, an unsafe/absolute/traversal path, a path
        not contained within the parent's owned paths, or any pairwise overlap
        (duplicate or ancestor/descendant) across ALL children."""
        parent_norms: list[str] = []
        parent_all = False
        for raw in parent_owned:
            candidate = str(raw).strip().replace("\\", "/")
            if candidate in ("", ".", "./") or posixpath.normpath(candidate) == ".":
                parent_all = True
                continue
            normalized = self._norm_rel_path(raw)
            if normalized is not None:
                parent_norms.append(normalized)
        per_task: list[list[str]] = []
        for item in subtasks:
            owned_raw = item.get("owned_paths") or []
            if not owned_raw:
                return None  # non-empty ownership required
            norms: list[str] = []
            for raw in owned_raw:
                normalized = self._norm_rel_path(raw)
                if normalized is None:
                    return None
                if not self._contained_in(normalized, parent_norms, parent_all):
                    return None
                norms.append(normalized)
            per_task.append(norms)
        flat = [path for norms in per_task for path in norms]
        for i in range(len(flat)):
            for j in range(i + 1, len(flat)):
                if self._paths_overlap(flat[i], flat[j]):
                    return None  # duplicate or ancestor/descendant overlap
        return per_task

    def _rebuild_plan_for_split(
        self,
        state: _RunState,
        parent_key: str,
        cards: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str] | None:
        """Derived-plan upkeep: swap the parent spec for the subtask specs and
        bump the plan version. Board tables are the machine truth (rewired in
        ONE transaction by ``split_task``); plan_json is the projection PLAN.md
        renders from, and ``split_task`` now writes it in that same transaction
        so the children's stamped lineage and the bump can never disagree.

        Returns ``(payload, serialized)`` — the rebuilt projection and the exact
        bytes to store — or ``None`` when it could not be rebuilt. Rebuilding is
        best-effort by design and NOTHING here may fail the split, so the
        serialization happens here too: a projection that cannot be dumped is a
        ``None``, never an exception raised inside the split transaction."""
        try:
            run = self._dal.get_run(state.run_id)
            if run is None:
                return None
            payload = json.loads(run.get("plan_json") or "{}")
            plan = SwarmPlan.model_validate(payload)
            parent_spec = next((t for t in plan.tasks if t.id == parent_key), None)
            if parent_spec is None:
                return None
            from omniagentos.swarm.contracts import SwarmTaskSpec

            new_specs = []
            for card in cards:
                swarm_json = card["swarm_json"]
                new_specs.append(
                    SwarmTaskSpec(
                        id=str(swarm_json["task_key"]),
                        title=str(card["title"]),
                        description=str(card["description"]),
                        depends_on=list(parent_spec.depends_on),
                        complexity=str(swarm_json.get("complexity") or "simple"),
                        est_agent_minutes=int(swarm_json.get("est_agent_minutes") or 10),
                        owned_paths=list(swarm_json.get("owned_paths") or []),
                        acceptance=str(swarm_json.get("acceptance") or ""),
                        verify_command=str(swarm_json.get("verify_command") or ""),
                    )
                )
            sub_keys = [spec.id for spec in new_specs]
            for spec in plan.tasks:
                if parent_key in spec.depends_on:
                    spec.depends_on = [
                        *(d for d in spec.depends_on if d != parent_key),
                        *sub_keys,
                    ]
            plan.tasks = [t for t in plan.tasks if t.id != parent_key] + new_specs
            plan.version += 1
            from omniagentos.swarm.planner import plan_payload

            rebuilt = plan_payload(plan)
            # SwarmPlan.model_validate drops extra plan_json keys (pydantic);
            # 'params' carries the run-level speed/priority/pins record (P6)
            # and must survive a mid-run split.
            if isinstance(payload.get("params"), dict):
                rebuilt["params"] = payload["params"]
            # Same encoding SwarmDal.set_plan uses — this replaces that write.
            return rebuilt, json.dumps(rebuilt, separators=(",", ":"), sort_keys=True)
        except Exception:  # noqa: BLE001 -- projection upkeep must not fail the split.
            LOG.warning("could not rebuild plan_json for split", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Terminal settlement: ownership diff → mechanical gate → review
    # ------------------------------------------------------------------

    def _settle_terminal(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session: dict[str, Any],
        tier: str,
        snapshot_sha: str,
    ) -> str:
        task_id = str(task_row["id"])
        swarm_json = self._dal.get_swarm_json(task_id) or {}

        # Cost accumulates on EVERY priced terminal, success or not. Missing
        # cost remains missing; the live ledger makes that an explicit budget
        # control issue instead of adding a counterfeit $0.00.
        cost_raw = session.get("cost_usd")
        if cost_raw is not None:
            cost = float(cost_raw)
            if cost > 0:
                self._dal.add_cost(state.run_id, cost)
        run = self._dal.get_run(state.run_id) or {}
        budget_issue = self._budget_issue(run)
        if budget_issue is not None:
            state.signals.put(("budget", budget_issue))

        # Late completion after ANY external run terminalization (cancel /
        # failed / completed stamped outside this process): recorded, then
        # ignored. Pre-fix only checked ``state.cancelled``, so an external
        # ``failed``/``completed`` stamp still ran the quality gate against a
        # dying fleet — H-19 requires the same settle short-circuit for every
        # terminal reason.
        #
        # L16 settle-handoff (TaskContract / CBM close on settle) remains
        # SEPARATE: it requires ``cbm_wiring`` + ``contract_bridge`` modules
        # that are L16 exclusive production paths, not plain-data interfaces
        # wholly inside this file. L10 must not import L16-owned surfaces.
        if state.cancelled or state.terminalizing:
            reason = state.terminal_reason or ("cancelled" if state.cancelled else "terminalized")
            self._dal.close_attempt(
                attempt_id,
                "killed",
                f"late terminal after run {reason} — recorded and ignored",
            )
            return "cancelled"

        outcome = self._classifier(session)

        if outcome == "rate_limited":
            return self._handle_rate_limited(state, task_row, attempt_id, session, swarm_json)

        if outcome in ("crashed", "killed", "auth_failed"):
            reason = "auth_failed" if outcome == "auth_failed" else "crashed"
            detail = str(session.get("error") or f"session ended {outcome}")
            self._dal.close_attempt(attempt_id, reason, detail[:500])
            return self._mechanical_failure(state, task_id, tier, detail)

        # outcome == completed → the quality gate.
        attempt: dict[str, Any] = next(
            (a for a in self._dal.list_attempts(task_id) if str(a["id"]) == attempt_id),
            {},
        )
        return self._quality_gate(
            state, task_row, attempt_id, dict(attempt), session, swarm_json, tier, snapshot_sha
        )

    def _handle_rate_limited(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session: dict[str, Any],
        swarm_json: Mapping[str, Any],
    ) -> str:
        task_id = str(task_row["id"])
        attempt: dict[str, Any] = next(
            (a for a in self._dal.list_attempts(task_id) if str(a["id"]) == attempt_id),
            {},
        )
        provider = str(attempt.get("provider") or "claude")
        account_id = attempt.get("account_id")
        detail = str(session.get("error") or "rate limited")
        self._dal.close_attempt(attempt_id, "rate_limited", detail[:500])
        try:
            self._limits.report_rate_limited(
                provider, account_id, detail, session.get("rate_limit_reset_at")
            )
        except Exception:  # noqa: BLE001
            LOG.warning("rate-limit report failed", exc_info=True)
        self._emit(
            state.run_id,
            ACTION_RATE_LIMIT,
            {"task_id": task_id, "provider": provider, "detail": detail[:200]},
        )
        self._emit(
            state.run_id,
            ACTION_PROVIDER_SWITCHED,
            {"task_id": task_id, "from_provider": provider, "reason": "rate_limited"},
        )
        requeues = int(swarm_json.get("rate_limit_requeues") or 0) + 1
        self._merge_swarm_json(task_id, {"rate_limit_requeues": requeues})
        if requeues > self._rate_limit_requeue_cap:
            self._block_task(
                state,
                task_id,
                "rate_limit_flapping",
                f"{requeues} rate-limit re-enqueues exceed the cap "
                f"({self._rate_limit_requeue_cap})",
            )
            return "blocked"
        # Re-enqueue WITHOUT consuming a retry.
        return "requeue"

    def _quality_gate(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        attempt: dict[str, Any],
        session: dict[str, Any],
        swarm_json: dict[str, Any],
        tier: str,
        snapshot_sha: str,
    ) -> str:
        task_id = str(task_row["id"])
        owned_paths = [str(p) for p in (swarm_json.get("owned_paths") or [])]
        flags: list[str] = []

        # Phase-2 worktree mode: the task settles against ITS OWN worktree —
        # ownership diff, revert, verify, and review all read the worktree;
        # the merge into the main workspace happens only on CONFIRM.
        worktree_path = str(swarm_json.get("worktree_path") or "")
        worktree_branch = str(swarm_json.get("worktree_branch") or "")
        in_worktree = bool(worktree_path and worktree_branch)
        if in_worktree and not os.path.isdir(worktree_path):
            self._dal.close_attempt(attempt_id, "crashed", "task worktree missing at settle")
            return self._mechanical_failure(
                state, task_id, tier, f"task worktree vanished: {worktree_path}"
            )
        task_dir = worktree_path if in_worktree else state.working_dir
        if in_worktree:
            diff_base = str(swarm_json.get("worktree_base_sha") or "") or snapshot_sha
        else:
            diff_base = snapshot_sha

        # Post-terminal ownership diff. Worktree mode diffs the worktree's
        # CUMULATIVE delta vs the branch base (committed + uncommitted +
        # untracked — workers commit freely, so session files_json or a
        # HEAD-only delta would under-report). Phase 1 keeps session-reported
        # files first with the uncommitted git delta as the fallback signal.
        # Violations revert from the base/snapshot and flag the review.
        #
        # H-04: observation failure MUST block confirmation. Substituting an
        # empty change set fails open — a worker that touched another task's
        # files would be confirmed. Never do that.
        observation_error: str | None = None
        touched: list[str] = []
        if in_worktree:
            try:
                observed = self._worktrees().changed_paths_since(worktree_path, diff_base)
                touched = list(observed or [])
            except Exception as exc:  # noqa: BLE001
                LOG.warning("worktree changed_paths_since failed", exc_info=True)
                observation_error = f"worktree ownership observation failed: {exc}"
        else:
            session_files = self._session_files(session)
            if session_files is not None:
                touched = list(session_files)
            else:
                try:
                    observed = self._git.changed_paths(state.working_dir)
                    touched = list(observed or [])
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("changed_paths failed", exc_info=True)
                    observation_error = f"git ownership observation failed: {exc}"
        if observation_error is not None:
            self._dal.close_attempt(attempt_id, "crashed", observation_error[:500])
            return self._mechanical_failure(state, task_id, tier, observation_error)
        pre_attempt_dirty = (
            set()
            if in_worktree
            else {str(path) for path in (swarm_json.get("pre_attempt_dirty_paths") or [])}
        )
        worker_touched = [path for path in touched if path not in pre_attempt_dirty]
        violations = [
            path
            for path in worker_touched
            if path not in _COORDINATOR_FILES and not self._path_owned(path, owned_paths)
        ]
        if violations:
            if diff_base:
                try:
                    with self._git_guard(state, task_dir, "ownership-revert"):
                        self._git.revert_paths(task_dir, diff_base, violations)
                        if in_worktree:
                            # Persist the revert ON THE BRANCH: committed
                            # out-of-scope changes must not survive to merge.
                            self._git.commit_paths(
                                task_dir,
                                violations,
                                f"swarm {state.run_id}: revert out-of-scope paths",
                            )
                except Exception:  # noqa: BLE001
                    LOG.exception("snapshot revert failed for %s", task_id)
            flags.append(
                "ownership violation: out-of-scope changes reverted from the "
                f"pre-attempt snapshot: {', '.join(violations[:20])}"
            )
            # P0.2: if the revert removed EVERYTHING substantive, the attempt
            # produced no in-scope work. Continuing would run the suite against
            # an unchanged tree and hand a vacuous green to the reviewer — the
            # exact way a correct fix was silently discarded. Fail it instead.
            remaining_in_scope = [
                path
                for path in worker_touched
                if path not in violations and path not in _COORDINATOR_FILES
            ]
            if not remaining_in_scope:
                return self._mechanical_failure(
                    state,
                    task_id,
                    tier,
                    "ownership revert removed every change: the attempt left no "
                    "in-scope diff, so its verification would pass against an "
                    "unchanged tree. Declare the correct owned paths "
                    f"(reverted: {', '.join(violations[:10])})",
                )
            # OmniAgentOS Phase 1.4: post_attempt + G5 change the next action
            # (not only annotate flags). Ownership was already reverted above.
            try:
                from omniagentos.execution.post_attempt import evaluate_post_attempt
                from omniagentos.gates.service import GateService

                owned = swarm_json.get("owned_paths") or []
                if not isinstance(owned, (list, tuple)):
                    owned = []
                post = evaluate_post_attempt(
                    working_dir=task_dir,
                    undeclared_paths=violations,
                    declared_scope=[str(p) for p in owned],
                    expected_files=[str(p) for p in owned],
                    lane="swarm",
                    holder_id=task_id,
                    # Undeclared already known; still pass owned scope for assess.
                    use_verify=False,
                )
                if post.violation_actions:
                    flags.append("violation_plan:" + ",".join(post.violation_actions[:8]))
                g5 = GateService().g5_local_verify(
                    {
                        "verify_ok": False,
                        "mechanical_pass": post.mechanical_pass,
                        "undeclared_paths": list(violations),
                        "assess_verdict": post.assess_verdict,
                    }
                )
                # Ownership was ALREADY reverted above. Recoverable scope
                # (flag/notify/revert) proceeds to review with flags so the
                # reviewer sees "ownership violation: …". Only hard-stop when
                # assess/gate demands a non-recoverable park (not mere flagging).
                non_recoverable = any(
                    a in post.violation_actions
                    for a in ("block_tier_p", "kill_session", "park_run", "fail_task")
                )
                assess_hard = str(post.assess_verdict or "").lower() in {
                    "fail",
                    "failed",
                    "park",
                    "blocked",
                    "deny",
                }
                if non_recoverable and (g5.decision == "deny" or assess_hard):
                    self._dal.close_attempt(
                        attempt_id,
                        "blocked",
                        f"post_attempt+g5 deny: {post.assess_verdict} "
                        f"actions={','.join(post.violation_actions[:6])}",
                    )
                    return self._mechanical_failure(
                        state,
                        task_id,
                        tier,
                        f"scope/verify gate blocked task: {post.assess_verdict}",
                    )
                # Escalation path: non-pass assess with retry-ish actions
                if not post.mechanical_pass and any(
                    a in post.violation_actions for a in ("notify_operator", "flag_scope_violation")
                ):
                    flags.append("post_attempt:escalate_or_review")
            except Exception:  # noqa: BLE001 — quality gate must not die on helper
                LOG.warning("post_attempt evaluation failed", exc_info=True)

        # m8: coordinator-owned files (PLAN.md) are exempt from the ownership
        # VIOLATIONS above, but in worktree mode a worker-committed edit to
        # them would still land in main at merge time (or manufacture a
        # spurious conflict with the coordinator's own PLAN.md writes) —
        # revert them from the branch base and persist the revert on the
        # branch, the same idiom as ownership violations.
        if in_worktree and diff_base:
            coordinator_edits = sorted(path for path in touched if path in _COORDINATOR_FILES)
            if coordinator_edits:
                try:
                    with state.git_lock:
                        self._git.revert_paths(task_dir, diff_base, coordinator_edits)
                        self._git.commit_paths(
                            task_dir,
                            coordinator_edits,
                            f"swarm {state.run_id}: revert coordinator-owned files",
                        )
                except Exception:  # noqa: BLE001
                    LOG.exception("coordinator-file revert failed for %s", task_id)
                flags.append(
                    "coordinator-file edits reverted (these files are "
                    f"coordinator-owned): {', '.join(coordinator_edits)}"
                )

        # M3: persist the still-uncommitted owned work on the branch NOW and
        # capture the EXACT sha this gate diffs/verifies/reviews — CONFIRM
        # merges that sha (never the branch ref), so a branch ref tampered by
        # a sandboxed same-run sibling between gate and merge can no longer
        # change what lands in main.
        verified_sha: str | None = None
        if in_worktree:
            task_key = str(swarm_json.get("task_key") or task_id)
            try:
                with state.git_lock:
                    if owned_paths:
                        self._git.commit_paths(
                            task_dir,
                            owned_paths,
                            f"swarm {state.run_id}: task {task_key} work",
                        )
                    verified_sha = self._worktrees().head_sha(task_dir)
            except Exception as exc:  # noqa: BLE001 -- infra failure, not the worker's fault.
                LOG.exception("could not persist worktree work for %s", task_id)
                self._dal.close_attempt(
                    attempt_id, "crashed", f"could not persist worktree work: {exc}"
                )
                return self._mechanical_failure(
                    state, task_id, tier, f"could not persist worktree work: {exc}"
                )

        # Phase 1.4: verify_working_tree on owned paths (clean path — no undeclared
        # pre-fill so the verify branch actually runs). Failures feed G5 below.
        try:
            from omniagentos.execution.post_attempt import evaluate_post_attempt

            post_clean = evaluate_post_attempt(
                working_dir=task_dir,
                declared_scope=list(owned_paths),
                expected_files=list(owned_paths),
                lane="swarm",
                holder_id=task_id,
                use_verify=True,
            )
            if not post_clean.mechanical_pass:
                flags.append("post_attempt:verify:" + (post_clean.assess_verdict or "needs_review"))
                if any(
                    a in post_clean.violation_actions
                    for a in ("block_tier_p", "kill_session", "park_run", "fail_task")
                ):
                    self._dal.close_attempt(
                        attempt_id,
                        "blocked",
                        f"post_attempt verify blocked: {post_clean.assess_verdict}",
                    )
                    return self._mechanical_failure(
                        state,
                        task_id,
                        tier,
                        f"scope/verify blocked: {post_clean.assess_verdict}",
                    )
        except Exception:  # noqa: BLE001
            LOG.warning("post_attempt verify path failed", exc_info=True)

        # Mechanical gate: failing verify_command or empty output gets ONE
        # same-tier retry with the error fed back, before any escalation.
        verify_ok, verify_output = True, ""
        try:
            verify_ok, verify_output = self._verifier(task_row, swarm_json, task_dir)
        except Exception as exc:  # noqa: BLE001
            verify_ok, verify_output = False, f"verifier crashed: {exc}"
        # Phase 1.3: G5 decides on mechanical verify evidence (deny ≠ silent allow).
        g5_degraded: str | None = None
        try:
            from omniagentos.gates.service import GateService

            g5 = GateService().g5_local_verify(
                {
                    "verify_ok": verify_ok,
                    "mechanical_pass": verify_ok,
                    "verify_output": (verify_output or "")[:300],
                }
            )
            if g5.decision == "deny":
                verify_ok = False
                verify_output = (
                    f"g5_local_verify denied: {g5.evidence.get('reason') or 'failed'} "
                    f"| {verify_output}"
                )[:500]
        except Exception as exc:  # noqa: BLE001
            # G5 itself failed. Posture is unchanged — the mechanical verdict stands —
            # but the degradation is recorded so a gate that never ran is queryable
            # after the fact instead of being a log line nobody reads.
            LOG.warning("g5_local_verify failed open to mechanical path", exc_info=True)
            g5_degraded = f"{type(exc).__name__}: {exc}"
            from omniagentos.swarm.contracts import ACTION_GATE_DEGRADED

            self._emit(
                state.run_id,
                ACTION_GATE_DEGRADED,
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "gate": "g5_local_verify",
                    "reason": g5_degraded[:300],
                    "mechanical_verdict": "pass" if verify_ok else "fail",
                },
            )
        produced_nothing = not str(session.get("output_text") or "").strip() and not worker_touched
        if not verify_ok or produced_nothing:
            detail = (
                f"verify_command failed: {verify_output[:500]}"
                if not verify_ok
                else "attempt produced no output and changed no files"
            )
            if g5_degraded:
                detail = f"{detail} | g5_degraded: {g5_degraded}"
            self._dal.close_attempt(attempt_id, "review_denied", detail[:500])
            return self._mechanical_failure(state, task_id, tier, detail)

        # Worker-initiated fan-out (PKG-REQUEST-SUBTASKS; config-gated, no-hidden-
        # children invariant intact). A worker that found its task decomposes
        # wrote a structured subtasks_request.json beside its workbook; the
        # coordinator validates it and, on ALL guards passing, registers the
        # children via the SAME atomic split machinery the timeout path uses —
        # the parent attempt then terminalizes with the "split" end_reason and
        # review is SKIPPED, exactly as the timeout-split path skips it. A missing
        # file, a malformed one, or any guard denial leaves today's review flow
        # untouched (a denial is emitted, then execution falls through to review).
        if self._subtask_requests_enabled():
            # PKG-INSESSION-FANOUT: a CONSUMED grant means the children already
            # executed INSIDE this attempt — a card-split now would schedule
            # the same work twice, so the parent proceeds through verify/review
            # as the single unit it is. An unconsumed grant is voided and
            # today's flow runs unchanged.
            if not self._insession_grant_consumed(attempt_id):
                split = self._maybe_split_from_request(
                    state, task_row, attempt_id, swarm_json, session
                )
                if split is not None:
                    return split

        # Bind review to the ACTUAL attempt model recorded before spawn, not to
        # a formation preference or a display label.  Assignment failures are
        # deterministic policy refusals: block immediately, do not warn, call
        # an adapter, or retry the same forbidden pair as though it were an
        # infrastructure outage.
        from omniagentos.formation.lineage import (
            LineageAssignmentError,
            assign_reviewer,
            lineage_for_model,
        )

        review_swarm_json = dict(swarm_json)
        implementer_model = str(attempt.get("model") or "")
        # Identify the implementer by what ACTUALLY RAN. An unregistered model name
        # would otherwise refuse and hard-block the task — measured in the
        # simharness, where the stub model `stub-simple` produced
        # `seq=0 crashed -> seq=1 blocked` and failed a recoverable run. The
        # provider is the harness that executed the work, so it is the more
        # reliable lineage signal; `lineage_for_execution` still refuses when
        # neither model nor provider is known.
        implementer_provider = str(attempt.get("provider") or "")
        # Test the MODEL ALONE. `lineage_for_execution` succeeds via the provider
        # fallback, so testing it here would keep the unresolvable model name and
        # push the same refusal one layer down into assign_reviewer.
        # Test the MODEL ALONE. `lineage_for_execution` succeeds via the provider
        # fallback, so testing it here would keep the unresolvable model name and
        # push the same refusal one layer down into assign_reviewer.
        #
        # Only substitute the provider when the provider is itself resolvable. If
        # neither is known we keep the MODEL as the identifier, so the refusal names
        # what the operator actually configured — "unknown lineage for
        # 'some-new-model-9'" is actionable; "for 'future-provider'" sends them
        # looking in the wrong place.
        try:
            lineage_for_model(implementer_model)
            implementer_ident = implementer_model
        except LineageAssignmentError:
            implementer_ident = implementer_model
            if implementer_provider:
                try:
                    lineage_for_model(implementer_provider)
                    implementer_ident = implementer_provider
                except LineageAssignmentError:
                    pass
        reviewer_candidate = str(swarm_json.get("formation_reviewer") or "cli-codex")
        review_surface = str(swarm_json.get("review_surface") or "standard")
        try:
            # Default fallbacks apply to STANDARD surfaces only.
            #
            # On a standard surface, offering a single fixed candidate means an
            # openai implementer is offered an openai reviewer, the cross-lineage
            # rule correctly refuses, and a recoverable run is BLOCKED — measured as
            # `seq=0 crashed -> seq=1 blocked` in the simharness.
            #
            # On a SECURITY surface we deliberately do NOT auto-fill. That surface
            # requires two reviewers of two lineages, and quietly inventing the
            # second one is the favourable-default pattern this repo exists to
            # remove: the operator declared one reviewer, and the honest answer is
            # "this configuration is insufficient", not a silently-chosen stand-in.
            # Fall back ONLY when nothing was declared.
            #
            #  - nothing declared        -> choose a legal reviewer from defaults.
            #    Offering a single fixed candidate means an openai implementer gets
            #    an openai reviewer, the rule correctly refuses, and a recoverable
            #    run is BLOCKED (`seq=0 crashed -> seq=1 blocked` in the simharness).
            #  - declared but ILLEGAL    -> REFUSE. Silently substituting a legal
            #    reviewer would hide an operator mistake: they asked for codex to
            #    review sol, and the honest answer is that this is same-lineage, not
            #    a quiet swap to claude.
            #  - security surface        -> never auto-fill; two reviewers of two
            #    lineages must both be declared explicitly.
            declared_reviewer = str(swarm_json.get("formation_reviewer") or "").strip()
            candidates = [reviewer_candidate]
            if not declared_reviewer and review_surface != "security":
                candidates += ["cli-kimi", "cli-codex", "cli-claude", "cli-grok"]
            assigned_reviewers = assign_reviewer(
                implementer=implementer_ident,
                candidates=[c for c in candidates if c],
                surface=review_surface,
            )
        except LineageAssignmentError as exc:
            detail = f"reviewer assignment refused: {exc}"
            self._dal.close_attempt(attempt_id, "blocked", detail[:500])
            self._block_task(
                state,
                task_id,
                "reviewer_assignment_refused",
                detail[:500],
            )
            return "blocked"
        review_swarm_json.update(
            {
                # Propagate the RESOLVED identifier: the downstream reviewer prompt
                # re-runs assignment, and handing it an unregistered model name
                # would refuse there instead — the same block one layer down.
                "implementer_model": implementer_ident,
                "assigned_reviewers": list(assigned_reviewers),
            }
        )

        # Three-valued review. A SINGLE-attempt infra failure retries the
        # reviewer once (it may have been a transient blip), then blocks on
        # review — no retry consumed, never auto-CONFIRM.
        #
        # A reviewer that reports ``exhausted`` already failed over across every
        # legal lineage and/or already issued its corrective re-prompt. Re-running
        # it here would multiply invocations (2x the chain for infra, 4x for
        # drift) to re-learn an answer it just produced — and a second pass
        # against the same dead CLI is exactly what turns "the reviewer is
        # down" into "failed twice" across every queued attempt.
        outcome = self._review_once(
            task_row,
            review_swarm_json,
            session,
            verify_output,
            flags,
        )
        if outcome.verdict == "error" and not outcome.exhausted:
            outcome = self._review_once(
                task_row,
                review_swarm_json,
                session,
                verify_output,
                flags,
            )
        if outcome.verdict == "error":
            self._dal.close_attempt(
                attempt_id, "blocked", f"reviewer infrastructure failed: {outcome.feedback}"
            )
            self._block_task(
                state,
                task_id,
                "reviewer_infrastructure",
                "every legal reviewer failed — blocked on review, "
                "no retry consumed, never auto-confirmed",
            )
            return "blocked"

        if outcome.verdict == "confirm":
            if in_worktree:
                return self._confirm_merge(
                    state,
                    task_row,
                    attempt_id,
                    session,
                    swarm_json,
                    worktree_path,
                    worktree_branch,
                    verified_sha,
                    flags,
                    outcome,
                )
            # Coordinator-owned pathspec-limited commit: a confirm can never
            # sweep up a sibling's half-written files.
            #
            # M-35: a pathspec commit FAILURE must not mark the task done with
            # commit=null. That would leave uncommitted work in the shared
            # Phase-1 tree for the next snapshot to mis-attribute, or lose it
            # on run end. commit_paths returning None (nothing staged) is fine;
            # only exceptions fail closed.
            commit_sha = None
            if owned_paths:
                try:
                    with self._git_guard(state, state.working_dir, "confirm-commit"):
                        commit_sha = self._git.commit_paths(
                            state.working_dir,
                            owned_paths,
                            f"swarm {state.run_id}: task "
                            f"{swarm_json.get('task_key') or task_id} confirmed",
                        )
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("pathspec commit failed for %s", task_id)
                    detail = f"pathspec commit failed after review confirm: {exc}"
                    self._dal.close_attempt(attempt_id, "crashed", detail[:500])
                    return self._mechanical_failure(state, task_id, tier, detail)
            self._dal.close_attempt(attempt_id, "completed", outcome.feedback[:500])
            self._collab.update_board_task(
                task_id,
                {"status": "done", "result_ref": str(session.get("id") or "")},
            )
            self._emit(
                state.run_id,
                ACTION_REVIEW_CONFIRMED,
                {"task_id": task_id, "reviewer": outcome.reviewer, "commit": commit_sha},
            )
            self._emit(
                state.run_id,
                ACTION_TASK_COMPLETED,
                {"task_id": task_id, "attempt_id": attempt_id, "flags": flags},
            )
            state.plan_dirty = True
            return "done"

        # DENY: cascade trace + re-enqueue with feedback; LLM denies escalate
        # a tier immediately and consume a retry.
        self._dal.close_attempt(attempt_id, "review_denied", outcome.feedback[:500])
        self._emit(
            state.run_id,
            ACTION_REVIEW_DENIED,
            {"task_id": task_id, "feedback": outcome.feedback[:500], "flags": flags},
        )
        del attempt
        return self._consume_retry(
            state,
            task_id,
            tier,
            f"review denied: {outcome.feedback[:500]}",
            escalate=True,
        )

    def _confirm_merge(
        self,
        state: _RunState,
        task_row: dict[str, Any],
        attempt_id: str,
        session: dict[str, Any],
        swarm_json: dict[str, Any],
        worktree_path: str,
        worktree_branch: str,
        verified_sha: str | None,
        flags: list[str],
        outcome: SwarmReviewOutcome,
    ) -> str:
        """Phase-2 CONFIRM: merge the task's VERIFIED sha ``--no-ff`` into the
        main workspace, then remove the worktree (owned work was already
        committed by the quality gate before verify/review — M3).

        The merge target is ``verified_sha`` — the exact worktree HEAD the
        quality gate diffed/verified/reviewed — never the branch ref, so a
        ref tampered by a sandboxed same-run sibling cannot change what lands
        in main (M3; the Seatbelt profile already denies every ref outside
        the run's own branch namespace).

        Merging at CONFIRM under the run git_lock IS topological: eligibility
        already requires all deps done, and done ⇒ merged-at-confirm, so a
        branch always merges onto a main containing its deps' content. A
        CONFLICT still completes the task (the work is reviewed and committed
        on its branch) but: the branch stays alive, the conflict routes to the
        integration task's feedback for a manual merge, and dependents PARK
        via propagate_blocked with reason ``dep_merge_conflict`` (D5;
        integration exempt — a dependent must never silently build against a
        main missing its dep's content)."""
        task_id = str(task_row["id"])
        task_key = str(swarm_json.get("task_key") or task_id)
        owned_paths = [str(p) for p in (swarm_json.get("owned_paths") or [])]
        merged_sha: str | None = None
        merge = None
        conflicted = False
        conflict_files: tuple[str, ...] = ()
        conflict_detail = ""
        try:
            with self._git_guard(state, state.working_dir, "merge"):
                merge = self._worktrees().merge_branch(
                    state.working_dir,
                    worktree_branch,
                    f"swarm {state.run_id}: merge task {task_key}",
                    sha=verified_sha,
                )
            if merge.status == "conflict":
                conflicted = True
                conflict_files = merge.conflict_files
                conflict_detail = merge.detail
            else:
                merged_sha = merge.sha
        except Exception as exc:  # noqa: BLE001 -- infra failure: the work is CONFIRMED;
            # never burn a worker retry on a coordinator-side merge fault —
            # route it to integration exactly like a content conflict.
            LOG.exception("branch merge failed for task %s", task_id)
            conflicted = True
            conflict_detail = f"merge infrastructure failure: {exc}"
            # M4b: a merge that raised mid-flight can leave MERGE_HEAD wedging
            # the MAIN workspace — abort best-effort before moving on.
            try:
                with self._git_guard(state, state.working_dir, "post-failure-abort"):
                    self._worktrees().abort_merge(state.working_dir)
            except Exception:  # noqa: BLE001
                LOG.debug("post-failure merge abort failed", exc_info=True)
        # M2: remove the worktree either way — but NEVER destroy unpersisted
        # reviewer-CONFIRMED work: clean-check first, salvage-commit anything
        # dirty (bounded), and when salvage itself fails LEAVE the worktree in
        # place (recorded in the integration feedback + an activity event).
        self._remove_confirmed_worktree(
            state, task_id, task_key, worktree_branch, worktree_path, owned_paths
        )

        self._dal.close_attempt(attempt_id, "completed", outcome.feedback[:500])
        if conflicted:
            # Conflict bookkeeping lands BEFORE the task flips done: marking
            # done makes the integration task eligible the moment its last dep
            # terminalizes, and a racing worker must never claim it with a
            # brief that predates this feedback. Dependents likewise park
            # first (they cannot be claimed while this task is still
            # in_progress, so the block always wins the race).
            self._merge_swarm_json(
                task_id,
                {"merge_conflict": True, "conflict_files": list(conflict_files)},
            )
            self._route_conflict_to_integration(
                state, task_key, worktree_branch, verified_sha, conflict_files, conflict_detail
            )
            self._park_dependents_for_conflict(state, task_id)
        self._collab.update_board_task(
            task_id,
            {"status": "done", "result_ref": str(session.get("id") or "")},
        )
        self._emit(
            state.run_id,
            ACTION_REVIEW_CONFIRMED,
            {"task_id": task_id, "reviewer": outcome.reviewer, "commit": merged_sha},
        )
        if conflicted:
            self._emit(
                state.run_id,
                ACTION_MERGE_CONFLICT,
                {
                    "task_id": task_id,
                    "branch": worktree_branch,
                    "conflict_files": list(conflict_files)[:50],
                    "detail": conflict_detail[:300],
                },
            )
        else:
            self._emit(
                state.run_id,
                ACTION_BRANCH_MERGED,
                {
                    "task_id": task_id,
                    "branch": worktree_branch,
                    "sha": merged_sha,
                    "noop": bool(merge is not None and merge.status == "noop"),
                },
            )
        self._emit(
            state.run_id,
            ACTION_TASK_COMPLETED,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "flags": flags,
                **({"merge_conflict": True} if conflicted else {}),
            },
        )
        state.plan_dirty = True
        return "done"

    def _keep_worktree(
        self,
        state: _RunState,
        task_id: str,
        task_key: str,
        branch: str,
        worktree_path: str,
        dirty: Sequence[str],
        reason: str,
    ) -> None:
        """Record a worktree that must NOT be removed (unpersisted work with
        no salvage): in-memory for this coordinator, DURABLY on the task's
        swarm_json (R2a — crash-resume GC and terminal cleanup consult the
        record, not this process's memory), in the integration task's
        feedback, and as a ``worktree_kept`` activity event."""
        with state.cond:
            state.kept_worktrees.add(worktree_path)
        try:
            self._merge_swarm_json(task_id, {"worktree_kept": True})
        except Exception:  # noqa: BLE001 -- marker is best-effort; memory + feedback remain.
            LOG.warning("could not persist worktree_kept for %s", task_id, exc_info=True)
        self._append_integration_feedback(
            state,
            {
                "source": "worktree_kept",
                "branch": branch,
                "text": (
                    f"task {task_key}: {reason}; its worktree was left in place "
                    f"at {worktree_path} (branch {branch}) — recover the "
                    "uncommitted files there manually"
                )[:800],
            },
        )
        self._emit(
            state.run_id,
            ACTION_WORKTREE_KEPT,
            {
                "task_key": task_key,
                "path": worktree_path,
                "branch": branch,
                "dirty": list(dirty)[:20],
                "reason": reason,
            },
        )

    def _remove_confirmed_worktree(
        self,
        state: _RunState,
        task_id: str,
        task_key: str,
        branch: str,
        worktree_path: str,
        owned_paths: Sequence[str],
    ) -> bool:
        """Post-CONFIRM worktree removal that can never destroy confirmed
        work (M2c/M2d).

        Before the remove, the worktree is checked CLEAN of owned files
        (uncommitted or untracked): any owned dirt is salvage-committed to
        the task branch first (bounded index.lock retry inside
        ``salvage_commit``), and when the salvage itself fails the worktree
        is LEFT IN PLACE — recorded durably (R2a) — instead of being force-
        removed. A salvage that SUCCEEDS is surfaced to integration (R3):
        the commit sits on the branch AHEAD of the merged verified sha, so
        its content is NOT in main and someone must be told. Returns True
        when the worktree was kept."""
        try:
            with state.git_lock:
                worktrees = self._worktrees()
                dirty = [
                    path
                    for path in worktrees.dirty_paths(worktree_path)
                    if path not in _COORDINATOR_FILES and self._path_owned(path, owned_paths)
                ]
                if dirty:
                    salvage_sha = worktrees.salvage_commit(
                        worktree_path,
                        f"swarm {state.run_id}: salvage confirmed work for task {task_key}",
                    )
                    if salvage_sha is None:
                        LOG.error(
                            "could not salvage confirmed work for task %s — leaving "
                            "worktree %s in place (dirty: %s)",
                            task_key,
                            worktree_path,
                            ", ".join(dirty[:10]),
                        )
                        self._keep_worktree(
                            state,
                            task_id,
                            task_key,
                            branch,
                            worktree_path,
                            dirty,
                            "confirmed work could not be salvage-committed",
                        )
                        return True
                    self._append_integration_feedback(
                        state,
                        {
                            "source": "post_gate_salvage",
                            "branch": branch,
                            "sha": salvage_sha,
                            "text": (
                                f"task {task_key}: files changed AFTER the quality gate "
                                f"were salvaged to branch {branch} at {salvage_sha[:12]} "
                                "and are NOT in the workspace — merge/cherry-pick that "
                                f"commit if the changes matter: {', '.join(dirty[:10])}"
                            )[:800],
                        },
                    )
                outcome = worktrees.remove(
                    state.working_dir,
                    worktree_path,
                    salvage=True,
                    message=f"swarm {state.run_id}: salvage late work for task {task_key}",
                )
                if outcome.status == "salvage_failed":
                    # Dirt appeared between the clean-check and the remove (a
                    # dying worker's last writes) and could not be committed.
                    self._keep_worktree(
                        state,
                        task_id,
                        task_key,
                        branch,
                        worktree_path,
                        [],
                        "late dirty state could not be salvage-committed",
                    )
                    return True
        except Exception:  # noqa: BLE001 -- removal is best-effort; the worktree simply stays.
            LOG.warning("worktree remove failed for %s", worktree_path, exc_info=True)
        return False

    def _append_integration_feedback(self, state: _RunState, entry: dict[str, Any]) -> str | None:
        """Append one structured entry to the INTEGRATION task's feedback.

        M5: the ``get_swarm_json → append → set_swarm_json`` read-modify-
        write is ATOMIC under the run's ``git_lock`` — two workers confirming
        conflicting tasks concurrently must BOTH land their entries (the
        unlocked RMW silently lost one). ``git_lock`` on purpose: every
        caller already lives in the git serialization domain, and the RLock
        makes reentry from ``_confirm_merge``'s locked sections safe."""
        with state.git_lock:
            run = self._dal.get_run(state.run_id) or {}
            integration = self._integration_task(self._member_tasks(state.run_id, run))
            if integration is None:
                return None
            integration_id = str(integration["id"])
            current = self._dal.get_swarm_json(integration_id) or {}
            current["feedback"] = [*list(current.get("feedback") or []), entry]
            self._dal.set_swarm_json(integration_id, current)
            return integration_id

    def _route_conflict_to_integration(
        self,
        state: _RunState,
        task_key: str,
        branch: str,
        sha: str | None,
        conflict_files: Sequence[str],
        detail: str,
    ) -> str | None:
        """Land a structured merge-conflict entry in the INTEGRATION task's
        feedback: integration runs in the main workspace (owned_paths ['.'])
        and its verify_command (the full suite) is the post-final-merge gate.

        R4: the entry carries the VERIFIED sha, and the brief instructs
        merging that sha — a same-run sibling can move the branch ref inside
        the namespace allow after routing, but it cannot change what
        integration merges."""
        files = ", ".join(list(conflict_files)[:20]) or "(conflict files unavailable)"
        target = sha or branch
        text = (
            f"task {task_key} was confirmed but its branch {branch} conflicts "
            f"with the workspace — merge {target} manually and resolve: {files}"
        )
        if detail:
            text += f" ({detail[:200]})"
        return self._append_integration_feedback(
            state,
            # ``branch``/``sha`` are structured on purpose: the integration
            # brief's worktree variant lists the routed merges scoped to
            # exactly these (M1/R4 — merge the sha, branch is for humans).
            {"source": "merge_conflict", "branch": branch, "sha": sha, "text": text[:800]},
        )

    def _park_dependents_for_conflict(self, state: _RunState, task_id: str) -> None:
        """D5: PARK every transitive dependent of a confirmed-but-conflicted
        task (``propagate_blocked``; the integration task is exempt in the DAL
        and runs over completed work) — the summary marks the run partial."""
        for blocked_id in self._dal.propagate_blocked(state.run_id, task_id):
            self._emit(
                state.run_id,
                ACTION_TASK_BLOCKED,
                {
                    "task_id": blocked_id,
                    "reason": "dep_merge_conflict",
                    "detail": (
                        f"dependency {task_id} confirmed with a merge conflict; "
                        "integration resolves the branch manually"
                    ),
                },
            )
            self._remove_task_worktree(state, blocked_id, salvage=True)
        state.plan_dirty = True

    def _remove_task_worktree(self, state: _RunState, task_id: str, *, salvage: bool) -> None:
        """Best-effort worktree removal for a task leaving the live set
        (blocked/parked). With ``salvage`` the dirty state is committed to the
        branch first so partial work stays inspectable. Gated on the RECORDED
        registration (m6) so cleanup still runs when only the record says
        this run uses worktrees."""
        if not (state.worktree_mode or state.worktree_recorded):
            return
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        worktree_path = str(swarm_json.get("worktree_path") or "")
        if not worktree_path:
            return
        if swarm_json.get("worktree_kept") or worktree_path in state.kept_worktrees:
            return  # R2a: kept worktrees hold unsalvageable work — never remove.
        try:
            with state.git_lock:
                outcome = self._worktrees().remove(
                    state.working_dir,
                    worktree_path,
                    salvage=salvage,
                    message=f"swarm {state.run_id}: salvage partial work for task {task_id}",
                )
            if outcome.status == "salvage_failed":
                self._keep_worktree(
                    state,
                    task_id,
                    str(swarm_json.get("task_key") or task_id),
                    str(swarm_json.get("worktree_branch") or ""),
                    worktree_path,
                    [],
                    "partial work could not be salvage-committed",
                )
        except Exception:  # noqa: BLE001 -- GC best-effort; prune/terminal cleanup backstop.
            LOG.warning("worktree remove failed for task %s", task_id, exc_info=True)

    def _review_once(
        self,
        task_row: Mapping[str, Any],
        swarm_json: Mapping[str, Any],
        session: Mapping[str, Any],
        verify_output: str,
        flags: Sequence[str],
    ) -> SwarmReviewOutcome:
        try:
            return self._reviewer.review(
                task=task_row,
                swarm_json=swarm_json,
                session=session,
                verify_output=verify_output,
                flags=flags,
            )
        except Exception as exc:  # noqa: BLE001 -- a raising reviewer is an infra failure.
            return SwarmReviewOutcome(verdict="error", feedback=str(exc))

    def _session_files(self, session: Mapping[str, Any]) -> list[str] | None:
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

    @staticmethod
    def _path_owned(path: str, owned_paths: Sequence[str]) -> bool:
        normalized = path.strip().lstrip("./")
        for owned in owned_paths:
            owned_norm = owned.strip().lstrip("./")
            if owned == "." or owned_norm == "":
                return True
            if normalized == owned_norm or normalized.startswith(owned_norm + "/"):
                return True
        return False

    def _mechanical_failure(self, state: _RunState, task_id: str, tier: str, detail: str) -> str:
        """Mechanical failures get ONE same-tier retry with the error fed back
        (no retry consumed) — the worker usually fixes its own compile error —
        before the escalation ladder is spent."""
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        if not swarm_json.get("mechanical_retry_used"):
            self._merge_swarm_json(
                task_id,
                {
                    "mechanical_retry_used": True,
                    "feedback": [
                        *list(swarm_json.get("feedback") or []),
                        {"source": "mechanical", "text": detail[:800]},
                    ],
                },
            )
            return "requeue"
        return self._consume_retry(state, task_id, tier, detail, escalate=True)

    def _consume_retry(
        self, state: _RunState, task_id: str, tier: str, detail: str, *, escalate: bool
    ) -> str:
        swarm_json = self._dal.get_swarm_json(task_id) or {}
        # The cap consults BOTH spines: this run's own counter and the pump
        # ledger (109). Before 109 a lane could be re-dispatched 726 times by a
        # pump while this counter still read 0, because nothing wrote a row.
        retries = effective_retry_count(
            self._dal, task_id, swarm_json_retries=int(swarm_json.get("retries") or 0)
        )
        feedback = [
            *list(swarm_json.get("feedback") or []),
            {"source": "retry", "text": detail[:800]},
        ]
        if retries > self._retry_cap:
            self._merge_swarm_json(task_id, {"retries": retries, "feedback": feedback})
            self._block_task(
                state,
                task_id,
                "retry_cap",
                f"{retries} failed attempts exceed the {self._retry_cap}-retry cap",
            )
            return "blocked"
        self._merge_swarm_json(
            task_id,
            {
                "retries": retries,
                "feedback": feedback,
                "current_tier": self._escalate(tier) if escalate else tier,
            },
        )
        return "requeue"

    def _block_task(self, state: _RunState, task_id: str, reason: str, detail: str) -> None:
        """Block a task and propagate transitively (integration exempt — it
        runs over completed work and the summary is marked partial). Worktree
        mode salvage-removes the blocked task's worktree so partial work stays
        inspectable on the branch."""
        self._collab.update_board_task(task_id, {"status": "blocked"})
        self._emit(
            state.run_id,
            ACTION_TASK_BLOCKED,
            {"task_id": task_id, "reason": reason, "detail": detail[:300]},
        )
        self._remove_task_worktree(state, task_id, salvage=True)
        for blocked_id in self._dal.propagate_blocked(state.run_id, task_id):
            self._emit(
                state.run_id,
                ACTION_TASK_BLOCKED,
                {"task_id": blocked_id, "reason": f"dependency {task_id} blocked"},
            )
            self._remove_task_worktree(state, blocked_id, salvage=True)
        state.plan_dirty = True

    # ------------------------------------------------------------------
    # PLAN.md projection
    # ------------------------------------------------------------------

    @staticmethod
    def _coordinator_file_states(
        working_dir: str,
    ) -> dict[str, tuple[bool, str | None, str]]:
        """Capture (exists, content digest, git mode) for each coordinator file.

        Mode is part of provenance: an operator-only ``chmod`` must not be
        treated as this run's write, and digest-bound staging must not re-read
        live path bits for the committed mode.
        """
        states: dict[str, tuple[bool, str | None, str]] = {}
        for path in _COORDINATOR_FILES:
            target = Path(working_dir) / path
            try:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except FileNotFoundError:
                states[path] = (False, None, "100644")
            except OSError:
                # Existing but unreadable/non-regular state is never eligible
                # for a coordinator commit.
                states[path] = (True, None, "100644")
            else:
                mode = SubprocessSwarmGit._file_mode(working_dir, path)
                states[path] = (True, digest, mode)
        return states

    @staticmethod
    def _coordinator_content_matches(
        current: tuple[bool, str | None, str] | None,
        expected: tuple[bool, str | None, str],
    ) -> bool:
        """Content identity only — mode drift is bound at stage time, not here."""
        if current is None:
            return False
        return current[0] == expected[0] and current[1] == expected[1] and expected[1] is not None

    def _seed_coordinator_file_delta(
        self,
        state: _RunState,
        run: Mapping[str, Any],
    ) -> None:
        """Recognize the deterministic PLAN projection written at provision.

        Provisioning precedes scheduler launch, so it cannot call the runtime
        write tracker directly. Exact content identity supplies the missing
        provenance: a modified operator PLAN does not match and stays
        ineligible, while the generated projection remains available in fresh
        worktrees forked from the first snapshot.
        """
        try:
            from omniagentos.swarm.planner import render_plan_md

            plan = SwarmPlan.model_validate(json.loads(run.get("plan_json") or "{}"))
            statuses = {}
            for task in self._member_tasks(state.run_id, run):
                key = str(self._swarm_json_of(task).get("task_key") or task["id"])
                statuses[key] = str(task["status"])
            rendered_digests = {
                hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                for rendered in (
                    render_plan_md(plan),
                    render_plan_md(plan, statuses),
                )
            }
            plan_state = state.coordinator_file_state.get("PLAN.md")
            if plan_state is not None and plan_state[0] and plan_state[1] in rendered_digests:
                # Provision writes a regular non-executable file. Never adopt
                # ambient operator mode from the pre-run capture as "this run
                # produced" — mode-only dirt is not a coordinator write.
                exists, digest, _live_mode = plan_state
                state.coordinator_file_pending["PLAN.md"] = (exists, digest, "100644")
        except Exception:  # noqa: BLE001 -- provenance failure excludes, never includes.
            LOG.debug(
                "could not recognize provisioned PLAN.md for %s",
                state.run_id,
                exc_info=True,
            )

    def _pending_coordinator_delta(
        self,
        state: _RunState,
    ) -> dict[str, tuple[bool, str | None, str]]:
        current = self._coordinator_file_states(state.working_dir)
        with state.cond:
            pending = dict(state.coordinator_file_pending)
        # Content match only: operator mode dirt must not drop a still-valid
        # run-written PLAN from the branch-base commit. Mode is rebound from
        # the approved pending triple at stage time.
        return {
            path: expected
            for path, expected in pending.items()
            if self._coordinator_content_matches(current.get(path), expected)
        }

    @staticmethod
    def _settle_coordinator_delta(
        state: _RunState,
        committed: Mapping[str, tuple[bool, str | None, str]],
    ) -> None:
        with state.cond:
            for path, committed_state in committed.items():
                pending_state = state.coordinator_file_pending.get(path)
                if pending_state is None:
                    continue
                # Match on content identity so a mode-only working-tree drift
                # after a successful digest-bound commit still clears pending.
                if (
                    pending_state[0] == committed_state[0]
                    and pending_state[1] == committed_state[1]
                    and pending_state[1] is not None
                ):
                    state.coordinator_file_pending.pop(path, None)

    def _write_plan_doc(self, state: _RunState) -> None:
        """Regenerate PLAN.md (tmp+fsync+rename) from plan_json + live statuses.
        Best-effort: the database is the machine truth."""
        try:
            from omniagentos.swarm.planner import write_plan_md

            run = self._dal.get_run(state.run_id)
            if run is None or not os.path.isdir(state.working_dir):
                return
            plan = SwarmPlan.model_validate(json.loads(run.get("plan_json") or "{}"))
            statuses = {}
            for task in self._member_tasks(state.run_id, run):
                key = str(self._swarm_json_of(task).get("task_key") or task["id"])
                statuses[key] = str(task["status"])
            write_plan_md(plan, state.working_dir, statuses)
            written_states = self._coordinator_file_states(state.working_dir)
            with state.cond:
                for path, written_state in written_states.items():
                    if written_state[1] is not None:
                        state.coordinator_file_pending[path] = written_state
                    state.coordinator_file_state[path] = written_state
        except Exception:  # noqa: BLE001 -- a projection failure never touches control flow.
            LOG.debug("PLAN.md regeneration failed for %s", state.run_id, exc_info=True)


# ---------------------------------------------------------------------------
# API wire-up (feature-flagged; WP10 flips the flag)
# ---------------------------------------------------------------------------


_DEFAULT_SCHEDULERS: dict[str, SwarmScheduler] = {}
_DEFAULT_SCHEDULER_LOCK = threading.Lock()


def _canonical_scheduler_db_path(db_path: str) -> str:
    if db_path == ":memory:":
        return db_path
    return os.path.realpath(os.path.abspath(os.path.expanduser(db_path)))


def _default_scheduler(db_path: str | None = None) -> SwarmScheduler:
    """The production wiring (WP5b): SwarmRouter + UnifiedSpawner + the
    longhaul-backed terminal classifier + configs/swarm.yaml tier timeouts.
    Imports stay inside the function — ``swarm.router``/``swarm.spawn`` import
    this module for the seam types. Instances are cached per database so the
    scheduler's adoption CAS always targets the database its caller selected."""
    resolved = _canonical_scheduler_db_path(db_path if db_path is not None else default_db_path())
    with _DEFAULT_SCHEDULER_LOCK:
        cached = _DEFAULT_SCHEDULERS.get(resolved)
        if cached is not None:
            return cached
        from omniagentos.db.store import SqliteStore
        from omniagentos.swarm.activity import StoreSwarmEmitter
        from omniagentos.swarm.router import SwarmRouter, tier_timeout_minutes
        from omniagentos.swarm.spawn import UnifiedSpawner, swarm_terminal_classifier

        emitter = StoreSwarmEmitter(SqliteStore(resolved))
        scheduler = SwarmScheduler(
            dal=SwarmDal(resolved),
            collab=CollabStore(resolved),
            emitter=emitter,
            spawner=UnifiedSpawner(db_path=resolved),
            router=SwarmRouter(db_path=resolved, emitter=emitter),
            classifier=swarm_terminal_classifier,
            timeout_minutes=tier_timeout_minutes(),
            db_path=resolved,
        )
        _DEFAULT_SCHEDULERS[resolved] = scheduler
        return scheduler


def shutdown_default_schedulers() -> None:
    """Stop process-lifetime schedulers and close their router limits DALs."""
    with _DEFAULT_SCHEDULER_LOCK:
        schedulers = list(_DEFAULT_SCHEDULERS.values())
        _DEFAULT_SCHEDULERS.clear()
    for scheduler in schedulers:
        try:
            scheduler.shutdown()
        except Exception:  # noqa: BLE001 -- application shutdown remains best-effort.
            LOG.exception("default swarm scheduler shutdown failed")


def activate_run_if_enabled(run_id: str) -> bool:
    """Start a provisioned run's coordinator IF the feature flag is on.

    ``POST /api/swarm`` calls this after provisioning; with
    ``OMNIAGENTOS_SWARM_EXECUTE`` unset/false (the default) it is a no-op, so
    merging WP5a cannot change API behavior. WP10 flips the flag.
    """
    if not swarm_execute_enabled():
        return False
    try:
        handle = _default_scheduler().start_run(run_id)
        return handle is not None
    except Exception:  # noqa: BLE001 -- activation failure must not fail the provisioning response.
        LOG.exception("swarm run activation failed for %s", run_id)
        return False


def _parse_heartbeat(value: Any) -> datetime | None:
    """Tolerant parse of a ``swarm_runs.heartbeat_at`` stamp to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def resume_stale_swarms(
    *,
    scheduler: SwarmScheduler | None = None,
    dal: SwarmDal | None = None,
    reconcile_orphans: Callable[[], Mapping[str, str]] | None = None,
    stale_minutes: float = DEFAULT_ADOPT_STALE_MINUTES,
    db_path: str | None = None,
) -> dict[str, Any]:
    """WP10 startup resume sweep: re-adopt orphaned runs + reconcile WP3 orphans.

    Called ONCE on supervisor startup (``SessionSupervisor.resume_swarms_once``),
    never per poll pass — the per-pass stale handling stays A5's
    ``mark_stale_failed`` sweep. Two best-effort phases, each isolated so one
    failing can never block the other or escape to the caller:

    1. **Provider orphan reconciliation** (``ProviderSessionRunner
       .reconcile_orphans``): nonterminal codex/grok/gemini/kimi/qwen session rows are
       verified by pgid AND command identity, so dead provider processes are
       terminal in the DB before any adopted coordinator rebuilds from it.
    2. **Heartbeat-lease takeover**: every active run whose
       coordinator heartbeat is > ``stale_minutes`` old (or NULL) gets a
       ``resume_swarm`` call — which self-guards via the ``adopt_run`` CAS, so a
       coordinator that came alive between the list and the call is never
       displaced. Fresh heartbeats are never touched (live coordinator exists).
       Queued runs are admission-parked with no coordinator on purpose and are
       left alone.

    With no injected ``scheduler`` and ``OMNIAGENTOS_SWARM_EXECUTE`` unset/false
    the takeover phase is skipped entirely (execution is disabled — starting
    coordinators would flip merge-safety off from a sweep), while phase 1 still
    runs: terminalizing dead provider rows is safe and useful regardless.

    The summary includes the resolved ``db_path``, ``total_runs``, and
    ``candidates``. An empty or mismatched database is reported in ``errors``;
    neither condition can masquerade as a successful no-op.
    """
    summary: dict[str, Any] = {
        "reconciled": {},
        "resumed": [],
        "skipped_fresh": [],
        "errors": [],
        "total_runs": 0,
        "candidates": 0,
    }
    resolved_db_path = _canonical_scheduler_db_path(
        db_path or str(getattr(dal, "db_path", "") or default_db_path())
    )
    summary["db_path"] = resolved_db_path

    try:
        if reconcile_orphans is None:
            from omniagentos.swarm.provider_exec import ProviderSessionRunner

            reconcile_orphans = ProviderSessionRunner(db_path=resolved_db_path).reconcile_orphans
        summary["reconciled"] = dict(reconcile_orphans())
    except Exception:  # noqa: BLE001 -- best-effort: the takeover phase must still run.
        LOG.exception("provider orphan reconcile failed (resume sweep continues)")
        summary["errors"].append("reconcile_orphans")

    try:
        if scheduler is None:
            if not swarm_execute_enabled():
                summary["skipped_flag_off"] = True
                summary["recovery_disabled"] = True
                LOG.warning(
                    "OMNIAGENTOS_SWARM_EXECUTE is off; stale-run recovery is DISABLED. "
                    "Set OMNIAGENTOS_SWARM_EXECUTE=1 to enable automatic recovery. "
                    "Active runs may not resume on re-entry."
                )
                return summary
            scheduler = _default_scheduler(resolved_db_path)
        own_dal = dal is None
        if dal is None:
            dal = SwarmDal(resolved_db_path)
        try:
            bindings = {
                "scheduler": getattr(scheduler, "_db_path", None),
                "scheduler_dal": getattr(getattr(scheduler, "_dal", None), "db_path", None),
                "dal": getattr(dal, "db_path", None),
            }
            mismatches = {
                name: str(path)
                for name, path in bindings.items()
                if path is not None
                # Safety (`is not True`): unknown binding identity is a DB mismatch.
                and inode_paths_equal(
                    _canonical_scheduler_db_path(str(path)),
                    resolved_db_path,
                )
                is not True
            }
            if mismatches:
                summary["errors"].append("db_mismatch")
                summary["db_mismatch"] = {"listing": resolved_db_path, **mismatches}
                LOG.error("swarm resume sweep db_mismatch: %s", summary["db_mismatch"])
                return summary

            runs = dal.list_runs()
            summary["total_runs"] = len(runs)
            candidates = [run for run in runs if str(run.get("status")) in _ACTIVE_STATUSES]
            summary["candidates"] = len(candidates)
            if not runs:
                summary["errors"].append("empty_database")
                LOG.error("swarm resume sweep database is empty: %s", resolved_db_path)
                return summary

            cutoff = datetime.now(UTC) - timedelta(minutes=max(0.0, stale_minutes))
            for run in candidates:
                run_id = str(run["id"])
                heartbeat = _parse_heartbeat(run.get("heartbeat_at"))
                if heartbeat is not None and heartbeat >= cutoff:
                    summary["skipped_fresh"].append(run_id)
                    continue
                try:
                    handle = scheduler.resume_swarm(run_id)
                except Exception:  # noqa: BLE001 -- one bad run must not stop the sweep.
                    LOG.exception("resume_swarm failed for %s (resume sweep continues)", run_id)
                    summary["errors"].append(run_id)
                    continue
                if handle is not None:
                    summary["resumed"].append(run_id)
                else:
                    # The adopt CAS refused (fresh heartbeat / terminal by now).
                    summary["skipped_fresh"].append(run_id)
        finally:
            if own_dal:
                dal.close()
    except Exception:  # noqa: BLE001 -- a sweep fault must never block supervisor startup.
        LOG.exception("swarm resume sweep failed")
        summary["errors"].append("sweep")
    return summary


__all__ = [
    "ACTION_LIVENESS_CHANGED",
    "ACTION_LIVENESS_SUMMARY",
    "DEFAULT_TIMEOUT_MINUTES",
    "IDLE_TIMEOUT_FRACTION",
    "LIVENESS_POLL_SECONDS",
    "MAX_SLOTS",
    "SWARM_EXECUTE_ENV",
    "TIER_LADDER",
    "CrossLineageSwarmReviewer",
    "DefaultSwarmRouter",
    "DurableLimits",
    "RouteDecision",
    "SchedulerClock",
    "SpawnRequest",
    "SubprocessSwarmGit",
    "SwarmReviewOutcome",
    "SwarmRunHandle",
    "SwarmScheduler",
    "activate_run_if_enabled",
    "build_worker_brief",
    "default_task_splitter",
    "default_terminal_classifier",
    "default_verifier",
    "resume_stale_swarms",
    "shutdown_default_schedulers",
    "subtasks_request_protocol_lines",
    "swarm_execute_enabled",
]
