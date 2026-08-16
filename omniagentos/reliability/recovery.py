"""Safe transient-failure recovery dispatch (§5b.5).

Allow-listed per class: only RATE_LIMIT and TIMEOUT recover, and only with a bounded
cap of one requeue per event. Recovery is HONEST: it never reports success without a
durable record. A transient run failure is recovered by minting a follow-up run through
the sanctioned in-process service layer (``create_task_service`` + ``create_run_service``,
the same D-001 path the suggestions-approve flow uses); anything else is escalated to the
improvement pipeline (status ``proposed``) with the reason recorded. Nothing destructive;
money/delete/auth classes are never touched. CAS via ``claim_recovery()`` ensures exactly
one worker acts per event.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityEvent, ReliabilityStore
from omniagentos.reliability.taxonomy import EventStatus, FailureClass

# Allow-listed classes for safe recovery.
_RECOVERABLE_CLASSES = {
    FailureClass.RATE_LIMIT.value,
    FailureClass.TIMEOUT.value,
}

# A single requeue per originating run — never a runaway retry loop.
_RECOVERY_CAP = 1

# A requeue action: (store, event) -> {"ok": bool, "run_id"?: str, "reason"?: str}.
RequeueFn = Callable[[ReliabilityStore, ReliabilityEvent], dict[str, Any]]

# A stranded-approval re-drive pass: (store) -> redrive summary (H-07).
RedriveFn = Callable[[ReliabilityStore], dict[str, Any]]


def _base_store(store: ReliabilityStore) -> Any | None:
    """A base ``SqliteStore`` over the SAME db file as the reliability store.

    Reliability writes use ``insert_reliability_event``; the base SSE feed still
    uses ``insert_event``. ``create_run_service`` emits over the base ``events``
    feed, so the run-enqueue service layer still prefers a base store handle when
    available. Both stores point at one WAL db file, so the follow-up run is
    visible to everyone.
    """
    conn = getattr(store, "_connection", None)
    if conn is None:
        return None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:  # noqa: BLE001
        return None
    path: str | None = None
    for r in rows:
        try:
            name = r["name"] if hasattr(r, "keys") else r[1]
            file = r["file"] if hasattr(r, "keys") else r[2]
        except (KeyError, IndexError, TypeError):
            continue
        if name == "main" and file:
            path = file
            break
    if not path:
        return None
    from omniagentos.db.store import SqliteStore

    return SqliteStore(path)


def _default_requeue(store: ReliabilityStore, event: ReliabilityEvent) -> dict[str, Any]:
    """Mint a follow-up run for a transient run failure via the sanctioned service layer.

    Honest and bounded: it re-enqueues the failed run's work exactly once, and only
    when the originating run has not already been retried. Any failure to construct
    the follow-up returns ``{"ok": False, "reason": ...}`` — never a false success.
    """
    if event.ref_type != "run" or not event.ref_id:
        return {"ok": False, "reason": "no_run_ref"}

    try:
        run = store.get_run(event.ref_id)  # inherited from SqliteStore
    except Exception as exc:  # noqa: BLE001 - lookup failure is not a safe action
        return {"ok": False, "reason": f"run_lookup_failed:{exc}"}
    if run is None:
        return {"ok": False, "reason": "run_not_found"}

    # retry_count 0 gate: attempt 1 is the first try; anything higher was already retried.
    try:
        attempt = int(run.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    if attempt > 1:
        return {"ok": False, "reason": "retry_cap_reached"}

    base = _base_store(store)
    if base is None:
        return {"ok": False, "reason": "no_base_store"}
    try:
        from omniagentos.api.services import create_run_service, create_task_service
        from omniagentos.policy import load_policy

        policy = load_policy()
        harness_params = run.get("harness_params")
        if isinstance(harness_params, str):
            try:
                harness_params = json.loads(harness_params or "{}")
            except ValueError:
                harness_params = {}
        prompt = (harness_params or {}).get("prompt") if isinstance(harness_params, dict) else None

        task = create_task_service(
            base,
            policy,
            title=f"[recovery] requeue {event.failure_class} run {event.ref_id}",
            discipline_id=run.get("discipline_id"),
            input={"recovery_of_run": event.ref_id, "reliability_event": event.id},
        )
        new_run = create_run_service(
            base,
            policy,
            task_id=task["id"],
            harness=run.get("harness") or "cli-claude",
            model=run.get("model"),
            prompt=prompt,
        )
        return {"ok": True, "run_id": new_run.get("id"), "task_id": task.get("id")}
    except Exception as exc:  # noqa: BLE001 - never crash recovery; record and escalate
        return {"ok": False, "reason": f"requeue_failed:{exc}"}
    finally:
        conn = getattr(base, "_connection", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def attempt_recovery(
    store: ReliabilityStore,
    event: ReliabilityEvent,
    *,
    requeue_fn: RequeueFn | None = None,
) -> bool:
    """Attempt safe, honest recovery for one transient failure event.

    Flow:
      1. Reject non-allow-listed classes (no claim, event stays ``open``).
      2. CAS ``claim_recovery(event.id)``: ``open → recovering`` (exactly one worker wins).
      3. Take the safe action (requeue). On success, persist the action and move the
         event to ``recovered``. On no-safe-action, persist ``{"no_safe_action": reason}``
         and escalate the event to ``proposed`` so the improvement pipeline picks it up.

    Returns:
        True ONLY when an action succeeded and was durably recorded; False otherwise.
        Never returns True without a persisted ``recovery_json`` record (codex #4).
    """
    if event.failure_class not in _RECOVERABLE_CLASSES:
        return False

    try:
        claimed = store.claim_recovery(event.id)
    except Exception:  # noqa: BLE001 - CAS unavailable ⇒ not recoverable this cycle
        return False
    if not claimed:
        # Another worker already claimed this event (or it left 'open').
        return False

    now = utc_now_iso()
    requeue = requeue_fn or _default_requeue
    try:
        result = requeue(store, event)
    except Exception as exc:  # noqa: BLE001 - a broken action is a no-safe-action, not a crash
        result = {"ok": False, "reason": f"requeue_error:{exc}"}

    if isinstance(result, dict) and result.get("ok"):
        store.update_event_recovery(
            event.id,
            recovery_json={
                "action": "requeue",
                "recovery_count": _RECOVERY_CAP,
                "new_run_id": result.get("run_id"),
                "reason": event.failure_class,
                "recovered_at": now,
            },
            status=EventStatus.RECOVERED.value,
            actor="recovery",
        )
        return True

    reason = result.get("reason", "unknown") if isinstance(result, dict) else "unknown"
    store.update_event_recovery(
        event.id,
        recovery_json={
            "no_safe_action": reason,
            "attempted_at": now,
            "recovery_count": 0,
        },
        status=EventStatus.PROPOSED.value,
        actor="recovery",
    )
    return False


def _redrive_stranded_approvals(
    store: ReliabilityStore,
    redrive_fn: RedriveFn | None,
) -> dict[str, Any]:
    """H-07: re-drive approved rows stranded by spawn failure, worker death, or restart.

    This is the PRODUCTION hook. The launchd ``watch`` loop (every 600 s) and the
    ``recover`` command both run a recovery cycle, so a stranded approval is re-driven
    on a real schedule — the manual ``redrive`` command is an operator convenience, not
    the only path. A broken re-drive is recorded and never aborts recovery, which must
    still report its own outcome honestly.
    """
    fn = redrive_fn
    if fn is None:
        from omniagentos.reliability.worker_drive import run_redrive_cycle

        fn = run_redrive_cycle
    try:
        return fn(store)
    except Exception as exc:  # noqa: BLE001 - a failed re-drive is evidence, not a crash
        return {"error": f"redrive_failed:{exc}"}


def run_recovery_cycle(
    store: ReliabilityStore,
    *,
    requeue_fn: RequeueFn | None = None,
    redrive_fn: RedriveFn | None = None,
) -> dict[str, Any]:
    """Run one cycle of recovery over open transient events.

    Scans open events, attempts honest recovery on the recoverable classes, then
    re-drives improvements stranded in ``approved`` (H-07), and returns a summary.
    Every recoverable event ends the cycle with a durable ``recovery_json`` record:
    ``recovered`` (action succeeded) or ``proposed`` (escalated with a reason).

    Returns:
        {recovered_count, proposed_count, skipped_count, error_count, redrive}
    """
    summary: dict[str, Any] = {
        "recovered_count": 0,
        "proposed_count": 0,
        "skipped_count": 0,
        "error_count": 0,
    }

    for event in store.list_events(status=EventStatus.OPEN.value, limit=100):
        if event.failure_class not in _RECOVERABLE_CLASSES:
            summary["skipped_count"] += 1
            continue
        try:
            if attempt_recovery(store, event, requeue_fn=requeue_fn):
                summary["recovered_count"] += 1
            else:
                summary["proposed_count"] += 1
        except Exception:  # noqa: BLE001 - one event never aborts the cycle
            summary["error_count"] += 1

    summary["redrive"] = _redrive_stranded_approvals(store, redrive_fn)
    return summary
