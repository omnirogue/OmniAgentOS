"""HTTP handlers for the local control plane.

All persistence goes through :class:`omniagentos.contracts.Store`; keeping the
translation here deliberately makes the API usable with both SQLite and test
stores.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from omniagentos.api.deps import PolicyDep, StoreDep
from omniagentos.api.models import (
    ApprovalDecisionRequest,
    CreateDisciplineRequest,
    CreateRunRequest,
    CreateTaskRequest,
    PauseRequest,
)
from omniagentos.api.services import (
    ApiError,
    create_run_service,
    create_task_service,
    fail,
)
from omniagentos.contracts import (
    TERMINAL_RUN_STATES,
    Events,
    default_ledger_dir,
    utc_now_iso,
)

router = APIRouter(prefix="/api")

__all__ = ["ApiError", "fail", "router"]

# Server-side operator identity stamped on session-linked approval decisions
# (SEC-005). A human-role string the shared gate accepts (not an automation
# identity), so a forged client decided_by cannot impersonate a human.
_OPERATOR_IDENTITY = "operator"


def _authorized(x_session_token: str | None = Header(None, alias="X-Session-Token")) -> None:
    """Require the local session token (SEC-005 / fix6).

    Gates the mutating control-plane routes (decide_approval, put_pause,
    create_discipline, create_task, create_run, cancel_run). The token lives in
    ``var/secrets/sessions-token`` (0600) which a confined agent provably cannot
    read, so an agent that loopback-POSTs these routes is rejected 401 — closing
    the create_task/create_run scope-escape at the control plane. Server-internal
    callers (Steward, intake, scheduler, suggestions) do NOT use these HTTP routes
    — they call ``create_task_service``/``create_run_service`` / ``store.*``
    directly — so gating here does not affect them. The dashboard reaches these
    routes through same-origin Next.js route handlers (``dashboard/src/app/api/**``)
    that attach the token server-side (see ``dashboard/src/lib/serverProxy.ts``).
    """
    from omniagentos.sessions.token import verify_token

    if not verify_token(x_session_token):
        fail(401, "unauthorized", "invalid session token")


try:
    API_VERSION = version("omniagentos")
except PackageNotFoundError:
    API_VERSION = "0.1.0"


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _row(row: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-safe copy and normalize SQLite's integer booleans."""
    if row is None:
        return {}
    out = dict(row)
    for key in ("paused", "usage_estimated"):
        if key in out and out[key] is not None:
            out[key] = bool(out[key])
    return out


def _payload(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value if value is not None else {}


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    value = _row(row)
    value["payload"] = _payload(value.pop("payload_json", {}))
    return value


class _RunEventStore(Protocol):
    """SQLite extension used without widening the frozen Store contract."""

    def get_events_for_run(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]: ...


class _ProjectScopedStore(Protocol):
    """SQLite project-scoped list extensions (see db.store); not in the Store seam."""

    def list_runs_for_project(
        self, project_id: str, filters: dict[str, Any], limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def list_approvals_for_project(
        self, project_id: str, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...


def _require_project_scope(resource_project_id: Any, requested_project_id: str | None) -> None:
    """Enforce that a resource belongs to the requested project.

    A ``project_id`` on a detail/mutation route binds the request to one
    project; a resource whose owning task lives elsewhere (or nowhere) must not
    be reachable through it. Raises 403 on a cross-project reference so Project A
    can never read or mutate Project B's tasks, runs, or approvals (F3).
    """
    if requested_project_id is None:
        return
    if str(resource_project_id or "") != requested_project_id:
        fail(
            403,
            "forbidden",
            "resource does not belong to the requested project",
            {"project_id": requested_project_id},
        )


def _task_project_id(store: StoreDep, task_id: str | None) -> Any:
    if not task_id:
        return None
    task = store.get_task(task_id)
    return task.get("project_id") if task else None


def _emit(
    store: Any,
    event_type: str,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
    trace_id: str = "",
) -> None:
    store.insert_event(
        event_type,
        "api",
        action,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
        trace_id=trace_id,
    )


def _latest_heartbeat(heartbeats: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        heartbeats, key=lambda heartbeat: str(heartbeat.get("last_beat_at", "")), default=None
    )


def _is_alive(last_beat_at: str | None) -> bool:
    if not last_beat_at:
        return False
    try:
        stamp = datetime.strptime(last_beat_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return stamp >= datetime.now(UTC) - timedelta(seconds=30)


@router.get("/health")
def health(store: StoreDep) -> dict[str, Any]:
    from omniagentos.api.eventbus import get_event_hub

    try:
        event_hub = get_event_hub(store).status()
    except Exception as exc:
        # Event-hub status itself is part of the health boundary. Unknown must
        # fail closed, not silently become a healthy/non-degraded result.
        event_hub = {
            "contract_version": 1,
            "state": "degraded",
            "degraded": True,
            "last_error": f"event_hub_status: {type(exc).__name__}: {exc}",
            "last_failure_at": datetime.now(UTC).timestamp(),
        }

    try:
        beat = _latest_heartbeat(store.get_heartbeats())
        last_beat_at = beat.get("last_beat_at") if beat else None
    except Exception as exc:
        # A dead store is also a dead source for the event tailer. Preserve any
        # existing hub counters, but make the returned snapshot explicitly
        # degraded so operators and L13 cannot mistake an outage for "ok".
        event_hub = {
            **event_hub,
            "contract_version": 1,
            "state": "degraded",
            "degraded": True,
            "last_error": f"health_store: {type(exc).__name__}: {exc}",
            "last_failure_at": datetime.now(UTC).timestamp(),
        }
        return {
            "status": "degraded",
            "version": API_VERSION,
            "db": False,
            "worker": {"alive": False, "last_beat_at": None},
            "event_hub": event_hub,
        }

    overall = "degraded" if event_hub.get("degraded") else "ok"
    return {
        "status": overall,
        "version": API_VERSION,
        "db": True,
        "worker": {"alive": _is_alive(last_beat_at), "last_beat_at": last_beat_at},
        "event_hub": event_hub,
    }


@router.get("/pause")
def get_pause(store: StoreDep) -> dict[str, Any]:
    return _row(store.get_pause())


@router.put("/pause")
def put_pause(
    body: PauseRequest,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    del _
    value = _row(store.set_pause(body.paused, body.reason or ""))
    _emit(
        store,
        Events.PAUSE_CHANGED,
        "pause.changed",
        target_type="pause",
        target_id="1",
        payload={"paused": value["paused"], "reason": value.get("reason", "")},
    )
    return value


@router.get("/disciplines")
def list_disciplines(store: StoreDep) -> list[dict[str, Any]]:
    rows = [_row(row) for row in store.list_disciplines()]
    return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)[:100]


@router.post("/disciplines", status_code=201)
def create_discipline(
    body: CreateDisciplineRequest,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    del _
    metric = body.metric_contract
    row: dict[str, Any] = {
        "id": body.id,
        "name": body.name,
        "metric_contract": metric if isinstance(metric, str) else _json(metric or {}),
        "status": "active",
        "created_at": utc_now_iso(),
    }
    if not store.create_discipline(row):
        fail(409, "conflict", "discipline already exists", {"id": body.id})
    _emit(store, Events.AUDIT, "discipline.created", target_type="discipline", target_id=body.id)
    return row


@router.post("/tasks", status_code=201)
def create_task(
    body: CreateTaskRequest,
    store: StoreDep,
    policy_cfg: PolicyDep,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    del _
    return create_task_service(
        store,
        policy_cfg,
        title=body.title,
        discipline_id=body.discipline_id,
        project_id=body.project_id,
        input=body.input,
        acceptance=body.acceptance,
        risk=body.risk,
        tools_allowed=body.tools_allowed,
    )


@router.get("/tasks")
def list_tasks(
    store: StoreDep,
    state: str | None = None,
    discipline: str | None = None,
    project_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    filters = {
        key: value
        for key, value in {
            "state": state,
            "discipline_id": discipline,
            "project_id": project_id,
        }.items()
        if value is not None
    }
    return [_row(row) for row in store.list_tasks(filters, limit=limit)]


@router.get("/tasks/{task_id}")
def get_task(task_id: str, store: StoreDep, project_id: str | None = None) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        fail(404, "not_found", "task not found", {"id": task_id})
    _require_project_scope(task.get("project_id"), project_id)
    result = _row(task)
    result["runs"] = [_run_summary(row) for row in store.list_runs({"task_id": task_id}, limit=100)]
    return result


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    source = _row(row)
    fields = (
        "id",
        "task_id",
        "state",
        "harness",
        "arm",
        "model",
        "agent",
        "queued_at",
        "started_at",
        "finished_at",
        "cost_usd",
        "usage_estimated",
    )
    return {field: source.get(field) for field in fields}


@router.post("/tasks/{task_id}/runs", status_code=201)
def create_run(
    task_id: str,
    body: CreateRunRequest,
    store: StoreDep,
    policy_cfg: PolicyDep,
    _: Annotated[None, Depends(_authorized)],
    project_id: str | None = None,
) -> dict[str, Any]:
    del _
    if project_id is not None:
        task = store.get_task(task_id)
        if task is None:
            fail(404, "not_found", "task not found", {"id": task_id})
        _require_project_scope(task.get("project_id"), project_id)
    return create_run_service(
        store,
        policy_cfg,
        task_id=task_id,
        harness=body.harness,
        arm=body.arm,
        model=body.model,
        plan=body.plan,
        budget=body.budget,
        prompt=body.prompt,
    )


@router.get("/runs")
def list_runs(
    store: StoreDep,
    state: str | None = None,
    task_id: str | None = None,
    arm: str | None = None,
    harness: str | None = None,
    project_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    filters = {
        key: value
        for key, value in {
            "state": state,
            "task_id": task_id,
            "arm": arm,
            "harness": harness,
        }.items()
        if value is not None
    }
    if project_id is not None:
        # Scope in SQL before ORDER BY / LIMIT so a project's older runs survive
        # a global window of newer, unrelated runs (F6).
        scoped = cast(_ProjectScopedStore, store)
        rows = scoped.list_runs_for_project(project_id, filters, limit=limit)
    else:
        rows = store.list_runs(filters, limit=limit)
    return [_run_summary(row) for row in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: str, store: StoreDep, project_id: str | None = None) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        fail(404, "not_found", "run not found", {"id": run_id})
    _require_project_scope(_task_project_id(store, run.get("task_id")), project_id)
    result = _row(run)
    result["steps"] = [_row(row) for row in store.get_steps(run_id)]
    # This optimized concrete extension avoids widening the frozen Store seam.
    event_store = cast(_RunEventStore, store)
    events = event_store.get_events_for_run(run_id, limit=500)
    result["events"] = [_event_row(row) for row in events]
    result["artifacts"] = [_row(row) for row in store.get_artifacts(run_id)]
    result["approvals"] = [
        _row(row) for row in store.list_approvals(None, limit=500) if row.get("run_id") == run_id
    ]
    result["receipts"] = [_row(row) for row in store.idem_for_run(run_id)]
    return result


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)],
    project_id: str | None = None,
) -> dict[str, bool]:
    del _
    run = store.get_run(run_id)
    if run is None:
        fail(404, "not_found", "run not found", {"id": run_id})
    _require_project_scope(_task_project_id(store, run.get("task_id")), project_id)
    if run.get("state") in {state.value for state in TERMINAL_RUN_STATES}:
        fail(409, "invalid_state", "cannot cancel a terminal run", {"state": run.get("state")})
    if not store.request_cancel(run_id):
        fail(409, "invalid_state", "cancel request could not be recorded")
    _emit(
        store,
        Events.AUDIT,
        "run.cancel_requested",
        target_type="run",
        target_id=run_id,
        payload={"run_id": run_id},
        trace_id=str(run.get("trace_id", "")),
    )
    return {"ok": True}


@router.get("/approvals")
def list_approvals(
    store: StoreDep,
    state: str | None = "pending",
    project_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    if project_id is not None:
        # Scope in SQL before ORDER BY / LIMIT (F6), same reasoning as list_runs.
        scoped = cast(_ProjectScopedStore, store)
        rows = scoped.list_approvals_for_project(project_id, state, limit=limit)
    else:
        rows = store.list_approvals(state, limit=limit)
    return [_row(row) for row in rows]


def _resolve_approval(store: Any, approval_id: str) -> dict[str, Any] | None:
    """Resolve an approval by id.

    Fast path is the newest-window scan (also the in-memory test-store path).
    DR-009 / T-DESIGN-004: an approval OUTSIDE the newest-500 window is still
    decidable via a point lookup on the shared approvals table, so a decision on
    an older approval no longer 404s.
    """
    approval = next(
        (row for row in store.list_approvals(None, limit=500) if row.get("id") == approval_id),
        None,
    )
    if approval is not None:
        return approval
    reader = _open_sessions_reader()
    if reader is None:
        return None
    return reader.get_approval_by_id(approval_id)


@router.post("/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    body: ApprovalDecisionRequest,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)],
    project_id: str | None = None,
) -> dict[str, Any]:
    del _
    approval = _resolve_approval(store, approval_id)
    if approval is None:
        fail(404, "not_found", "approval not found", {"id": approval_id})
    _require_project_scope(_task_project_id(store, approval.get("task_id")), project_id)
    if approval.get("state") != "pending":
        fail(409, "invalid_state", "approval is not pending", {"state": approval.get("state")})
    # SEC-005: for a SESSION-linked approval the deciding identity is stamped
    # server-side (never trust a client-supplied decided_by), so a forged human
    # name cannot impersonate a real operator through the shared gate. Run-scoped
    # approvals keep the existing client-supplied identity behavior.
    is_session_approval = bool(approval.get("session_id"))
    decided_by = (
        _OPERATOR_IDENTITY if is_session_approval else (body.decided_by or _OPERATOR_IDENTITY)
    )
    # T-CODE-002 (decide path): a session approval past its expiry cannot be decided
    # into resume authority — expire it atomically instead. Scoped to session
    # approvals (session_id present) so runner-approval behavior is unchanged.
    expires_at = approval.get("expires_at")
    if is_session_approval and expires_at and str(expires_at) <= utc_now_iso():
        store.decide_approval(approval_id, "expired", decided_by, "expired before decision")
        fail(409, "invalid_state", "approval expired", {"id": approval_id})
    if not store.decide_approval(approval_id, body.decision, decided_by, body.note):
        fail(409, "invalid_state", "approval is not pending")
    decided = _resolve_approval(store, approval_id) or approval
    _emit(
        store,
        Events.APPROVAL_DECIDED,
        "approval.decided",
        target_type="approval",
        target_id=approval_id,
        payload={
            "approval_id": approval_id,
            "run_id": decided.get("run_id"),
            "session_id": decided.get("session_id"),
            "action_class": decided.get("action_class"),
            "state": decided.get("state"),
        },
    )
    return _row(decided)


@router.get("/budgets")
def list_budgets(store: StoreDep) -> list[dict[str, Any]]:
    return [_row(row) for row in store.list_budgets()]


def _format_sse(event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event_type}\ndata: {_json(data)}\n\n"


def _open_sessions_reader() -> Any | None:
    """Return the shared process-lifetime sessions reader for SSE/point-lookup.

    T-DESIGN-002: session.updated is SSE-ONLY, synthesized from the sessions table
    (never persisted), mirroring worker.heartbeat. This is the SAME process-lifetime
    DAL as the sessions routes (T-OPS-006); callers must NOT close it. Returns None
    if unavailable.
    """
    try:
        from omniagentos.api.routes.sessions import get_sessions_dal

        return get_sessions_dal()
    except Exception:
        return None


def _session_updated_frame(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row.get("id"),
        "source": row.get("source"),
        "state": row.get("state"),
        "project_dir": row.get("project_dir"),
        "title": row.get("title"),
        "model": row.get("model"),
        "cost_usd": row.get("cost_usd"),
        "last_activity_at": row.get("last_activity_at"),
        "updated_at": row.get("updated_at"),
    }


@router.get("/events")
async def events(
    request: Request,
    store: StoreDep,
    after_id: int | None = Query(None, ge=0),
    types: str | None = None,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    if after_id is None:
        try:
            cursor = int(last_event_id or "0")
        except ValueError:
            cursor = 0
    else:
        cursor = after_id
    selected = [item.strip() for item in (types or "").split(",") if item.strip()] or None

    async def stream() -> Any:
        nonlocal cursor
        # T1.5: this connection no longer polls. It subscribes to the process-wide
        # EventHub, whose SINGLE tailer thread runs the three former per-connection
        # queries (get_heartbeats / list_sessions / get_events_after) once per tick
        # for the WHOLE process and fans the result out over an asyncio.Queue. The
        # loop-blocking sqlite work that used to be O(connections) is now O(1), and
        # it happens off the event-loop thread entirely.
        from omniagentos.api.eventbus import (
            FRAME_DEGRADED,
            FRAME_EVENT,
            FRAME_HEARTBEAT,
            FRAME_SESSION,
            get_event_hub,
        )

        want_heartbeat = selected is None or Events.WORKER_HEARTBEAT in selected
        want_session_updated = selected is None or Events.SESSION_UPDATED in selected
        # Subscribe BEFORE the replay read: the hub starts buffering into this
        # connection's queue immediately, so an event inserted between the replay
        # query and the first drain cannot fall down the gap. The `id <= cursor`
        # check in the loop drops the resulting overlap.
        subscription = get_event_hub(store).subscribe(
            wants_heartbeats=want_heartbeat, wants_sessions=want_session_updated
        )
        try:
            # Flush the connection immediately so the browser's EventSource.onopen
            # fires at once and the dashboard "Live events connected" badge flips true
            # without waiting for the first real event. A quiet stream previously
            # produced no bytes until the 15s keepalive (or the first heartbeat/event),
            # which read as "disconnected" and made the UI fall back to the poll badge.
            # `retry:` also pins the client's auto-reconnect backoff.
            yield "retry: 3000\n"
            yield ": connected\n\n"
            latest = store.latest_event_id()
            if cursor + 500 < latest:
                yield _format_sse("resync", {"latest_id": latest})
                replay_after = latest - 500
            else:
                replay_after = cursor
            replay = store.get_events_after(replay_after, types=selected, limit=500)
            for row in replay:
                event = _event_row(row)
                cursor = max(cursor, int(event["id"]))
                yield _format_sse(event["type"], event, int(event["id"]))

            session_updated_seen: dict[str, str] = {}
            # worker.heartbeat and session.updated are SSE-ONLY synthesized types
            # (contracts.Events.SSE_ONLY): never written to the events table, so a
            # heartbeat flood can never evict real events from the replay window --
            # which also means they are NOT replayable from the cursor. A connecting
            # client is caught up with the hub's cached SNAPSHOT instead, taken once
            # per tick for the whole process rather than once per connection.
            beats, session_rows = subscription.snapshot()
            if want_heartbeat:
                for beat in beats:
                    worker_id = str(beat.get("worker_id", ""))
                    if worker_id:
                        yield _format_sse(
                            Events.WORKER_HEARTBEAT,
                            {"worker_id": worker_id, "current_run_id": beat.get("current_run_id")},
                        )
            if want_session_updated:
                for srow in session_rows:
                    sid = str(srow.get("id") or "")
                    if sid:
                        session_updated_seen[sid] = str(srow.get("updated_at") or "")
                        yield _format_sse(Events.SESSION_UPDATED, _session_updated_frame(srow))

            last_keepalive = asyncio.get_running_loop().time()
            resyncing = False
            while not await request.is_disconnected():
                # Blocks on the queue instead of sleeping, so a new event reaches
                # the client as soon as the tailer publishes it. The 1s ceiling only
                # bounds how often disconnect/keepalive are re-checked; a catch-up
                # in progress polls without waiting so it converges promptly.
                frames, lagged = await subscription.drain(0.0 if resyncing else 1.0)
                if lagged:
                    # This connection fell far enough behind that its queue
                    # overflowed and dropped frames.
                    resyncing = True
                    # The overflow drops the REST of the tick once the queue is
                    # full, and a tick is ordered status/heartbeat/session/event
                    # — so the frames at the front of the drop are the SSE-only
                    # kinds (worker.heartbeat, session.updated) that are never
                    # written to the events table and therefore cannot be
                    # refetched by the cursor resync below. The hub's session
                    # dedupe stamp is hub-wide, so a dropped session.updated is
                    # never published again and the row would stay stale on this
                    # connection forever. Recover them from the hub's cached
                    # snapshot -- the same source a fresh connect is caught up
                    # from -- and let the per-connection dedupe drop the rows
                    # this connection has already seen.
                    lagged_beats, lagged_sessions = subscription.snapshot()
                    recovered: list[tuple[str, dict[str, Any]]] = []
                    if want_heartbeat:
                        recovered.extend((FRAME_HEARTBEAT, beat) for beat in lagged_beats)
                    if want_session_updated:
                        recovered.extend((FRAME_SESSION, srow) for srow in lagged_sessions)
                    frames = [*recovered, *frames]
                if resyncing:
                    # Durable events are authoritative, so re-read them by cursor and
                    # ignore whatever is queued (those rows are either duplicates or
                    # sit past the gap). Read from the hub's in-memory ring while it
                    # still covers us, else from the store on a worker thread so the
                    # event loop stays free. Repeat until the log tail is reached --
                    # bounded per pass, with the remainder taken on the next lap, so
                    # one connection's catch-up can never monopolize the loop.
                    frames = [frame for frame in frames if frame[0] != FRAME_EVENT]
                    scan = cursor
                    catchup: list[dict[str, Any]] = []
                    for _ in range(20):
                        rows, covered = subscription.ring_replay(scan)
                        if not covered:
                            rows = await asyncio.to_thread(
                                store.get_events_after, scan, selected, 500
                            )
                        rows = [row for row in rows if int(row["id"]) > scan]
                        if not rows:
                            resyncing = False
                            break
                        catchup.extend(rows)
                        scan = int(rows[-1]["id"])
                    frames = [*frames, *((FRAME_EVENT, row) for row in catchup)]
                for kind, payload in frames:
                    if kind == FRAME_EVENT:
                        event = _event_row(payload)
                        event_id = int(event["id"])
                        if event_id <= cursor:
                            continue
                        if selected is not None and str(event.get("type", "")) not in selected:
                            continue
                        cursor = max(cursor, event_id)
                        yield _format_sse(event["type"], event, event_id)
                    elif kind == FRAME_HEARTBEAT:
                        if not want_heartbeat:
                            continue
                        worker_id = str(payload.get("worker_id", ""))
                        if worker_id:
                            yield _format_sse(
                                Events.WORKER_HEARTBEAT,
                                {
                                    "worker_id": worker_id,
                                    "current_run_id": payload.get("current_run_id"),
                                },
                            )
                    elif kind == FRAME_SESSION:
                        if not want_session_updated:
                            continue
                        sid = str(payload.get("id") or "")
                        stamp = str(payload.get("updated_at") or "")
                        if sid and session_updated_seen.get(sid) != stamp:
                            session_updated_seen[sid] = stamp
                            yield _format_sse(
                                Events.SESSION_UPDATED, _session_updated_frame(payload)
                            )
                    elif kind == FRAME_DEGRADED:
                        # Operator-visible event-hub degraded/recovery notice (H-41).
                        # Always forwarded; clients may ignore unknown event types.
                        yield _format_sse("eventbus.status", dict(payload))
                now_mono = asyncio.get_running_loop().time()
                if now_mono - last_keepalive >= 15:
                    last_keepalive = now_mono
                    yield ": keepalive\n\n"
        finally:
            # Drops this connection from the hub and stops the tailer once the
            # last subscriber leaves. The sessions reader the tailer uses is a
            # shared process-lifetime DAL (T-OPS-006) and is intentionally never
            # closed.
            subscription.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/ledger")
def ledger(
    store: StoreDep, limit: int = Query(100, ge=1, le=500), run_id: str | None = None
) -> list[Any]:
    # Imported here so p06 can land independently and tests can monkeypatch it.
    del store  # This endpoint is file-backed by contract, not a database query.
    from omniagentos.ledger import read_manifests

    manifests = read_manifests(default_ledger_dir(), run_id=run_id, limit=limit)
    return [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in manifests
    ]
