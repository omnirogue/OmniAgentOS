"""Low-cost hygiene sweep for stale collaboration-board cards.

Done cards use the terminal threshold; blocked and cancelled cards use the
faster failed threshold.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import Store, default_db_path
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.service import (
    _claim_reconcile_stale_check,
    _emit_board_event,
    _orchestration_stale_minutes,
    _sqlite_db_path,
    pause_board_task_work,
)
from omniagentos.sessions.dal import TERMINAL_SESSION_STATES, SessionsDal

LOG = logging.getLogger(__name__)

STALE_MARKER = " [auto-blocked: stale — no live worker]"


def _env_hours(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        LOG.warning("invalid %s; using default %s", name, default)
        return default


TERMINAL_HOURS = _env_hours("OMNIAGENTOS_BOARD_SWEEP_TERMINAL_HOURS", 48)
FAILED_HOURS = _env_hours("OMNIAGENTOS_BOARD_SWEEP_FAILED_HOURS", 4)
ORPHAN_MINUTES = _env_hours("OMNIAGENTOS_BOARD_SWEEP_ORPHAN_MINUTES", 60)
STALE_HOURS = _env_hours("OMNIAGENTOS_BOARD_SWEEP_STALE_HOURS", 24)

# NSG-025 residual: goal-level limbo detector. the operator's real work flows through
# board_tasks/goals, not the loopqueue -- the loopqueue no-drain alert
# (pipeline/bridge/integrity.py) has no counterpart here, so a card can sit
# open/blocked/awaiting_approval forever with no signal. `_env_hours` is
# generically an int-env-var reader despite the name; reused rather than
# duplicated for a day-granularity knob.
GOAL_LIMBO_DAYS = _env_hours("OMNIAGENTOS_GOAL_LIMBO_DAYS", 7)

# Statuses a card can be silently abandoned in. PENDING/CLAIMED are
# transient triage states owned elsewhere; DONE/CANCELLED are terminal and
# already covered by the terminal-archive sweep above.
LIMBO_STATUSES: tuple[str, ...] = (
    BoardTaskStatus.OPEN.value,
    BoardTaskStatus.BLOCKED.value,
    BoardTaskStatus.AWAITING_APPROVAL.value,
)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _older_than(task: dict[str, Any], cutoff: datetime) -> bool:
    updated_at = _as_utc(task.get("updated_at"))
    return updated_at is not None and updated_at < cutoff


def _run_map(store: Store, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch linked runs in one SQLite query, with a safe store-seam fallback."""
    unique = list(dict.fromkeys(run_ids))
    if not unique:
        return {}
    # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
    connection = getattr(store, "_connection", None)
    if connection is not None:
        placeholders = ",".join("?" for _ in unique)
        rows = connection.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders})", unique
        ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for run_id in unique:
        run = store.get_run(run_id)
        if run is not None:
            result[run_id] = run
    return result


def _pending_run_ids(store: Store, run_ids: list[str]) -> set[str]:
    unique = list(dict.fromkeys(run_ids))
    if not unique:
        return set()
    # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
    connection = getattr(store, "_connection", None)
    if connection is not None:
        placeholders = ",".join("?" for _ in unique)
        rows = connection.execute(
            f"SELECT DISTINCT run_id FROM approvals WHERE state = 'pending' "
            f"AND run_id IN ({placeholders})",
            unique,
        ).fetchall()
        return {str(row["run_id"]) for row in rows if row["run_id"]}
    return {
        str(approval["run_id"])
        for approval in store.list_approvals("pending", limit=10_000)
        if approval.get("run_id") in unique
    }


def _archive(
    store: Store,
    collab_store: Any,
    task: dict[str, Any],
    *,
    sessions_dal: Any,
    now_iso: str,
) -> None:
    pause_board_task_work(store, task, sessions_dal=sessions_dal)
    collab_store.update_board_task(str(task["id"]), {"archived_at": now_iso})
    _emit_board_event(collab_store, str(task["id"]))


def _is_session_ref(value: Any) -> bool:
    """Check if a value is a session reference."""
    return isinstance(value, str) and value.startswith("ses_")


def _is_orchestration_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("orch_")


def sweep_board(
    store: Store,
    collab_store: Any,
    sessions_dal: Any = None,
    now: datetime | None = None,
    orchestrations_dal: Any = None,
) -> dict[str, int]:
    """Sweep and archive stale cards. Returns counts: ``{"archived": N, "blocked": N}``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    now_iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    owns_sessions_dal = sessions_dal is None
    owns_orchestrations_dal = orchestrations_dal is None
    db_path = _sqlite_db_path(store)
    if sessions_dal is None:
        sessions_dal = SessionsDal(default_db_path())
    if orchestrations_dal is None:
        orchestrations_dal = OrchestrationsDal(db_path)

    try:
        # One board query plus batched run/session/approval lookups keeps this
        # suitable for the five-minute fresh-process scheduler tick.
        #
        # archived=0 (not None): the sweep only ever looks at `active`, so
        # fetching archived rows too was pure waste that GREW FOREVER — at 200
        # concurrent agents the archive is the fastest-growing table in the
        # database and this ran every tick. archived=0 pushes the filter into
        # SQL, where idx_board_tasks_archived_created (migration 070) serves it:
        #   SEARCH board_tasks USING INDEX idx_board_tasks_archived_created
        # instead of a full scan. `active` is unchanged by construction — the
        # predicate is identical, it just runs in SQLite now.
        tasks = collab_store.list_board_tasks(archived=0)
        active = [task for task in tasks if task.get("archived_at") is None]
        run_ids = [str(task["run_id"]) for task in active if task.get("run_id")]
        session_ids = [
            str(task["result_ref"]) for task in active if _is_session_ref(task.get("result_ref"))
        ]
        orchestration_ids = [
            str(task["result_ref"])
            for task in active
            if _is_orchestration_ref(task.get("result_ref"))
        ]
        runs = _run_map(store, run_ids)
        sessions = sessions_dal.get_sessions_by_ids(session_ids) if session_ids else {}
        # Shared claim (A5): board reads (reconcile_board), this routines-tick
        # sweep, and the session supervisor's own poll loop all race the SAME
        # 30s-throttled claim keyed by db_path, so whichever gets there first in
        # a window runs the sweep and the other two no-op instead of duplicating it.
        if _claim_reconcile_stale_check(db_path):
            orchestrations_dal.mark_stale_failed(stale_minutes=_orchestration_stale_minutes())
        orchestrations = (
            orchestrations_dal.get_by_ids(orchestration_ids) if orchestration_ids else {}
        )
        pending_runs = _pending_run_ids(store, run_ids)

        terminal_cutoff = moment - timedelta(hours=TERMINAL_HOURS)
        failed_cutoff = moment - timedelta(hours=FAILED_HOURS)
        orphan_cutoff = moment - timedelta(minutes=ORPHAN_MINUTES)
        stale_cutoff = moment - timedelta(hours=STALE_HOURS)
        archived = blocked = 0

        for task in active:
            # Longhaul lifecycle is owned elsewhere: the A2 reaper reaps idle
            # longhaul sessions under the per-lane idle_minutes override, and the
            # engine (sole respawn owner) handles cancel/archive/respawn.  A generic
            # stale sweep must never archive or block a parked continuity chain.
            if task.get("lane") == "longhaul":
                continue
            # Swarm cards are owned by their run's coordinator (same contract):
            # the sweep must not archive/block tasks waiting on DAG dependencies.
            if task.get("swarm_run_id"):
                continue
            run_id = str(task.get("run_id") or "")
            session_id = str(task.get("result_ref") or "")
            if run_id and run_id in pending_runs:
                continue

            run = runs.get(run_id)
            live_run = run is not None and str(run.get("state")) not in {
                "completed",
                "failed",
                "cancelled",
            }
            session = sessions.get(session_id)
            live_session = session is not None and str(session.get("state")) not in {
                state.value for state in TERMINAL_SESSION_STATES
            }
            orchestration = orchestrations.get(session_id)
            live_orchestration = orchestration is not None and str(
                orchestration.get("status")
            ) not in {"completed", "failed", "cancelled"}
            status = str(task.get("status"))

            is_terminal = status in {BoardTaskStatus.DONE.value, BoardTaskStatus.CANCELLED.value}
            terminal_age_cutoff = (
                failed_cutoff if status == BoardTaskStatus.CANCELLED.value else terminal_cutoff
            )
            if (
                is_terminal
                and _older_than(task, terminal_age_cutoff)
                and not live_run
                and not live_session
                and not live_orchestration
            ):
                _archive(store, collab_store, task, sessions_dal=sessions_dal, now_iso=now_iso)
                archived += 1
                continue

            if (
                status == BoardTaskStatus.BLOCKED.value
                and _older_than(task, failed_cutoff)
                and not live_run
                and not live_session
                and not live_orchestration
            ):
                _archive(store, collab_store, task, sessions_dal=sessions_dal, now_iso=now_iso)
                archived += 1
                continue

            if (
                run_id
                and run is None
                and not live_session
                and not live_run
                and not live_orchestration
                and _older_than(task, orphan_cutoff)
            ):
                _archive(store, collab_store, task, sessions_dal=sessions_dal, now_iso=now_iso)
                archived += 1
                continue

            if (
                _is_orchestration_ref(session_id)
                and orchestration is None
                and not live_session
                and not live_run
                and _older_than(task, orphan_cutoff)
            ):
                _archive(store, collab_store, task, sessions_dal=sessions_dal, now_iso=now_iso)
                archived += 1
                continue

            if (
                status
                in {
                    BoardTaskStatus.IN_PROGRESS.value,
                    # A card parked on a human decision whose session has since died
                    # is just as stale as an in_progress one -- the guards below
                    # (no live run/session/orchestration) are what actually decide.
                    BoardTaskStatus.AWAITING_APPROVAL.value,
                }
                and not live_run
                and not live_session
                and not live_orchestration
                and _older_than(task, stale_cutoff)
            ):
                description = str(task.get("description") or "")
                if STALE_MARKER not in description:
                    description += STALE_MARKER
                collab_store.update_board_task(
                    str(task["id"]),
                    {"status": BoardTaskStatus.BLOCKED.value, "description": description},
                )
                _emit_board_event(collab_store, str(task["id"]))
                blocked += 1

        # Lifecycle reconcile (completed-run unblock + dead-session flip).
        # Flag-gated; shadow computes without writes. Does not replace the
        # stale-card / approval-hang logic above — only extends the sweep.
        # When the flag is off the return shape stays {archived, blocked} so
        # existing callers/tests remain byte-compatible.
        #
        # Dead-session budget is owned by run_card_reconcile and is truthful
        # relative to this routines-tick cadence (StartInterval 300s): worst-case
        # detect→flip is ROUTINES_TICK_SECONDS + DEAD_SESSION_STALE_SECONDS, not
        # a false 60s claim. Ownership exclusions (longhaul / swarm_run_id) are
        # applied inside reconcile_run_cards so this path never fights
        # authoritative lifecycle managers.
        report: dict[str, Any] = {"archived": archived, "blocked": blocked}
        try:
            from omniagentos.intake.run_card_reconcile import (
                lifecycle_reconcile_mode,
                reconcile_run_cards,
            )

            if lifecycle_reconcile_mode() != "off":
                session_ids_for_reconcile = [
                    str(task["result_ref"])
                    for task in active
                    if _is_session_ref(task.get("result_ref"))
                ]
                reconcile_sessions = (
                    sessions_dal.get_sessions_by_ids(session_ids_for_reconcile)
                    if session_ids_for_reconcile
                    else {}
                )
                report["reconcile"] = reconcile_run_cards(
                    store,
                    collab_store,
                    sessions=reconcile_sessions,
                    runs=runs,
                    now=moment,
                )
        except Exception:  # noqa: BLE001 — never break the existing sweep
            LOG.debug("lifecycle reconcile skipped", exc_info=True)

        return report
    finally:
        if owns_sessions_dal:
            sessions_dal.close()
        if owns_orchestrations_dal:
            orchestrations_dal.close()


def goal_limbo_candidates(
    collab_store: Any,
    *,
    now: datetime | None = None,
    limbo_days: int = GOAL_LIMBO_DAYS,
) -> list[dict[str, Any]]:
    """Cards in open/blocked/awaiting_approval untouched for ``limbo_days``.

    ``updated_at`` is the durable signal: every state transition,
    description/evidence edit, and status flip goes through
    ``collab_store.update_board_task`` (or the CAS claim/verify paths), and
    ALL of those bump the SAME column -- so staleness of ``updated_at`` is
    staleness of "no state transition, evidence attachment, or session
    touch" combined, not just of status, without needing a second read.
    The code still refuses to TRUST that always-present claim structurally:
    a row whose ``updated_at`` is missing or unparseable is surfaced as
    limbo with ``updated_at_unknown=True`` (never skipped as fresh) --
    an instrument problem must not read as a healthy card.

    Excludes the same lifecycle-owned lanes ``sweep_board`` excludes
    (longhaul/swarm) so this detector never fights an authoritative owner
    that intentionally parks a card.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = moment - timedelta(days=max(0, limbo_days))
    tasks = collab_store.list_board_tasks(archived=0, statuses=list(LIMBO_STATUSES))
    out: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("archived_at"):
            continue
        if task.get("lane") == "longhaul":
            continue
        if task.get("swarm_run_id"):
            continue
        updated_at = _as_utc(task.get("updated_at"))
        if updated_at is None:
            # C-MAJ-001: a card whose updated_at is missing, empty, or
            # unparseable cannot prove itself fresh. Skipping it here would
            # read an instrument problem as a healthy card (favourable
            # absence) and let auto-resolve falsely recover an open limbo
            # alert — surface it as limbo with the problem named instead.
            annotated = dict(task)
            annotated["updated_at_unknown"] = True
            out.append(annotated)
            continue
        if updated_at >= cutoff:
            continue
        out.append(task)
    return out


def limbo_recommended_action(task: dict[str, Any]) -> str:
    """The estate "recommended-action rule": every human-facing alert names one.

    Single source of truth (imported by ``steward.alerts.rules.goal_limbo``)
    so the recommendation logic is never duplicated between the candidate
    selector and the alert shaper -- one clone family instead of two.
    """
    status = str(task.get("status") or "")
    blocked_reason = str(task.get("blocked_reason") or "").strip()
    owner = task.get("owner_employee_id")
    if status == BoardTaskStatus.BLOCKED.value:
        if not blocked_reason:
            return (
                "unblock: blocked_reason is empty — supply the blocker or move "
                "the card back to open"
            )
        return f"unblock-hint: resolve blocker — {blocked_reason[:200]}"
    if status == BoardTaskStatus.AWAITING_APPROVAL.value:
        return "escalate: needs a human approval decision"
    if not owner:
        return "reassign: no owner assigned — assign an owner or close if stale"
    return "escalate: review with owner; close if superseded"


__all__ = [
    "GOAL_LIMBO_DAYS",
    "LIMBO_STATUSES",
    "STALE_MARKER",
    "goal_limbo_candidates",
    "limbo_recommended_action",
    "sweep_board",
]
