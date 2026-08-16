"""API routes for reliability monitoring and events (§9)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import _authorized, _emit, fail
from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.taxonomy import EventStatus

if TYPE_CHECKING:
    from omniagentos.reliability.store import SqliteReliabilityStore

router = APIRouter(prefix="/api/reliability", tags=["reliability"])

_SUMMARY_CONTRACT_VERSION = "reliability-summary.v1"
_WATCH_STALE_SECONDS = 45 * 60
_UNKNOWN_EVENT_COUNTS: dict[str, None] = {
    "info": None,
    "warning": None,
    "critical": None,
}


class IgnoreEventRequest(BaseModel):
    """Request to mark an event as ignored."""

    pass


class AuditRunRequest(BaseModel):
    """Request to trigger an audit.

    ``kind``:
      - ``watch`` — detector sweep (fast, no full judge panel)
      - ``twice_daily`` — full sweep + analyzer + sandbox + **3-family judge panel**
      - ``daily_summary`` / ``weekly_architecture`` — report cadences

    ``panel_size`` is recorded in stats (default 3) for UI; judge seating still
    comes from governance (min 3 distinct families: claude/codex/grok).
    """

    kind: str = "twice_daily"
    panel_size: int = 3


def _rel_store(store: StoreDep) -> ReliabilityStore:
    """Cast generic store to ReliabilityStore (protocol, not instantiable)."""
    # The store already implements the ReliabilityStore protocol
    return cast(ReliabilityStore, store)


def _value(row: Any, key: str) -> Any:
    return getattr(row, key, None) if not isinstance(row, dict) else row.get(key)


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _incident(code: str, component: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "component": component,
        "severity": "critical",
        "message": message,
    }


def _persist_health_incident(
    store: Any,
    incident: dict[str, str],
    *,
    generated_at: datetime,
) -> None:
    """Best-effort durable incident; the response remains visible if storage is down."""
    writer = getattr(store, "insert_reliability_event", None)
    if writer is None and hasattr(store, "count_open_events_by_severity"):
        # The concrete reliability store uses ``insert_event`` for this table;
        # the API composite exposes the unambiguous alias above.
        writer = getattr(store, "insert_event", None)
    if writer is None:
        return
    bucket = generated_at.strftime("%Y%m%d%H")
    try:
        writer(
            failure_class="other",
            severity="critical",
            signature=f"other|api:reliability_summary|{incident['component']}",
            occurrence_key=(f"reliability_summary_store_read|{incident['component']}|{bucket}"),
            source="api:reliability_summary",
            ref_type="reliability_health",
            ref_id=incident["component"],
            evidence_json={
                "code": incident["code"],
                "component": incident["component"],
            },
        )
    except Exception:
        # The response itself is the incident surface when persistence is the
        # failing boundary. Never turn a health read into a 500 while reporting it.
        return


def _read_open_counts(store: Any) -> dict[str, int]:
    if hasattr(store, "count_open_events_by_severity"):
        raw = store.count_open_events_by_severity()
        return {severity: int(raw.get(severity, 0)) for severity in ("info", "warning", "critical")}
    if not hasattr(store, "list_events"):
        raise RuntimeError("reliability event store is unavailable")
    # Compatibility seam for store fakes/older implementations. The concrete
    # production store always takes the aggregate path above.
    events = store.list_events(status="open", limit=2_147_483_647)
    counts = {"info": 0, "warning": 0, "critical": 0}
    for event in events:
        severity = _value(event, "severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def _read_watch_state(store: Any) -> dict[str, Any]:
    if hasattr(store, "get_watch_state"):
        state = store.get_watch_state()
        if not isinstance(state, dict):
            raise ValueError("watch state is not an object")
        return state
    if not hasattr(store, "get_watch_cursor"):
        raise RuntimeError("watch state store is unavailable")
    cursor = store.get_watch_cursor()
    if cursor is None:
        return {
            "state": "never_run",
            "cursor_at": None,
            "heartbeat_at": None,
            "error": None,
        }
    # Compatibility for protocol implementations predating explicit heartbeat
    # state. They only expose one durable timestamp, so do not invent another.
    return {
        "state": "recorded",
        "cursor_at": str(cursor),
        "heartbeat_at": str(cursor),
        "error": None,
    }


def _watch_summary(
    store: Any,
    *,
    now: datetime,
) -> tuple[dict[str, Any], str | None]:
    try:
        durable = _read_watch_state(store)
        heartbeat_at = durable.get("heartbeat_at")
        if isinstance(heartbeat_at, str) and heartbeat_at:
            try:
                store._last_reliability_watch_state = dict(durable)
            except Exception:
                pass
    except Exception:
        cached = getattr(store, "_last_reliability_watch_state", None)
        if isinstance(cached, dict) and cached.get("heartbeat_at"):
            durable = dict(cached)
            durable["state"] = "last_known_good"
            durable["error"] = "watch_state_unavailable"
        else:
            return (
                {
                    "state": "unavailable",
                    "heartbeat_at": None,
                    "cursor_at": None,
                    "age_seconds": None,
                    "stale_after_seconds": _WATCH_STALE_SECONDS,
                    "error": "watch_state_unavailable",
                },
                "watch_state_unavailable",
            )

    state = str(durable.get("state") or "")
    heartbeat_at = durable.get("heartbeat_at")
    cursor_at = durable.get("cursor_at")
    if state == "never_run":
        return (
            {
                "state": "never_run",
                "heartbeat_at": None,
                "cursor_at": None,
                "age_seconds": None,
                "stale_after_seconds": _WATCH_STALE_SECONDS,
                "error": None,
            },
            "watch_never_run",
        )

    heartbeat = _parse_utc(heartbeat_at)
    age_seconds = max(0, int((now - heartbeat).total_seconds())) if heartbeat is not None else None
    error = durable.get("error")
    if state == "corrupt" or error == "corrupt_watch_state":
        output_state = "last_known_good"
        error = "corrupt_watch_state"
        reason = "corrupt_watch_state"
    elif state == "last_known_good":
        output_state = "last_known_good"
        error = "watch_state_unavailable"
        reason = "watch_state_unavailable"
    elif heartbeat is None:
        output_state = "last_known_good"
        error = "corrupt_watch_heartbeat"
        reason = "corrupt_watch_heartbeat"
    elif age_seconds is not None and age_seconds > _WATCH_STALE_SECONDS:
        output_state = "stale"
        error = None
        reason = "watch_stale"
    else:
        output_state = "current"
        error = None
        reason = None

    return (
        {
            "state": output_state,
            "heartbeat_at": heartbeat_at,
            "cursor_at": cursor_at,
            "age_seconds": age_seconds,
            "stale_after_seconds": _WATCH_STALE_SECONDS,
            "error": error,
        },
        reason,
    )


@router.get("/summary")
def get_summary(
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get measured reliability health without fabricating healthy state."""
    del _
    now = datetime.now(UTC)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    incidents: list[dict[str, str]] = []
    degraded_reasons: list[str] = []
    open_by_severity: dict[str, int | None]

    try:
        counts = _read_open_counts(store)
        open_by_severity = {
            severity: counts[severity] for severity in ("info", "warning", "critical")
        }
        open_events_state = "current"
    except Exception:
        open_by_severity = dict(_UNKNOWN_EVENT_COUNTS)
        open_events_state = "unavailable"
        degraded_reasons.append("open_events_unavailable")
        incidents.append(
            _incident(
                "open_events_unavailable",
                "open_events",
                "Open reliability-event totals could not be read.",
            )
        )

    last_audit = None
    last_audit_state = "never_run"
    try:
        if not hasattr(store, "list_audits"):
            raise RuntimeError("audit store is unavailable")
        audits = store.list_audits(limit=1)
        if audits:
            a = audits[0]
            last_audit = {
                "id": _value(a, "id"),
                "kind": _value(a, "kind"),
                "status": _value(a, "status"),
                "findings": _value(a, "findings"),
                "started_at": _value(a, "started_at"),
                "finished_at": _value(a, "finished_at"),
            }
            last_audit_state = "current"
    except Exception:
        last_audit_state = "unavailable"
        degraded_reasons.append("last_audit_unavailable")
        incidents.append(
            _incident(
                "last_audit_unavailable",
                "last_audit",
                "The latest reliability audit could not be read.",
            )
        )

    watch, watch_reason = _watch_summary(store, now=now)
    if watch_reason:
        degraded_reasons.append(watch_reason)
        incident_messages = {
            "watch_never_run": "The reliability watcher has never recorded a heartbeat.",
            "watch_stale": "The reliability watcher heartbeat is stale.",
            "corrupt_watch_state": "The watcher state is corrupt; showing its last durable heartbeat.",
            "corrupt_watch_heartbeat": "The watcher heartbeat timestamp is corrupt.",
            "watch_state_unavailable": "Watcher state could not be read; showing last known good state when available.",
        }
        incidents.append(
            _incident(
                watch_reason,
                "watch",
                incident_messages[watch_reason],
            )
        )

    for incident in incidents:
        if incident["code"].endswith("_unavailable"):
            _persist_health_incident(store, incident, generated_at=now)

    last_audit_at = None
    if last_audit is not None:
        last_audit_at = last_audit.get("finished_at") or last_audit.get("started_at")

    return {
        "contract_version": _SUMMARY_CONTRACT_VERSION,
        "generated_at": generated_at,
        "health": "degraded" if degraded_reasons else "healthy",
        "degraded_reasons": degraded_reasons,
        "open_events": open_by_severity,
        "open_events_state": open_events_state,
        "open_info": open_by_severity["info"],
        "open_warning": open_by_severity["warning"],
        "open_critical": open_by_severity["critical"],
        "last_audit": last_audit,
        "last_audit_state": last_audit_state,
        "last_audit_status": last_audit.get("status") if last_audit else None,
        "last_audit_at": last_audit_at,
        "last_audit_kind": last_audit.get("kind") if last_audit else None,
        "last_audit_findings": last_audit.get("findings") if last_audit else None,
        "watch": watch,
        "watch_heartbeat": watch["heartbeat_at"],
        "watch_cursor_at": watch["cursor_at"],
        "watch_cursor_age_s": watch["age_seconds"],
        "mode": "approve",
        "incidents": incidents,
    }


@router.get("/events")
def list_events(
    store: StoreDep,
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List reliability events with optional filters."""
    del _
    try:
        events = (
            store.list_events(status=status, severity=severity, limit=limit, offset=offset)
            if hasattr(store, "list_events")
            else []
        )
    except Exception:
        events = []

    result = []
    for e in events:
        item = {
            "id": e.id if hasattr(e, "id") else e.get("id"),
            "failure_class": e.failure_class
            if hasattr(e, "failure_class")
            else e.get("failure_class"),
            "severity": e.severity if hasattr(e, "severity") else e.get("severity"),
            "signature": e.signature if hasattr(e, "signature") else e.get("signature"),
            "occurrence_key": e.occurrence_key
            if hasattr(e, "occurrence_key")
            else e.get("occurrence_key"),
            "source": e.source if hasattr(e, "source") else e.get("source"),
            "ref_type": e.ref_type if hasattr(e, "ref_type") else e.get("ref_type"),
            "ref_id": e.ref_id if hasattr(e, "ref_id") else e.get("ref_id"),
            "evidence_json": (
                e.evidence_json if hasattr(e, "evidence_json") else e.get("evidence_json")
            )
            or {},
            "status": e.status if hasattr(e, "status") else e.get("status"),
            "recovery_json": (
                e.recovery_json if hasattr(e, "recovery_json") else e.get("recovery_json")
            )
            or {},
            "improvement_id": e.improvement_id
            if hasattr(e, "improvement_id")
            else e.get("improvement_id"),
            "audit_id": e.audit_id if hasattr(e, "audit_id") else e.get("audit_id"),
            "detected_at": e.detected_at if hasattr(e, "detected_at") else e.get("detected_at"),
            "updated_at": e.updated_at if hasattr(e, "updated_at") else e.get("updated_at"),
        }
        result.append(item)
    return result


class ResolveEventRequest(BaseModel):
    """Manual resolve for an open reliability event (F2)."""

    reason: str = "resolved_by_operator"


@router.post("/events/{event_id}/resolve")
def resolve_event(
    event_id: str,
    store: StoreDep,
    body: ResolveEventRequest | None = None,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Manually resolve/close an open reliability event (F2 close path)."""
    del _
    body = body or ResolveEventRequest()
    event = store.get_event(event_id) if hasattr(store, "get_event") else None
    if not event:
        fail(404, "not_found", "event not found", {"id": event_id})
    status = event.status if hasattr(event, "status") else event.get("status")
    if status in ("resolved", "ignored", "recovered"):
        return {
            "id": event_id,
            "status": status,
            "already_closed": True,
        }
    if hasattr(store, "close_event"):
        ok = store.close_event(
            event_id,
            actor="human:operator",
            reason=body.reason,
            expected=status,
        )
    else:
        # ``set_event_status`` lives on the concrete store, not the frozen protocol.
        ok = cast("SqliteReliabilityStore", store).set_event_status(
            event_id,
            EventStatus.RESOLVED.value,
            "human:operator",
            {"reason": body.reason, "source": "api"},
            status,
        )
    if not ok:
        fail(409, "conflict", "event status changed", {"id": event_id, "current_status": status})
    _emit(
        store,
        "reliability.event",
        "event.resolved",
        target_type="event",
        target_id=event_id,
        payload={"reason": body.reason},
    )
    return {"id": event_id, "status": "resolved", "reason": body.reason}


@router.post("/events/{event_id}/ignore")
def ignore_event(
    event_id: str,
    store: StoreDep,
    body: IgnoreEventRequest | None = None,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Mark an event as ignored."""
    del _
    event = store.get_event(event_id) if hasattr(store, "get_event") else None
    if not event:
        fail(404, "not_found", "event not found", {"id": event_id})

    status = event.status if hasattr(event, "status") else event.get("status")
    if status == "ignored":
        return {
            "id": event.id if hasattr(event, "id") else event.get("id"),
            "failure_class": event.failure_class
            if hasattr(event, "failure_class")
            else event.get("failure_class"),
            "severity": event.severity if hasattr(event, "severity") else event.get("severity"),
            "status": status,
            "detected_at": event.detected_at
            if hasattr(event, "detected_at")
            else event.get("detected_at"),
        }

    # ``set_event_status`` lives on the concrete store, not the frozen protocol.
    changed = cast("SqliteReliabilityStore", store).set_event_status(
        event_id,
        EventStatus.IGNORED.value,
        "human:operator",
        {"source": "api"},
        status,
    )
    if not changed:
        fail(
            409,
            "conflict",
            "event status changed",
            {"id": event_id, "current_status": status},
        )

    _emit(
        store,
        "reliability.event",
        "event.ignored",
        target_type="event",
        target_id=event_id,
        payload={
            "severity": event.severity if hasattr(event, "severity") else event.get("severity")
        },
    )

    return {
        "id": event.id if hasattr(event, "id") else event.get("id"),
        "failure_class": event.failure_class
        if hasattr(event, "failure_class")
        else event.get("failure_class"),
        "severity": event.severity if hasattr(event, "severity") else event.get("severity"),
        "status": "ignored",
        "detected_at": event.detected_at
        if hasattr(event, "detected_at")
        else event.get("detected_at"),
    }


@router.get("/audits")
def list_audits(
    store: StoreDep,
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List reliability audits."""
    del _
    try:
        audits = (
            store.list_audits(kind=kind, status=status, limit=limit)
            if hasattr(store, "list_audits")
            else []
        )
    except Exception:
        audits = []

    return [
        {
            "id": a.id if hasattr(a, "id") else a.get("id"),
            "kind": a.kind if hasattr(a, "kind") else a.get("kind"),
            "status": a.status if hasattr(a, "status") else a.get("status"),
            "window_start": a.window_start if hasattr(a, "window_start") else a.get("window_start"),
            "window_end": a.window_end if hasattr(a, "window_end") else a.get("window_end"),
            "stats_json": (a.stats_json if hasattr(a, "stats_json") else a.get("stats_json")) or {},
            "findings": a.findings if hasattr(a, "findings") else a.get("findings"),
            "report_note_path": a.report_note_path
            if hasattr(a, "report_note_path")
            else a.get("report_note_path"),
            "started_at": a.started_at if hasattr(a, "started_at") else a.get("started_at"),
            "finished_at": a.finished_at if hasattr(a, "finished_at") else a.get("finished_at"),
        }
        for a in audits
    ]


@router.get("/audits/{audit_id}")
def get_audit(
    audit_id: str,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get a specific audit."""
    del _
    audit = store.get_audit(audit_id) if hasattr(store, "get_audit") else None
    if not audit:
        fail(404, "not_found", "audit not found", {"id": audit_id})

    return {
        "id": audit.id if hasattr(audit, "id") else audit.get("id"),
        "kind": audit.kind if hasattr(audit, "kind") else audit.get("kind"),
        "status": audit.status if hasattr(audit, "status") else audit.get("status"),
        "window_start": audit.window_start
        if hasattr(audit, "window_start")
        else audit.get("window_start"),
        "window_end": audit.window_end if hasattr(audit, "window_end") else audit.get("window_end"),
        "stats_json": (
            audit.stats_json if hasattr(audit, "stats_json") else audit.get("stats_json")
        )
        or {},
        "findings": audit.findings if hasattr(audit, "findings") else audit.get("findings"),
        "report_note_path": audit.report_note_path
        if hasattr(audit, "report_note_path")
        else audit.get("report_note_path"),
        "started_at": audit.started_at if hasattr(audit, "started_at") else audit.get("started_at"),
        "finished_at": audit.finished_at
        if hasattr(audit, "finished_at")
        else audit.get("finished_at"),
    }


@router.post("/audit/run", status_code=202)
def trigger_audit(
    store: StoreDep,
    body: AuditRunRequest,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """Trigger an audit run (async, returns 202)."""
    del _
    import os
    import sys
    from pathlib import Path

    from omniagentos.contracts import new_id
    from omniagentos.reliability.taxonomy import AuditKind

    kind = (body.kind or "twice_daily").strip()
    panel_size = max(3, int(body.panel_size or 3))  # floor: 3 generators/judges
    try:
        AuditKind(kind)
    except ValueError:
        fail(422, "validation", "invalid audit kind", {"kind": kind})

    audit_id = (
        store.create_audit(
            kind=kind,
            window_start=utc_now_iso(),
            window_end=utc_now_iso(),
        )
        if hasattr(store, "create_audit")
        else new_id("aud")
    )

    # Prefer product venv python + product DB so the worker sees the same data.
    root = Path(__file__).resolve().parents[3]
    py = root / ".venv" / "bin" / "python"
    python_exe = str(py) if py.is_file() else sys.executable
    env = os.environ.copy()
    env.setdefault("OMNIAGENTOS_HOME", str(root))
    env.setdefault("OMNIAGENTOS_DB", str(root / "var" / "omniagentos.db"))
    env["OMNIAGENTOS_RELIABILITY_PANEL_SIZE"] = str(panel_size)
    # LLM stages on for twice_daily (analyzer + 3-family judges); watch can skip.
    if kind in {"twice_daily", "weekly_architecture"}:
        env.setdefault("OMNIAGENTOS_RELIABILITY_LLM", "1")

    log_path = root / "var" / "log" / f"reliability-audit-{audit_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = None
    try:
        log_f = open(log_path, "a", encoding="utf-8")
        try:
            subprocess.Popen(
                [
                    python_exe,
                    "-m",
                    "omniagentos.reliability",
                    "audit",
                    "--once",
                    "--kind",
                    kind,
                    "--audit-id",
                    str(audit_id),
                    "--db",
                    env["OMNIAGENTOS_DB"],
                ],
                cwd=str(root),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            # Popen dups the fd for the detached worker; close the parent's copy.
            log_f.close()
    except Exception as exc:  # noqa: BLE001
        _emit(
            store,
            "audit.completed",
            "audit.spawn_failed",
            target_type="audit",
            target_id=str(audit_id),
            payload={"kind": kind, "error": str(exc)},
        )
        fail(500, "spawn_failed", f"could not start audit worker: {exc}")
    finally:
        if log_f is not None:
            log_f.close()

    _emit(
        store,
        "audit.completed",
        "audit.queued",
        target_type="audit",
        target_id=str(audit_id),
        payload={
            "kind": kind,
            "panel_size": panel_size,
            "generators": panel_size,
            "log": str(log_path),
        },
    )

    return {
        "audit_id": audit_id,
        "status": "queued",
        "kind": kind,
        "panel_size": panel_size,
        "generators": panel_size,
        "log_path": str(log_path),
    }


@router.get("/scorecards")
def list_scorecards(
    store: StoreDep,
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    window: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """List scorecards with optional filters."""
    del _
    try:
        scorecards = (
            store.list_scorecards(
                subject_type=subject_type,
                subject_id=subject_id,
                window=window,
                limit=limit,
            )
            if hasattr(store, "list_scorecards")
            else []
        )
    except Exception:
        scorecards = []

    return [
        {
            "id": s.id if hasattr(s, "id") else s.get("id"),
            "subject_type": s.subject_type if hasattr(s, "subject_type") else s.get("subject_type"),
            "subject_id": s.subject_id if hasattr(s, "subject_id") else s.get("subject_id"),
            "window": s.window if hasattr(s, "window") else s.get("window"),
            "period_start": s.period_start if hasattr(s, "period_start") else s.get("period_start"),
            "metrics_json": (
                s.metrics_json if hasattr(s, "metrics_json") else s.get("metrics_json")
            )
            or {},
            "computed_at": s.computed_at if hasattr(s, "computed_at") else s.get("computed_at"),
        }
        for s in scorecards
    ]
