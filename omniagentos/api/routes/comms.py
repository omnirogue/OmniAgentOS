"""Inbound comms surface — the platform's most security-sensitive HTTP endpoint.

Message bodies here are attacker-controlled. The webhook request path is a HARD
security boundary: no network calls, no LLM, no PostgreSQL — only SQLite
persistence via :class:`~omniagentos.steward.store.StewardStore` and the pure
:mod:`omniagentos.comms.normalize` functions. Curated knowledge extraction is a
SEPARATE, offline batch job (:mod:`omniagentos.comms.extract_batch`); nothing in
this module imports the knowledge subsystem.

GitHub events (``source=github``) ride the same ingress after
``X-Hub-Signature-256`` (or shared ``X-Comms-Token``) auth, then
:func:`process_github_event` for normalize + correlation + flag-gated delivery.
"""

from __future__ import annotations

import json
import os
import secrets as secrets_module
from collections.abc import Mapping
from functools import lru_cache
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from omniagentos.api.deps import StoreDep
from omniagentos.api.services import ApiError, fail
from omniagentos.comms.normalize import normalize_webhook, process_github_event
from omniagentos.comms.ratelimit import RateLimiter
from omniagentos.contracts import Events
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.store import StewardStore

router = APIRouter(prefix="/api/comms", tags=["comms"])

__all__ = ["ApiError", "router"]

_rate_limiter = RateLimiter()
GITHUB_SOURCE = "github"


@lru_cache(maxsize=1)
def get_steward_config() -> StewardConfig:
    """Load and cache the process-wide Steward config (overridable in tests)."""
    return load_steward_config()


StewardConfigDep = Annotated[StewardConfig, Depends(get_steward_config)]


def reset_rate_limits() -> None:
    """Clear all in-process rate-limit state (test isolation only)."""
    _rate_limiter.reset()


def _steward(store: Any) -> StewardStore:
    return StewardStore(cast(SqliteStore, store))


def _authorize_inbound(
    *,
    source: str,
    source_cfg: Any,
    body: bytes,
    x_comms_token: str | None,
    x_hub_signature_256: str | None,
) -> None:
    """Auth for inbound webhooks. GitHub accepts hub signature OR shared token."""
    expected = os.environ.get(source_cfg.secret_env, "")
    if not expected:
        fail(401, "unauthorized", "invalid or missing webhook secret")

    if source == GITHUB_SOURCE:
        from omniagentos.comms.github_mode import verify_github_signature

        if verify_github_signature(body, expected, x_hub_signature_256):
            return
        if x_comms_token and secrets_module.compare_digest(x_comms_token, expected):
            return
        fail(401, "unauthorized", "invalid or missing GitHub signature / X-Comms-Token")

    if (
        not x_comms_token
        or not secrets_module.compare_digest(x_comms_token, expected)
    ):
        fail(401, "unauthorized", "invalid or missing X-Comms-Token")


@router.post("/inbound")
async def inbound(
    request: Request,
    store: StoreDep,
    cfg: StewardConfigDep,
    source: str = Query(...),
    x_comms_token: str | None = Header(default=None, alias="X-Comms-Token"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
) -> JSONResponse:
    # --- 1. auth: unknown source -> 401 (body auth after read for github) --------
    source_cfg = cfg.comms.sources.get(source)
    if source_cfg is None:
        fail(401, "unauthorized", "unknown comms source", {"source": source})

    # --- 2. size: Content-Length or actual body > inbound_max_bytes -> 413 ---------
    max_bytes = cfg.comms.inbound_max_bytes
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > max_bytes:
                fail(413, "payload_too_large", "request body exceeds inbound_max_bytes")
        except ValueError:
            pass

    # Read as a bounded stream (rather than request.body()) so a request with a
    # missing/false Content-Length header still can't force an unbounded buffer.
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            fail(413, "payload_too_large", "request body exceeds inbound_max_bytes")
    body = bytes(chunks)

    # Auth after body read so GitHub HMAC can cover the raw bytes.
    _authorize_inbound(
        source=source,
        source_cfg=source_cfg,
        body=body,
        x_comms_token=x_comms_token,
        x_hub_signature_256=x_hub_signature_256,
    )

    # --- 3. rate limit: per-source in-process token bucket -> 429 -----------------
    if not _rate_limiter.allow(source, cfg.comms.rate_limit_per_minute):
        fail(429, "rate_limited", "too many requests for this comms source")

    # --- normalize + store (SQLite only; no LLM, no network, no PG) ----------------
    # An empty body is rejected for parity with malformed bodies (CMP-006): a valid
    # token must not silently create a contentless transcript row.
    if not body:
        fail(400, "invalid_json", "request body is empty")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail(400, "invalid_json", "request body is not valid JSON")
    if not isinstance(payload, dict):
        fail(400, "invalid_json", "request body must be a JSON object")

    if source == GITHUB_SOURCE:
        return _inbound_github(
            store,
            payload,
            event_type=x_github_event,
        )

    message = normalize_webhook(source, payload)
    return _store_message(store, message)


def _inbound_github(
    store: Any,
    payload: dict[str, Any],
    *,
    event_type: str | None,
) -> JSONResponse:
    """Normalize + correlate + deliver GitHub events under the mode flag."""
    from omniagentos.comms.github_mode import github_comms_mode

    mode = github_comms_mode()
    if mode == "off":
        # Source is configured but adapter is flag-off: accept without store so
        # GitHub does not retry storms, but do not claim delivery.
        return JSONResponse(
            status_code=202,
            content={"id": None, "created": False, "mode": "off", "delivered": False},
        )

    steward = _steward(store)
    delivered_holder: dict[str, Any] = {"row": None, "created": False}

    def deliver(message: Mapping[str, Any], correlation: Any) -> None:
        # Persist under enforce; correlation rides in the event payload.
        row, created = steward.insert_comms_message(dict(message))
        delivered_holder["row"] = row
        delivered_holder["created"] = created
        if created:
            corr = correlation.as_dict() if hasattr(correlation, "as_dict") else {}
            store.insert_event(
                Events.COMMS_MESSAGE,
                "comms",
                "stored",
                target_type="comms_message",
                target_id=str(row["id"]),
                payload={
                    "source": row["source"],
                    "sender": row["sender"],
                    "subject": str(row["subject"])[:200],
                    "thread_id": row["thread_id"],
                    "correlation": corr,
                },
            )

    result = process_github_event(
        payload,
        event_type=event_type,
        deliver=deliver if mode == "enforce" else None,
    )
    if result is None:
        # Unknown / ignored event type — ack without store.
        return JSONResponse(
            status_code=202,
            content={"id": None, "created": False, "mode": mode, "delivered": False},
        )

    if mode == "shadow":
        # Shadow: normalize + correlate only; no durable write.
        return JSONResponse(
            status_code=202,
            content={
                "id": None,
                "created": False,
                "mode": "shadow",
                "delivered": False,
                "correlation": result.get("correlation"),
            },
        )

    row = delivered_holder["row"]
    created = bool(delivered_holder["created"])
    if row is None and result.get("message") is not None:
        # Enforce without deliver callback path: still store for audit.
        row, created = steward.insert_comms_message(dict(result["message"]))
        if created:
            store.insert_event(
                Events.COMMS_MESSAGE,
                "comms",
                "stored",
                target_type="comms_message",
                target_id=str(row["id"]),
                payload={
                    "source": row["source"],
                    "sender": row["sender"],
                    "subject": str(row["subject"])[:200],
                    "thread_id": row["thread_id"],
                    "correlation": result.get("correlation"),
                },
            )
    status_code = 202 if created else 200
    return JSONResponse(
        status_code=status_code,
        content={
            "id": None if row is None else row["id"],
            "created": created,
            "mode": mode,
            "delivered": bool(result.get("delivered") or created),
            "correlation": result.get("correlation"),
        },
    )


def _store_message(store: Any, message: dict[str, Any]) -> JSONResponse:
    steward = _steward(store)
    row, created = steward.insert_comms_message(message)

    if created:
        store.insert_event(
            Events.COMMS_MESSAGE,
            "comms",
            "stored",
            target_type="comms_message",
            target_id=str(row["id"]),
            payload={
                "source": row["source"],
                "sender": row["sender"],
                "subject": str(row["subject"])[:200],
                "thread_id": row["thread_id"],
            },
        )

    status_code = 202 if created else 200
    return JSONResponse(status_code=status_code, content={"id": row["id"], "created": created})


@router.get("/messages")
def list_messages(
    store: StoreDep,
    source: str | None = None,
    q: str | None = None,
    thread_id: str | None = None,
    kb_status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    steward = _steward(store)
    return steward.list_comms_messages(
        source=source,
        q=q,
        thread_id=thread_id,
        kb_status=kb_status,
        limit=limit,
        before_id=before_id,
    )


@router.get("/messages/{message_id}")
def get_message(message_id: int, store: StoreDep) -> dict[str, Any]:
    steward = _steward(store)
    row = steward.get_comms_message(message_id)
    if row is None:
        fail(404, "not_found", "comms message not found", {"id": message_id})
    return row


@router.get("/sources")
def list_sources(store: StoreDep) -> list[dict[str, Any]]:
    steward = _steward(store)
    return steward.list_comms_sources()
