"""Completed-run and dead-session board-card reconciliation.

* Completed/terminal runs: clear blocked cards for that run (post-condition:
  zero blocked cards remain for the run) via the guarded transition path that
  emits a board event and completion notification (same seam as board_sweep /
  service reconcile), never a bare unconditional status write.
* Dead sessions: flip the corresponding card under a detect→flip budget that
  is truthful relative to the 300s routines-tick cadence that drives
  ``board_sweep`` (not a false 60s claim).

Gated by ``OMNIAGENTOS_LIFECYCLE_RECONCILE_MODE`` (off → shadow → enforce).
Does not rebuild board_sweep stale-card or approval-hang logic — those remain
the source of truth for their own cases; this module only clears completed-run
blocked cards and flips dead-session cards.

Longhaul and swarm-owned cards are excluded (same ownership contract as
``board_sweep``): their lifecycle managers are authoritative.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from omniagentos.collab.contracts import BoardTaskStatus

__all__ = [
    "DEAD_SESSION_STALE_SECONDS",
    "LIFECYCLE_RECONCILE_ENV",
    "MAX_DETECT_TO_FLIP_SECONDS",
    "ROUTINES_TICK_SECONDS",
    "lifecycle_reconcile_mode",
    "reconcile_run_cards",
]

LOG = logging.getLogger(__name__)

LIFECYCLE_RECONCILE_ENV = "OMNIAGENTOS_LIFECYCLE_RECONCILE_MODE"
ReconcileMode = Literal["off", "shadow", "enforce"]
RECONCILE_MODES: tuple[ReconcileMode, ...] = ("off", "shadow", "enforce")
DEFAULT_MODE: ReconcileMode = "off"

# board_sweep is invoked from routines-tick, whose launchd StartInterval is 300s
# (scripts/scheduler/com.omniagentos.routines.plist.template). A 60s
# detect→flip budget is therefore impossible under the real cadence. Budget =
# one routines interval + staleness threshold so injected-clock tests and
# production semantics stay aligned.
ROUTINES_TICK_SECONDS = 300
DEAD_SESSION_STALE_SECONDS = 45
MAX_DETECT_TO_FLIP_SECONDS = ROUTINES_TICK_SECONDS + DEAD_SESSION_STALE_SECONDS
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_SESSION_STATES = frozenset(
    {"completed", "failed", "cancelled", "terminated", "dead", "error"}
)
TERMINAL_BOARD_STATUSES = frozenset(
    {
        BoardTaskStatus.DONE.value,
        BoardTaskStatus.CANCELLED.value,
    }
)


def lifecycle_reconcile_mode(env: Mapping[str, str] | None = None) -> ReconcileMode:
    source = env if env is not None else os.environ
    raw = str(source.get(LIFECYCLE_RECONCILE_ENV, DEFAULT_MODE)).strip().lower()
    if raw in RECONCILE_MODES:
        return raw  # type: ignore[return-value]
    return DEFAULT_MODE


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_session_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("ses_")


def _is_owned_elsewhere(task: Mapping[str, Any]) -> bool:
    """True when longhaul engine or swarm coordinator owns the card lifecycle.

    Mirrors board_sweep ownership exclusions so this reconciler never fights
    authoritative managers.
    """
    if task.get("lane") == "longhaul":
        return True
    if task.get("swarm_run_id"):
        return True
    return False


def reconcile_run_cards(
    store: Any,
    collab_store: Any,
    *,
    sessions: Mapping[str, Mapping[str, Any]] | None = None,
    runs: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    mode: ReconcileMode | None = None,
    env: Mapping[str, str] | None = None,
    stale_seconds: int = DEAD_SESSION_STALE_SECONDS,
) -> dict[str, Any]:
    """Run one reconcile pass. Idempotent and safe to call repeatedly.

    Returns counts and transition plans. In ``shadow`` mode, computes would-be
    transitions without applying writes.
    """
    resolved_mode = mode if mode is not None else lifecycle_reconcile_mode(env)
    if resolved_mode == "off":
        return {
            "mode": "off",
            "unblocked": 0,
            "flipped": 0,
            "blocked_remaining_for_completed": 0,
            "planned": [],
            "skipped_owned": 0,
        }

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    tasks = list(collab_store.list_board_tasks(archived=0))
    active = [t for t in tasks if t.get("archived_at") is None]

    run_ids = [str(t["run_id"]) for t in active if t.get("run_id")]
    if runs is None:
        runs = _load_runs(store, run_ids)
    session_map = dict(sessions or {})

    planned: list[dict[str, Any]] = []
    unblocked = 0
    flipped = 0
    skipped_owned = 0

    # (a) Completed-run reconcile: clear blocked cards for terminal runs.
    for task in active:
        if _is_owned_elsewhere(task):
            skipped_owned += 1
            continue
        if str(task.get("status")) != BoardTaskStatus.BLOCKED.value:
            continue
        run_id = str(task.get("run_id") or "")
        if not run_id:
            continue
        run = runs.get(run_id)
        if run is None:
            continue
        if str(run.get("state")) not in TERMINAL_RUN_STATES:
            continue
        planned.append(
            {
                "action": "unblock",
                "task_id": str(task["id"]),
                "run_id": run_id,
                "from_status": BoardTaskStatus.BLOCKED.value,
                "to_status": BoardTaskStatus.DONE.value
                if str(run.get("state")) == "completed"
                else BoardTaskStatus.CANCELLED.value,
            }
        )

    # (b) Dead-session flip within the truthful detect→flip budget.
    for task in active:
        if _is_owned_elsewhere(task):
            continue
        status = str(task.get("status") or "")
        if status not in {
            BoardTaskStatus.IN_PROGRESS.value,
            BoardTaskStatus.AWAITING_APPROVAL.value,
            BoardTaskStatus.CLAIMED.value,
            BoardTaskStatus.OPEN.value,
        }:
            continue
        session_id = str(task.get("result_ref") or "")
        if not _is_session_ref(session_id):
            continue
        session = session_map.get(session_id)
        if session is None:
            # Unknown session with a ses_ ref: treat as dead only if task is aged.
            heartbeat = _as_utc(task.get("updated_at"))
        else:
            state = str(session.get("state") or "")
            if state in TERMINAL_SESSION_STATES:
                heartbeat = _as_utc(session.get("heartbeat_at") or session.get("updated_at"))
            else:
                heartbeat = _as_utc(session.get("heartbeat_at") or session.get("updated_at"))
                if heartbeat is not None and (moment - heartbeat) < timedelta(
                    seconds=stale_seconds
                ):
                    continue  # still alive
                if state not in TERMINAL_SESSION_STATES and heartbeat is not None:
                    # live state but heartbeat stale
                    pass
                elif state not in TERMINAL_SESSION_STATES and heartbeat is None:
                    continue
        if heartbeat is not None and (moment - heartbeat) < timedelta(seconds=stale_seconds):
            if (
                session is not None
                and str(session.get("state") or "") not in TERMINAL_SESSION_STATES
            ):
                continue
        # Dead: plan flip to blocked (observable, not archive — archive stays with board_sweep).
        age = None if heartbeat is None else (moment - heartbeat).total_seconds()
        planned.append(
            {
                "action": "dead_session_flip",
                "task_id": str(task["id"]),
                "session_id": session_id,
                "from_status": status,
                "to_status": BoardTaskStatus.BLOCKED.value,
                "age_seconds": age,
            }
        )

    if resolved_mode == "shadow":
        LOG.info(
            "lifecycle reconcile shadow: would apply %d transitions",
            len(planned),
        )
        blocked_remaining = _count_blocked_for_completed(active, runs)
        return {
            "mode": "shadow",
            "unblocked": 0,
            "flipped": 0,
            "blocked_remaining_for_completed": blocked_remaining,
            "planned": planned,
            "skipped_owned": skipped_owned,
        }

    # enforce: apply via guarded path (status check + board event + notification)
    task_index = {str(t.get("id")): t for t in active}
    for item in planned:
        applied = _apply_guarded_transition(collab_store, item, task_index, store=store)
        if not applied:
            continue
        if item["action"] == "unblock":
            unblocked += 1
        elif item["action"] == "dead_session_flip":
            flipped += 1

    # Re-list for post-condition count when possible.
    try:
        post = list(collab_store.list_board_tasks(archived=0))
        blocked_remaining = _count_blocked_for_completed(post, runs)
    except Exception:  # noqa: BLE001
        blocked_remaining = 0

    return {
        "mode": "enforce",
        "unblocked": unblocked,
        "flipped": flipped,
        "blocked_remaining_for_completed": blocked_remaining,
        "planned": planned,
        "skipped_owned": skipped_owned,
    }


def _apply_guarded_transition(
    collab_store: Any,
    item: Mapping[str, Any],
    task_index: Mapping[str, Mapping[str, Any]],
    *,
    store: Any = None,
) -> bool:
    """Apply one transition only when the current status still matches the plan.

    Avoids unconditional status writes that can clobber a live completion that
    landed between plan and apply. Emits board event + completion notification
    on successful unblock→done (same seams as intake service / board_sweep).
    """
    task_id = str(item["task_id"])
    # Prefer a live re-read so concurrent completions between plan and apply
    # are observed (task_index is only a planning snapshot).
    current: Mapping[str, Any] | None = None
    getter = getattr(collab_store, "get_board_task", None)
    if callable(getter):
        current = getter(task_id)
    if current is None:
        current = task_index.get(task_id)
    if current is None:
        return False

    current_status = str(current.get("status") or "")
    expected_from = str(item.get("from_status") or "")
    to_status = str(item.get("to_status") or "")

    # Already at destination (idempotent).
    if current_status == to_status:
        return False
    # Status moved under us (e.g. operator/session completed the card) — do not
    # revert a live completion or other transition.
    if expected_from and current_status != expected_from:
        return False
    if current_status in TERMINAL_BOARD_STATUSES and item.get("action") == "dead_session_flip":
        return False

    fields: dict[str, Any] = {"status": to_status}
    if item.get("action") == "dead_session_flip":
        description = str(current.get("description") or "")
        marker = " [auto-blocked: dead-session]"
        if marker not in description:
            description = description + marker
        fields["description"] = description

    # Compare-and-set on the observed status so a concurrent completion that
    # lands between re-read and write is never overwritten.
    applied = collab_store.update_board_task(
        task_id,
        fields,
        expect_status=current_status,
    )
    if applied is False:
        return False

    run_id = item.get("run_id") or current.get("run_id")
    session_id = item.get("session_id")
    _emit_board_event_safe(
        collab_store,
        task_id,
        run_id=str(run_id) if run_id else None,
        session_id=str(session_id) if session_id else None,
    )
    if item.get("action") == "unblock" and to_status == BoardTaskStatus.DONE.value:
        _notify_completion_safe(
            collab_store,
            task_id,
            run_id=str(run_id) if run_id else None,
            store=store,
        )
    return True


def _emit_board_event_safe(
    collab_store: Any,
    task_id: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> None:
    try:
        from omniagentos.intake.service import _emit_board_event

        _emit_board_event(
            collab_store,
            task_id,
            run_id=run_id,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 — best-effort telemetry
        # Fallback: try the collab store's own event surface if present.
        try:
            h1 = getattr(collab_store, "_store", None)
            if h1 is not None and hasattr(h1, "insert_event"):
                payload: dict[str, Any] = {"task_id": task_id}
                if run_id is not None:
                    payload["run_id"] = run_id
                if session_id is not None:
                    payload["session_id"] = session_id
                h1.insert_event(
                    "board.updated",
                    "intake",
                    "board.reconciled",
                    target_type="board_task",
                    target_id=task_id,
                    payload=payload,
                )
        except Exception:  # noqa: BLE001
            LOG.debug("lifecycle reconcile board event failed", exc_info=True)


def _store_db_path(store: Any) -> str | None:
    """Resolve the filesystem path of a SqliteStore (or composed wrapper).

    Reads the real ``SqliteStore._db_path`` field, while accepting a public
    ``db_path`` accessor from compatible stores, then walks one composition
    layer (``_store``) used by ConversationStore / CollabStore-style wrappers.
    Never invents a default.
    """
    if store is None:
        return None
    for attr in ("_db_path", "db_path"):
        value = getattr(store, attr, None)
        if isinstance(value, str) and value:
            return value
        # Property may return Path-like.
        if value is not None and not callable(value):
            text = str(value)
            if text:
                return text
    inner = getattr(store, "_store", None)
    if inner is not None and inner is not store:
        return _store_db_path(inner)
    return None


def _notify_completion_safe(
    collab_store: Any,
    task_id: str,
    *,
    run_id: str | None,
    store: Any = None,
) -> None:
    try:
        # Reuse the established reconcile notification path, including its
        # kind-aware dedupe and default-DB handling.
        from omniagentos.intake.service import _notify_task_done_safe

        db_path = _store_db_path(store)
        _notify_task_done_safe(
            collab_store,
            task_id,
            workspace=None,
            run_id=run_id,
            session_id=None,
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001
        LOG.debug("lifecycle reconcile completion notification failed", exc_info=True)


def _count_blocked_for_completed(
    tasks: list[Mapping[str, Any]],
    runs: Mapping[str, Mapping[str, Any]],
) -> int:
    count = 0
    for task in tasks:
        if _is_owned_elsewhere(task):
            continue
        if str(task.get("status")) != BoardTaskStatus.BLOCKED.value:
            continue
        run_id = str(task.get("run_id") or "")
        run = runs.get(run_id)
        if run is not None and str(run.get("state")) in TERMINAL_RUN_STATES:
            count += 1
    return count


def _load_runs(store: Any, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(run_ids))
    if not unique:
        return {}
    connection = getattr(store, "_connection", None)
    if connection is not None:
        placeholders = ",".join("?" for _ in unique)
        rows = connection.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders})", unique
        ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for run_id in unique:
        getter = getattr(store, "get_run", None)
        if callable(getter):
            run = getter(run_id)
            if run is not None:
                result[run_id] = run
    return result
