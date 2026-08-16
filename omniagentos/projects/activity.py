"""A project's live progress, aggregated from existing runs/steps/events (+
ledger) -- and that same activity projected into a per-project human-readable
log on disk.

No new storage: everything read here comes from tables migrations 001/014/016
already define (runs, steps, events, tasks.project_id, approvals). The only
writes this module makes are plain UTF-8 text lines under
``var/projects/<project_id>/logs/activity.log`` -- a "thin projector" the
``GET /api/projects/{id}/activity`` route (omniagentos.api.routes.projects)
calls best-effort on every read, and that :mod:`omniagentos.projects.activity_tick`
can also run standalone/periodically. The events table (and the ledger's
terminal manifests) stay the single source of truth; the log file is a
convenience mirror a human can ``tail -f`` without opening the dashboard, and
it self-heals if deleted (see :func:`_last_projected_event_id`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore

LOG = logging.getLogger(__name__)

# Step statuses counted as "done" for progress purposes -- mirrors
# intake.service._enrich_board_row's steps_done/steps_total convention.
_DONE_STEP_STATUSES = frozenset({"completed", "skipped"})

_STEP_FIELDS = (
    "id",
    "run_id",
    "seq",
    "name",
    "action_class",
    "status",
    "error",
    "started_at",
    "finished_at",
)
_RUN_FIELDS = (
    "id",
    "task_id",
    "state",
    "harness",
    "arm",
    "model",
    "agent",
    "worker_id",
    "queued_at",
    "started_at",
    "finished_at",
    "cost_usd",
    "error",
)
_TASK_FIELDS = ("id", "title", "state", "risk", "discipline_id", "created_at", "updated_at")


# ---------------------------------------------------------------------------
# Aggregation (read path: GET /api/projects/{id}/activity)
# ---------------------------------------------------------------------------


def _row_subset(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    if isinstance(raw, dict):
        return raw
    raw = event.get("payload_json")
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _event_message(event: dict[str, Any], *, step_name: str | None = None) -> str:
    """One concise human phrase for an events-table row.

    No task/run prefix -- callers that merge events across runs
    (:func:`_project_activity_entries`) add their own context label.
    """
    event_type = str(event.get("type") or "")
    action = str(event.get("action") or "").strip()
    payload = _event_payload(event)
    if event_type == "run.updated":
        return f"run {action or 'updated'}".replace("_", " ")
    if event_type == "step.updated":
        seq = payload.get("seq")
        seq_part = f"step {seq}" if seq is not None else "step"
        name_part = f" ({step_name})" if step_name else ""
        return f"{seq_part}{name_part} {action or 'updated'}"
    if event_type == "task.updated":
        return f"task {action or 'updated'}".replace("_", " ")
    if event_type == "approval.requested":
        action_class = payload.get("action_class")
        proposed = payload.get("proposed_action")
        tag = f" ({action_class})" if action_class else ""
        detail = f": {proposed}" if proposed else ""
        return f"approval requested{tag}{detail}"
    if event_type == "approval.decided":
        return f"approval {payload.get('state') or 'decided'}"
    if event_type == "audit.event":
        return f"note: {action or 'event'}"
    return action or event_type or "event"


def _current_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    started = [step for step in steps if step.get("status") == "started"]
    if not started:
        return None
    chosen = max(started, key=lambda step: int(step.get("seq") or 0))
    return _row_subset(chosen, _STEP_FIELDS)


def _activity_line(event: dict[str, Any], step_name_by_seq: dict[int, str]) -> dict[str, Any]:
    payload = _event_payload(event)
    step_name = None
    if str(event.get("type")) == "step.updated":
        seq = payload.get("seq")
        if isinstance(seq, int):
            step_name = step_name_by_seq.get(seq)
    return {
        "id": event.get("id"),
        "ts": event.get("ts"),
        "line": _event_message(event, step_name=step_name),
    }


def _run_view(
    store: SqliteStore, run: dict[str, Any], steps: list[dict[str, Any]], log_tail_limit: int
) -> dict[str, Any]:
    run_id = str(run["id"])
    step_name_by_seq = {
        int(step["seq"]): str(step.get("name") or "")
        for step in steps
        if step.get("seq") is not None
    }
    # get_events_for_run returns its bounded window oldest->newest; reversed
    # here so log_tail matches the endpoint's newest-first contract.
    tail = list(reversed(store.get_events_for_run(run_id, limit=log_tail_limit)))
    return {
        **_row_subset(run, _RUN_FIELDS),
        "steps": [_row_subset(step, _STEP_FIELDS) for step in steps],
        "steps_done": sum(1 for step in steps if step.get("status") in _DONE_STEP_STATUSES),
        "steps_total": len(steps),
        "current_step": _current_step(steps),
        "log_tail": [_activity_line(event, step_name_by_seq) for event in tail],
    }


def _run_summary_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"runs": len(runs), "running": 0, "awaiting_approval": 0, "completed": 0, "failed": 0}
    for run in runs:
        state = str(run.get("state") or "")
        if state == "running":
            counts["running"] += 1
        elif state == "awaiting_approval":
            counts["awaiting_approval"] += 1
        elif state == "completed":
            counts["completed"] += 1
        elif state in {"failed", "cancelled"}:
            counts["failed"] += 1
    return counts


def _project_activity_entries(
    events: list[dict[str, Any]],
    store: SqliteStore,
    task_by_id: dict[str, dict[str, Any]],
    run_by_id: dict[str, dict[str, Any]],
    steps_cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Merge run+approval events across a project into one labeled, ordered feed."""
    entries: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        target_type = str(event.get("target_type") or "")
        run_id: str | None
        if target_type == "run":
            run_id = str(event.get("target_id") or "") or None
        elif target_type == "approval":
            # approval.decided (omniagentos.api.routes.control.decide_approval)
            # carries run_id in its payload -- the event row itself doesn't.
            run_id = str(payload.get("run_id") or "") or None
        else:
            run_id = None

        run_row = run_by_id.get(run_id) if run_id else None
        task_id = str(run_row["task_id"]) if run_row else None
        task = task_by_id.get(task_id) if task_id else None

        step_name = None
        if run_id and str(event.get("type")) == "step.updated":
            seq = payload.get("seq")
            if isinstance(seq, int):
                steps = steps_cache.get(run_id)
                if steps is None:
                    steps = store.get_steps(run_id)
                    steps_cache[run_id] = steps
                step_name = next(
                    (str(step.get("name")) for step in steps if step.get("seq") == seq), None
                )

        if task is not None:
            label = str(task.get("title") or task_id)
        elif run_id:
            label = f"run {run_id[:12]}"
        else:
            label = "project"

        entries.append(
            {
                "id": event.get("id"),
                "ts": event.get("ts"),
                "task_id": task_id,
                "run_id": run_id,
                "line": f"{label}: {_event_message(event, step_name=step_name)}",
            }
        )
    return entries


def build_project_activity(
    store: SqliteStore,
    project_id: str,
    *,
    tasks_limit: int = 100,
    runs_limit: int = 50,
    events_limit: int = 100,
    run_log_tail: int = 8,
) -> dict[str, Any]:
    """A project's live progress: tasks -> runs -> steps, newest-first, bounded.

    Pure read, no side effects (the on-disk human log is a separate, explicit
    step -- see :func:`project_pending_activity`). Does NOT check that
    `project_id` exists; callers (the API route) do that so a missing project
    is a clean 404 rather than a degenerate empty payload.
    """
    tasks = store.list_tasks_for_project(project_id, limit=tasks_limit)
    task_by_id = {str(task["id"]): task for task in tasks}

    runs = store.list_runs_for_project(project_id, {}, limit=runs_limit)
    run_by_id = {str(run["id"]): run for run in runs}
    runs_by_task: dict[str, list[dict[str, Any]]] = {}
    steps_cache: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        run_id = str(run["id"])
        steps_cache[run_id] = store.get_steps(run_id)
        runs_by_task.setdefault(str(run["task_id"]), []).append(run)

    # list_tasks_for_project/list_runs_for_project already return newest-first
    # (updated_at / queued_at DESC); grouping preserves that relative order.
    task_rows = [
        {
            **_row_subset(task, _TASK_FIELDS),
            "runs": [
                _run_view(store, run, steps_cache[str(run["id"])], run_log_tail)
                for run in runs_by_task.get(str(task["id"]), [])
            ],
        }
        for task in tasks
    ]

    events = store.list_events_for_project(project_id, limit=events_limit)
    activity_log = _project_activity_entries(events, store, task_by_id, run_by_id, steps_cache)

    return {
        "project_id": project_id,
        "generated_at": utc_now_iso(),
        "tasks": task_rows,
        "activity_log": activity_log,
        "summary": {"tasks": len(tasks), **_run_summary_counts(runs)},
    }


# ---------------------------------------------------------------------------
# On-disk human log projector: var/projects/<project_id>/logs/activity.log
# ---------------------------------------------------------------------------


def _var_root() -> str:
    """The same var/ root the ledger/vault/db defaults resolve to.

    ``OMNIAGENTOS_VAR_DIR`` wins when set -- the same knob
    :mod:`omniagentos.adapters.common` and :mod:`omniagentos.intake.service`
    already anchor their per-run logs/workspaces to. Otherwise the repo root's
    ``var/`` dir, computed locally via ``omniagentos.__file__`` (NOT
    ``contracts._repo_root``, which is private and contracts.py is
    frozen/lead-owned) -- resolves to the same repo root regardless of process
    cwd (council INT-002/OPS-005 reasoning contracts.py already documents).
    """
    override = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if override:
        return override
    import omniagentos

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
    return os.path.join(repo_root, "var")


def _safe_project_id(project_id: str) -> str:
    """Reject a project id that cannot be used as a single path segment (F7-style).

    Real ids are ``new_id("proj")`` output (``proj_`` + 20 hex chars) and
    always pass this; the check is defense-in-depth against a crafted/typo'd
    id ever reaching a filesystem path.
    """
    value = str(project_id or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe project id for a log path: {project_id!r}")
    return value


def project_log_dir(project_id: str, *, base_dir: str | None = None) -> Path:
    """The per-project human-readable log folder: ``<var>/projects/<id>/logs/``."""
    root = Path(base_dir) if base_dir else Path(_var_root())
    return root / "projects" / _safe_project_id(project_id) / "logs"


def project_log_path(project_id: str, *, base_dir: str | None = None) -> Path:
    """The single append-only human log file for a project."""
    return project_log_dir(project_id, base_dir=base_dir) / "activity.log"


_EVENT_TAG_RE = re.compile(r"\[evt:(\d+)\]")


def _last_projected_event_id(log_path: Path) -> int:
    """The event id embedded in the log's last written line, or 0 if none.

    Reading only the file's tail (not the whole file) keeps this cheap
    regardless of how large the log has grown, and makes the projector
    self-healing: delete the log and the next tick rebuilds it (bounded by
    `backfill_limit`) from the events table, which stays the source of truth.
    """
    try:
        if not log_path.exists():
            return 0
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(-min(size, 8192), os.SEEK_END)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    for line in reversed(tail.splitlines()):
        match = _EVENT_TAG_RE.search(line)
        if match:
            return int(match.group(1))
    return 0


def _lock_exclusively(handle: Any) -> None:
    """Advisory lock when the platform provides fcntl (mirrors omniagentos.ledger)."""
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: Any) -> None:
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_lines(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        _lock_exclusively(handle)
        try:
            for line in lines:
                handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _unlock(handle)


def project_pending_activity(
    store: SqliteStore,
    project_id: str,
    *,
    base_dir: str | None = None,
    backfill_limit: int = 500,
) -> int:
    """Append any not-yet-written events to the project's on-disk log.

    Returns the count of lines appended. Safe to call repeatedly, concurrently,
    or on a schedule -- the cursor is self-healing (see
    :func:`_last_projected_event_id`) rather than a separate piece of state
    that could drift. Never raises: a logging fault must not break whatever
    called it (the ``/activity`` read path calls this best-effort on every
    request; :mod:`omniagentos.projects.activity_tick` calls it from a
    standalone periodic tick).

    Bounded by `backfill_limit`: if more than that many project events
    happened since the last projection, only the newest `backfill_limit` are
    written -- an acceptable gap for a glanceable convenience log, since the
    events table (and the ledger's terminal manifests) remain the complete,
    authoritative record.
    """
    try:
        log_path = project_log_path(project_id, base_dir=base_dir)
        cursor = _last_projected_event_id(log_path)

        tasks = store.list_tasks_for_project(project_id, limit=backfill_limit)
        task_by_id = {str(task["id"]): task for task in tasks}
        runs = store.list_runs_for_project(project_id, {}, limit=backfill_limit)
        run_by_id = {str(run["id"]): run for run in runs}
        steps_cache: dict[str, list[dict[str, Any]]] = {}

        events = store.list_events_for_project(project_id, limit=backfill_limit)
        pending = sorted(
            (event for event in events if int(event["id"]) > cursor),
            key=lambda event: int(event["id"]),
        )
        if not pending:
            return 0

        entries = _project_activity_entries(pending, store, task_by_id, run_by_id, steps_cache)
        lines = [f"{entry['ts']} [evt:{entry['id']}] {entry['line']}" for entry in entries]
        _append_lines(log_path, lines)
        return len(pending)
    except Exception:  # noqa: BLE001 -- logging must never break its caller.
        LOG.warning("project activity log projection failed for %s", project_id, exc_info=True)
        return 0


__all__ = [
    "build_project_activity",
    "project_log_dir",
    "project_log_path",
    "project_pending_activity",
]
