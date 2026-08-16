"""Small, failure-tolerant metrics helpers for the knowledge API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.knowledge.contracts import KnowledgeUnavailable
from omniagentos.knowledge.store import KnowledgeStore


def empty_stats() -> dict[str, Any]:
    """Return the dashboard's stable shape while the knowledge DB is unavailable."""
    return {
        "facts": {"total": 0, "active": 0, "quarantined": 0, "superseded": 0},
        "entities": 0,
        "edges": 0,
        "episodes": 0,
        "recalls": {"count": 0, "avg_latency_ms": 0.0, "avg_tokens": 0.0},
        "health": {
            "last_successful_recall_at": None,
            "recall_errors_24h": 0,
            "last_error": None,
        },
        "available": False,
    }


def recall_health(store: KnowledgeStore, audit_store: Any | None = None) -> dict[str, Any]:
    """Aggregate successful recalls from PG and failures from the durable audit feed.

    Recall failures are deliberately recorded in the ordinary SQLite event stream: PG
    may be down precisely when this information matters.  This helper accepts the
    store protocol rather than depending on SQLite implementation details, which also
    makes it straightforward to exercise with a small fake in tests.
    """
    last_success = None
    try:
        lock = getattr(store, "_lock", None)
        conn_fn = getattr(store, "_conn", None)
        if lock is not None and conn_fn is not None and callable(conn_fn):
            with lock:
                with conn_fn().cursor() as cursor:
                    cursor.execute("SELECT MAX(created_at) FROM recall_log")
                    row = cursor.fetchone()
            last_success = row[0] if row and row[0] is not None else None
    except KnowledgeUnavailable:
        raise
    except (AttributeError, TypeError):
        # Store mock or test object without internal attributes; skip health query
        pass

    failures = _recall_failure_events(audit_store)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent = [event for event in failures if (t := _event_time(event)) and t >= cutoff]
    latest = max(failures, key=lambda event: str(event.get("ts", "")), default=None)
    return {
        "last_successful_recall_at": _iso(last_success),
        "recall_errors_24h": len(recent),
        "last_error": _event_error(latest) if latest is not None else None,
    }


def knowledge_stats(store: KnowledgeStore, audit_store: Any | None = None) -> dict[str, Any]:
    """Return API stats, degrading to an all-zero payload rather than raising."""
    try:
        result = store.stats()
        # Start from the frozen dashboard shape so a partial/mock store cannot
        # accidentally omit a field and make the defensive TypeScript normalizer
        # discard useful data.
        payload = empty_stats()
        payload["facts"].update(result.get("facts", {}))
        payload["recalls"].update(result.get("recalls", {}))
        for key in ("entities", "edges", "episodes"):
            if key in result:
                payload[key] = result[key]
        payload["health"] = recall_health(store, audit_store)
        payload["available"] = True
        return payload
    except Exception:
        # Stats is a health surface.  A broken socket or a partially restarted PG
        # connection must render the documented unavailable payload, never a 500.
        return empty_stats()


def _recall_failure_events(audit_store: Any | None) -> list[dict[str, Any]]:
    if audit_store is None or not hasattr(audit_store, "get_events_after"):
        return []
    try:
        return list(audit_store.get_events_after(0, types=["knowledge_recall_failed"], limit=500))
    except Exception:
        # Health telemetry must not turn an otherwise healthy KB endpoint into a 500.
        return []


def _event_time(event: dict[str, Any]) -> datetime | None:
    raw = event.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _event_error(event: dict[str, Any]) -> str | None:
    payload = event.get("payload", event.get("payload_json", {}))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return payload
    return str(payload["error"]) if isinstance(payload, dict) and payload.get("error") else None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["empty_stats", "knowledge_stats", "recall_health"]
