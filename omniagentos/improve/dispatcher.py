"""Improvement-lane dispatcher.

The dispatcher is the single SQLite writer for the improvement lane.
Workers emit result FILES; only the dispatcher ingests them into the DB.

Budget seam
-----------
``tick`` is the only place the lane turns a scheduled test into work that costs
money: in effective mode ``on`` it writes the ``CALL_RESERVED`` saga row that a
worker picks up. That INSERT is therefore the admission point for
``omniagentos.improve.budget`` — the $200/UTC-day hard-cap ledger. Every
dispatch reserves against the ``workers`` pool BEFORE the row is written, so a
day that has spent its ceiling cannot queue more work (the ledger's own pause +
notify path fires from inside ``reserve``/``settle``).

What a refusal DOES follows the house posture in ``omniagentos.budget.policy``
(operator ruling: budgets are task-sized guidance, not hard blockers):

  * ADVISORY (default) — the refusal is logged loudly with the pool, the reason
    and the day's committed spend, and the dispatch PROCEEDS.
  * BLOCK (``OMNIAGENTOS_BUDGET_ENFORCEMENT=block``, the mode the 24/7 loop runs
    under) — the dispatch is refused: no saga row, nothing queued, the test id is
    reported in ``budget_blocked``.

A dispatch that is admitted is settled immediately at its reserved maximum
(``usage_available=False`` — the ledger's fail-closed path). The dispatcher hands
work to out-of-process workers and never observes their real cost, and an
unsettled hold would silently expire at the reservation TTL, which would make the
"daily" ceiling forget the spend minutes after it was incurred. Charging the
estimate is deliberate and conservative: a cost the lane cannot measure costs
budget instead of hiding.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from omniagentos.budget.policy import DEFAULT_PER_TASK_USD
from omniagentos.budget.policy import blocks as budget_blocks
from omniagentos.db.store import SqliteStore
from omniagentos.improve.budget import (
    DAILY_CEILING_USD,
    BudgetError,
    BudgetLedger,
    BudgetRefused,
)
from omniagentos.runtime_paths import TOKEN_VAR_ENV_KEYS, resolve_var_root

LOG = logging.getLogger(__name__)

IMPROVE_MODE_ENV = "OMNIAGENTOS_IMPROVE_MODE"
ImproveMode = Literal["off", "shadow", "on"]
IMPROVE_MODES: tuple[ImproveMode, ...] = ("off", "shadow", "on")
DEFAULT_IMPROVE_MODE: ImproveMode = "off"
TICK_SECONDS = 300
IMPROVE_APPROVAL_ACTION_CLASS = "improve_lane_enable"

# Which hard-cap pool an improvement dispatch spends from. ``workers`` is the
# pool the plan assigns to patch lanes and escalations (POOL_CAPS: $90 of $200).
IMPROVE_BUDGET_POOL = "workers"
# What one dispatch is assumed to cost at admission. Reuses the house per-task
# number rather than inventing a second one; the ledger charges this estimate
# fail-closed because the dispatcher never sees the worker's real spend.
IMPROVE_DISPATCH_ESTIMATE_USD = DEFAULT_PER_TASK_USD
# Where the day-cap ledger lives when the caller does not inject one. Explicit
# override first, then the process-wide var root (the same knob tests and the
# launch scripts isolate), then repo ``var/``.
IMPROVE_BUDGET_DB_ENV = "OMNIAGENTOS_IMPROVE_BUDGET_DB"

NON_TERMINAL_SAGA_STATES: tuple[str, ...] = (
    "CALL_RESERVED",
    "CALL_SENT",
    "APPROVED_SHA",
    "MERGE_INTENT",
    "MERGED_SHA",
    "VERIFYING",
)
TERMINAL_SAGA_STATES: tuple[str, ...] = ("VERIFIED", "REVERTED")
# States that mean a merge landed on HEAD but nothing has proven it good yet.
UNVERIFIED_HEAD_STATES: tuple[str, ...] = ("MERGED_SHA", "VERIFYING")


def parse_improve_mode(raw: str | None) -> ImproveMode:
    if raw is None:
        return "off"
    cleaned = raw.strip().lower()
    if cleaned in IMPROVE_MODES:
        return cleaned  # type: ignore[return-value]
    return "off"


def improve_mode_requested(env: Mapping[str, str] | None = None) -> ImproveMode:
    source = env if env is not None else os.environ
    raw = source.get(IMPROVE_MODE_ENV)
    return parse_improve_mode(raw)


def human_approval(connection: sqlite3.Connection, *, now: str | None = None) -> str | None:
    if now is None:
        from omniagentos.contracts import utc_now_iso

        now = utc_now_iso()

    query = """
        SELECT decided_by FROM approvals
        WHERE action_class = ?
          AND state = 'approved'
          AND decided_by IS NOT NULL
          AND decided_by != ''
          AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
        ORDER BY decided_at DESC, id DESC
        LIMIT 1
    """
    row = connection.execute(query, (IMPROVE_APPROVAL_ACTION_CLASS, now)).fetchone()
    if row is not None:
        return str(row[0])
    return None


@dataclass(frozen=True)
class ModeDecision:
    requested: ImproveMode
    effective: ImproveMode
    approved_by: str | None
    reason: str


def resolve_improve_mode(
    connection: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
    now: str | None = None,
) -> ModeDecision:
    requested = improve_mode_requested(env)
    if requested == "off":
        return ModeDecision(
            requested="off",
            effective="off",
            approved_by=None,
            reason="flag off",
        )
    if requested == "shadow":
        return ModeDecision(
            requested="shadow",
            effective="shadow",
            approved_by=None,
            reason="flag shadow",
        )

    approved_by = human_approval(connection, now=now)
    if approved_by is not None:
        return ModeDecision(
            requested="on",
            effective="on",
            approved_by=approved_by,
            reason=f"flag on + human approval by {approved_by}",
        )
    else:
        return ModeDecision(
            requested="on",
            effective="shadow",
            approved_by=None,
            reason="flag on + no approved_by row",
        )


@dataclass(frozen=True)
class ImproveTest:
    test_id: str
    closure: frozenset[str]  # import closure: module paths this test loads/mutates
    depends_on: tuple[str, ...] = ()  # test_ids that must finish first


class SequencerError(ValueError):
    pass


def schedule_wave(
    tests: Sequence[ImproveTest],
    *,
    completed: AbstractSet[str] = frozenset(),
    running: Sequence[ImproveTest] = (),
) -> list[ImproveTest]:
    # Validate prerequisites first
    valid_test_ids = {t.test_id for t in tests}
    for t in tests:
        for dep in t.depends_on:
            if dep not in valid_test_ids and dep not in completed:
                raise SequencerError(f"unknown prerequisite: {dep}")

    admitted: list[ImproveTest] = []
    current_closures: list[frozenset[str]] = [r.closure for r in running]

    for t in sorted(tests, key=lambda x: x.test_id):
        if t.test_id in completed:
            continue
        if any(r.test_id == t.test_id for r in running):
            continue

        all_deps_met = True
        for dep in t.depends_on:
            if dep not in completed:
                all_deps_met = False
                break
        if not all_deps_met:
            continue

        intersects = False
        for c in current_closures:
            if not t.closure.isdisjoint(c):
                intersects = True
                break
        if intersects:
            continue

        admitted.append(t)
        current_closures.append(t.closure)

    return admitted


def sequence_waves(tests: Sequence[ImproveTest]) -> list[list[str]]:
    completed: set[str] = set()
    waves: list[list[str]] = []
    all_test_ids = {t.test_id for t in tests}

    while len(completed) < len(tests):
        wave = schedule_wave(tests, completed=completed)
        if not wave:
            remaining = sorted(all_test_ids - completed)
            raise SequencerError(f"dependency cycle: {remaining}")

        wave_ids = sorted([t.test_id for t in wave])
        waves.append(wave_ids)
        completed.update(wave_ids)

    return waves


class GitRefs(Protocol):
    def head_sha(self) -> str: ...
    def commit_exists(self, sha: str) -> bool: ...
    def is_ancestor(self, sha: str, descendant: str) -> bool: ...


class GitCli:
    repo_root: str | os.PathLike[str]

    def __init__(self, repo_root: str | os.PathLike[str]) -> None:
        self.repo_root = repo_root

    def head_sha(self) -> str:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip()

    def commit_exists(self, sha: str) -> bool:
        res = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0

    def is_ancestor(self, sha: str, descendant: str) -> bool:
        res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, descendant],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0


@dataclass(frozen=True)
class SagaReconciliation:
    attempt_id: str
    previous_state: str
    new_state: str
    reason: str


def reconcile_saga(
    connection: sqlite3.Connection, refs: GitRefs, *, now: str | None = None
) -> list[SagaReconciliation]:
    if now is None:
        from omniagentos.contracts import utc_now_iso

        now = utc_now_iso()

    head = refs.head_sha()

    query = """
        SELECT attempt_id, state, approved_sha, merged_sha FROM improve_saga
        WHERE state IN (?, ?, ?, ?, ?, ?)
        ORDER BY updated_at ASC, attempt_id ASC
    """
    rows = connection.execute(query, NON_TERMINAL_SAGA_STATES).fetchall()

    results: list[SagaReconciliation] = []

    for row in rows:
        attempt_id = str(row["attempt_id"])
        prev_state = str(row["state"])
        approved_sha = row["approved_sha"] or ""
        merged_sha = row["merged_sha"] or ""

        new_state = prev_state
        reason = ""
        derived_merged_sha: str | None = None

        if prev_state == "CALL_RESERVED":
            new_state = "REVERTED"
            reason = "reserved but never sent"
        elif prev_state == "CALL_SENT":
            new_state = "REVERTED"
            reason = "sent but never approved"
        elif prev_state == "APPROVED_SHA":
            if (
                approved_sha != ""
                and refs.commit_exists(approved_sha)
                and refs.is_ancestor(approved_sha, head)
            ):
                new_state = "VERIFYING"
                derived_merged_sha = approved_sha
                reason = "approved sha already on HEAD; verification owed"
            else:
                new_state = "REVERTED"
                reason = "approved but never merged"
        elif prev_state == "MERGE_INTENT":
            if (
                approved_sha != ""
                and refs.commit_exists(approved_sha)
                and refs.is_ancestor(approved_sha, head)
            ):
                new_state = "VERIFYING"
                derived_merged_sha = approved_sha
                reason = "merge intent landed on HEAD; verification owed"
            else:
                new_state = "REVERTED"
                reason = "merge intent never landed"
        elif prev_state == "MERGED_SHA":
            if (
                merged_sha != ""
                and refs.commit_exists(merged_sha)
                and refs.is_ancestor(merged_sha, head)
            ):
                new_state = "VERIFYING"
                reason = "merged sha on HEAD; verification owed"
            else:
                new_state = "REVERTED"
                reason = "merged sha is not on HEAD"
        elif prev_state == "VERIFYING":
            new_state = "VERIFYING"
            reason = "verification still owed"

        if derived_merged_sha is not None:
            connection.execute(
                "UPDATE improve_saga SET state = ?, merged_sha = ?, updated_at = ? "
                "WHERE attempt_id = ?",
                (new_state, derived_merged_sha, now, attempt_id),
            )
        else:
            connection.execute(
                "UPDATE improve_saga SET state = ?, updated_at = ? WHERE attempt_id = ?",
                (new_state, now, attempt_id),
            )

        results.append(
            SagaReconciliation(
                attempt_id=attempt_id,
                previous_state=prev_state,
                new_state=new_state,
                reason=reason,
            )
        )

    return results


def unverified_head(connection: sqlite3.Connection) -> str | None:
    query = f"""
        SELECT attempt_id FROM improve_saga
        WHERE state IN ({','.join('?' * len(UNVERIFIED_HEAD_STATES))})
        ORDER BY updated_at ASC, attempt_id ASC
        LIMIT 1
    """
    row = connection.execute(query, UNVERIFIED_HEAD_STATES).fetchone()
    if row is not None:
        return str(row[0])
    return None


def emit_worker_result(inbox: Path, payload: Mapping[str, Any]) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    run_id = payload.get("run_id")
    if not run_id:
        raise ValueError("payload must contain a run_id")
    file_path = inbox / f"{run_id}.json"
    tmp_path = inbox / f"{run_id}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, file_path)
    return file_path


def ingest_worker_results(
    connection: sqlite3.Connection, inbox: Path, *, now: str | None = None
) -> list[str]:
    if now is None:
        from omniagentos.contracts import utc_now_iso

        now = utc_now_iso()

    if not inbox.exists():
        return []

    ingested: list[str] = []
    for file_path in sorted(inbox.glob("*.json")):
        try:
            with open(file_path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            rejected_path = file_path.with_suffix(".rejected")
            os.replace(file_path, rejected_path)
            continue

        required_keys = {"run_id", "test_id", "category", "tier", "status"}
        if not isinstance(payload, dict) or not required_keys.issubset(payload.keys()):
            rejected_path = file_path.with_suffix(".rejected")
            os.replace(file_path, rejected_path)
            continue

        run_id = payload["run_id"]
        test_id = payload["test_id"]
        category = payload["category"]
        tier = payload["tier"]
        status = payload["status"]

        formation_json = payload.get("formation_json", "{}")
        models_json = payload.get("models_json", "[]")
        bracket_budget_json = payload.get("bracket_budget_json", "{}")
        gate_results_json = payload.get("gate_results_json")
        wall_ms = payload.get("wall_ms")
        tokens_in = payload.get("tokens_in")
        tokens_out = payload.get("tokens_out")
        cost_usd = payload.get("cost_usd")
        escalation_events_json = payload.get("escalation_events_json")
        lab_tournament_id = payload.get("lab_tournament_id")

        try:
            query = """
                INSERT OR IGNORE INTO configtest_runs (
                    run_id, test_id, category, tier, formation_json, models_json,
                    bracket_budget_json, status, gate_results_json, wall_ms,
                    tokens_in, tokens_out, cost_usd, escalation_events_json,
                    lab_tournament_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            connection.execute(
                query,
                (
                    run_id,
                    test_id,
                    category,
                    tier,
                    formation_json,
                    models_json,
                    bracket_budget_json,
                    status,
                    gate_results_json,
                    wall_ms,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    escalation_events_json,
                    lab_tournament_id,
                    now,
                ),
            )
            ingested.append(run_id)
            file_path.unlink()
        except Exception:
            rejected_path = file_path.with_suffix(".rejected")
            os.replace(file_path, rejected_path)
            continue

    return ingested


def _improve_budget_db_path() -> Path:
    """Where the improvement lane's hard-cap ledger lives.

    Day-scoped caps only mean something across ticks if the ledger is DURABLE,
    so this is a real file, not ``:memory:``.
    """
    explicit = os.environ.get(IMPROVE_BUDGET_DB_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return resolve_var_root(
        env_keys=TOKEN_VAR_ENV_KEYS,
        leaf=("improve", "budget.sqlite3"),
    ).expanduser()


def _notify_budget(pool: str, kind: str, fraction: float) -> None:
    """Surface the ledger's 80%-warning and auto-pause to the operator.

    Best-effort by contract: the ledger swallows notifier failures, and a lost
    notification must never change an admission decision.
    """
    try:
        from omniagentos.notifications.service import record_notification

        paused = kind == "pause"
        if paused:
            title = f"Improve lane PAUSED at the ${DAILY_CEILING_USD:,.2f}/day cap"
            body = (
                f"Pool {pool!r} tripped the hard daily ceiling "
                f"({fraction:.0%} committed). No further improvement runs are "
                "admitted until the next UTC day."
            )
        else:
            title = f"Improve lane at {fraction:.0%} of the ${DAILY_CEILING_USD:,.2f}/day cap"
            body = f"Pool {pool!r} crossed the warning threshold; the lane is still running."
        record_notification(
            kind="blocked" if paused else "info",
            title=title,
            body=body,
            severity="critical" if paused else "warning",
            ref_type="improve_budget",
            ref_id=pool,
            payload={
                "pool": pool,
                "event": kind,
                "fraction_used": fraction,
                "ceiling_usd": DAILY_CEILING_USD,
            },
        )
    except Exception:  # noqa: BLE001 - notification must never affect admission
        LOG.debug("improve budget notification failed", exc_info=True)


def _default_budget_ledger() -> BudgetLedger | None:
    """Open the durable day-cap ledger, or ``None`` when it cannot be opened.

    Self-arming on purpose: a caller that forgets to inject a ledger must still
    get the cap, because "built, tested, never wired" is exactly how this guard
    came to protect nothing.
    """
    try:
        path = _improve_budget_db_path()
        return BudgetLedger(path, notifier=_notify_budget)
    except Exception:  # noqa: BLE001 - reported as unenforceable, never crashes the tick
        LOG.error(
            "improve budget ledger path is unavailable or could not be opened",
            exc_info=True,
        )
        return None


@dataclass(frozen=True)
class _WaveDispatch:
    dispatched: list[str]
    budget_blocked: list[str]
    budget_overshot: bool
    budget_unenforceable: bool


def _dispatch_wave(
    connection: sqlite3.Connection,
    wave: Sequence[ImproveTest],
    *,
    now: str,
    ledger: BudgetLedger | None,
) -> _WaveDispatch:
    """Queue one CALL_RESERVED saga row per test the day's budget still admits.

    Admission order is: reserve -> INSERT -> settle. The reservation is taken
    BEFORE the row exists so a refusal costs nothing, and it is settled (or
    released, when the INSERT was a no-op duplicate) so the hold never leaks.
    """
    if not wave:
        return _WaveDispatch([], [], False, False)

    enforced = budget_blocks()
    enforcement = "block" if enforced else "advisory"
    dispatched: list[str] = []
    blocked: list[str] = []
    overshot = False
    unenforceable = False

    if ledger is None:
        unenforceable = True
        LOG.error(
            "improve dispatch: day-cap ledger unavailable; %s (enforcement=%s)",
            "REFUSING the wave" if enforced else "proceeding UNGUARDED",
            enforcement,
        )
        if enforced:
            return _WaveDispatch([], [t.test_id for t in wave], False, True)

    for t in wave:
        attempt_id = f"att_{t.test_id}_{now}"
        idempotency_key = f"improve:{t.test_id}:{now}"

        reservation = None
        if ledger is not None:
            try:
                reservation = ledger.reserve(
                    pool=IMPROVE_BUDGET_POOL,
                    estimated_max_usd=IMPROVE_DISPATCH_ESTIMATE_USD,
                    idempotency_key=idempotency_key,
                )
            except BudgetRefused as refusal:
                overshot = True
                LOG.error(
                    "improve dispatch %s by the $%.2f/day budget guard: test=%s pool=%s "
                    "reason=%s settled=$%.2f outstanding=$%.2f cap=$%.2f enforcement=%s",
                    "REFUSED" if enforced else "OVER CAP but PROCEEDING",
                    ledger.ceiling_usd,
                    t.test_id,
                    refusal.pool,
                    refusal.reason,
                    refusal.settled_usd,
                    refusal.outstanding_usd,
                    refusal.cap_usd,
                    enforcement,
                )
                if enforced:
                    blocked.append(t.test_id)
                    continue
            except BudgetError:
                # A ledger FAULT is not a refusal: it means the cap could not be
                # consulted at all, which is the unenforceable case above.
                unenforceable = True
                LOG.error(
                    "improve dispatch: budget ledger fault for test=%s; %s (enforcement=%s)",
                    t.test_id,
                    "REFUSING the dispatch" if enforced else "proceeding UNGUARDED",
                    enforcement,
                    exc_info=True,
                )
                if enforced:
                    blocked.append(t.test_id)
                    continue

        # The dispatcher must never report dispatched work it did not actually reserve.
        cursor = connection.execute(
            "INSERT OR IGNORE INTO improve_saga (attempt_id, state, idempotency_key, updated_at) "
            "VALUES (?, 'CALL_RESERVED', ?, ?)",
            (attempt_id, idempotency_key, now),
        )
        queued = cursor.rowcount == 1
        if queued:
            dispatched.append(t.test_id)

        if ledger is None or reservation is None or reservation.state != "open":
            # No hold of ours to resolve (unguarded dispatch, or an idempotent
            # replay that returned an already-settled reservation).
            continue
        try:
            if queued:
                # Fail closed: the real cost lands in a worker result file the
                # ledger never sees, so charge the reserved maximum now rather
                # than let the hold expire and forget today's spend.
                ledger.settle(reservation.id, actual_usd=None, usage_available=False)
            else:
                # Nothing was queued (duplicate attempt id) — refund the hold.
                ledger.release(reservation.id)
        except BudgetError:
            unenforceable = True
            LOG.error(
                "improve dispatch: could not %s budget reservation %s for test=%s",
                "settle" if queued else "release",
                reservation.id,
                t.test_id,
                exc_info=True,
            )

    return _WaveDispatch(dispatched, blocked, overshot, unenforceable)


def tick(
    connection: sqlite3.Connection,
    *,
    refs: GitRefs,
    inbox: Path,
    tests: Sequence[ImproveTest] = (),
    completed: AbstractSet[str] = frozenset(),
    env: Mapping[str, str] | None = None,
    now: str | None = None,
    ledger: BudgetLedger | None = None,
) -> dict[str, Any]:
    """One dispatcher tick: reconcile, ingest, then dispatch what the budget admits.

    ``ledger`` is the day-cap seam. Leave it ``None`` in production: the tick
    opens the durable ledger itself (see :func:`_default_budget_ledger`) so the
    cap does not depend on every caller remembering to pass one.
    """
    enforcement = "block" if budget_blocks() else "advisory"
    decision = resolve_improve_mode(connection, env=env, now=now)
    if decision.effective == "off":
        return {
            "mode": "off",
            "requested": decision.requested,
            "reason": decision.reason,
            "approved_by": decision.approved_by,
            "reconciled": [],
            "ingested": [],
            "dispatched": [],
            "blocked_by": None,
            "budget_enforcement": enforcement,
            "budget_blocked": [],
            "budget_overshot": False,
            "budget_unenforceable": False,
        }

    reconciled = reconcile_saga(connection, refs, now=now)
    reconciled_dicts = [dataclasses.asdict(r) for r in reconciled]

    ingested = ingest_worker_results(connection, inbox, now=now)

    blocker = unverified_head(connection)
    if blocker is not None:
        return {
            "mode": decision.effective,
            "requested": decision.requested,
            "reason": decision.reason,
            "approved_by": decision.approved_by,
            "reconciled": reconciled_dicts,
            "ingested": ingested,
            "dispatched": [],
            "blocked_by": blocker,
            "budget_enforcement": enforcement,
            "budget_blocked": [],
            "budget_overshot": False,
            "budget_unenforceable": False,
        }

    wave = schedule_wave(tests, completed=completed)

    budget_blocked: list[str] = []
    budget_overshot = False
    budget_unenforceable = False

    if decision.effective == "on":
        if now is None:
            from omniagentos.contracts import utc_now_iso

            now = utc_now_iso()
        # Only mode "on" spends money; shadow computes a wave and queues nothing,
        # so it must not reserve (and must not pause the ledger for a real lane).
        owned_ledger: BudgetLedger | None = None
        active_ledger = ledger
        if active_ledger is None and wave:
            active_ledger = owned_ledger = _default_budget_ledger()
        try:
            outcome = _dispatch_wave(connection, wave, now=now, ledger=active_ledger)
        finally:
            if owned_ledger is not None:
                owned_ledger.close()
        dispatched = outcome.dispatched
        budget_blocked = outcome.budget_blocked
        budget_overshot = outcome.budget_overshot
        budget_unenforceable = outcome.budget_unenforceable
    else:
        dispatched = [t.test_id for t in wave]

    return {
        "mode": decision.effective,
        "requested": decision.requested,
        "reason": decision.reason,
        "approved_by": decision.approved_by,
        "reconciled": reconciled_dicts,
        "ingested": ingested,
        "dispatched": dispatched,
        "blocked_by": None,
        "budget_enforcement": enforcement,
        "budget_blocked": budget_blocked,
        "budget_overshot": budget_overshot,
        "budget_unenforceable": budget_unenforceable,
    }


def ensure_improve_dispatcher_routine(store: SqliteStore) -> dict[str, Any]:
    from omniagentos.scheduler.routines import (
        IMPROVE_DISPATCHER_ROUTINE_NAME,
        improve_dispatcher_routine,
    )
    from omniagentos.scheduler.store import RoutinesStore

    r_store = RoutinesStore(store)
    existing = r_store.list_routines()
    for r in existing:
        if r.get("name") == IMPROVE_DISPATCHER_ROUTINE_NAME:
            return r

    return r_store.create_routine(improve_dispatcher_routine())


__all__ = [
    "IMPROVE_MODE_ENV",
    "ImproveMode",
    "IMPROVE_MODES",
    "DEFAULT_IMPROVE_MODE",
    "TICK_SECONDS",
    "IMPROVE_APPROVAL_ACTION_CLASS",
    "IMPROVE_BUDGET_POOL",
    "IMPROVE_DISPATCH_ESTIMATE_USD",
    "IMPROVE_BUDGET_DB_ENV",
    "NON_TERMINAL_SAGA_STATES",
    "TERMINAL_SAGA_STATES",
    "UNVERIFIED_HEAD_STATES",
    "parse_improve_mode",
    "improve_mode_requested",
    "human_approval",
    "ModeDecision",
    "resolve_improve_mode",
    "ImproveTest",
    "SequencerError",
    "schedule_wave",
    "sequence_waves",
    "GitRefs",
    "GitCli",
    "SagaReconciliation",
    "reconcile_saga",
    "unverified_head",
    "emit_worker_result",
    "ingest_worker_results",
    "tick",
    "ensure_improve_dispatcher_routine",
]
