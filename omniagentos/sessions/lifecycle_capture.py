"""PreToolUse experience capture into the existing memory-store interface.

Versioned envelope written via :class:`ConversationStore.append_turn` — no new
SQLite migration. Gated by ``OMNIAGENTOS_SESSION_MEMORY_CAPTURE_MODE``
(off → shadow → enforce). Fail-open: capture errors never block the hook gate.

Growth is bounded: each envelope is capped in bytes, and lifecycle_capture turns
for a session are purged down to a fixed keep-count after each enforce write so
PreToolUse traffic cannot unbounded-grow the ``conversations`` table.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Literal

from omniagentos.contracts import utc_now_iso

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_CAPTURES_PER_SESSION",
    "ENVELOPE_VERSION",
    "SESSION_MEMORY_CAPTURE_ENV",
    "build_envelope",
    "capture_pretool_use",
    "redact_secrets",
    "session_memory_capture_mode",
]

LOG = logging.getLogger(__name__)

SESSION_MEMORY_CAPTURE_ENV = "OMNIAGENTOS_SESSION_MEMORY_CAPTURE_MODE"
CaptureMode = Literal["off", "shadow", "enforce"]
CAPTURE_MODES: tuple[CaptureMode, ...] = ("off", "shadow", "enforce")
DEFAULT_MODE: CaptureMode = "off"
ENVELOPE_VERSION = "lifecycle-capture-v1"
DEFAULT_MAX_BYTES = 4_096
DEFAULT_MAX_CAPTURES_PER_SESSION = 32

# In-place value redaction: replace only the credential *value*, preserving
# surrounding syntax (key names, separators, quotes). Order matters.
# 1. Bearer tokens (value after ``Bearer ``) before generic authorization forms.
# 2. Named key/value forms with optional matching quotes around the value.
# 3. OpenAI-style ``sk-…`` keys, bare JWTs, and PEM private-key blocks.
_BEARER_VALUE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+=*)")
# Named forms (not authorization — Bearer path owns that header shape).
_NAMED_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*)"
    r"([\"']?)([^\s\"']+)\2"
)
# Bare ``Authorization: <token>`` without a Bearer prefix.
_AUTH_NON_BEARER = re.compile(r"(?i)(\bauthorization\s*[:=]\s*)([\"']?)(?!bearer\b)([^\s\"']+)\2")
_SK_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])")
# Compact JWT (header.payload.signature); bare form without ``Bearer `` prefix.
_BARE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM_PRIVATE_KEY = re.compile(
    r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)


def session_memory_capture_mode(env: Mapping[str, str] | None = None) -> CaptureMode:
    source = env if env is not None else os.environ
    raw = str(source.get(SESSION_MEMORY_CAPTURE_ENV, DEFAULT_MODE)).strip().lower()
    if raw in CAPTURE_MODES:
        return raw  # type: ignore[return-value]
    return DEFAULT_MODE


def _redact_secret_text(text: str) -> str:
    """Replace credential values in place; preserve keys, quotes, and separators."""
    text = _BEARER_VALUE.sub(r"\1[REDACTED]", text)
    text = _NAMED_SECRET_VALUE.sub(r"\1\2[REDACTED]\2", text)
    text = _AUTH_NON_BEARER.sub(r"\1\2[REDACTED]\2", text)
    text = _SK_KEY.sub("[REDACTED]", text)
    text = _BARE_JWT.sub("[REDACTED]", text)
    text = _PEM_PRIVATE_KEY.sub("[REDACTED]", text)
    return text


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking strings; never raises."""
    try:
        if isinstance(value, str):
            return _redact_secret_text(value)
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if re.search(r"(?i)(password|secret|token|api[_-]?key|authorization)", key):
                    out[key] = "[REDACTED]"
                else:
                    out[key] = redact_secrets(v)
            return out
        if isinstance(value, list):
            return [redact_secrets(item) for item in value]
        if isinstance(value, tuple):
            return [redact_secrets(item) for item in value]
        return value
    except Exception:  # noqa: BLE001
        return "[REDACT_ERROR]"


def build_envelope(
    payload: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Build a versioned, redacted experience envelope from a hook payload."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = {}
    body = {
        "version": ENVELOPE_VERSION,
        "event": "tool_call",
        "hook_event_name": str(payload.get("hook_event_name") or "PreToolUse"),
        "session_id": str(payload.get("session_id") or ""),
        "tool_name": str(payload.get("tool_name") or ""),
        "tool_input": redact_secrets(dict(tool_input)),
        "cwd": str(payload.get("cwd") or ""),
        "captured_at": utc_now_iso(),
    }
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > max_bytes:
        # Truncate tool_input content rather than drop the envelope.
        body["tool_input"] = {"_truncated": True}
        body["note"] = f"payload truncated to stay under {max_bytes} bytes"
    return body


def capture_pretool_use(
    payload: Mapping[str, Any],
    *,
    store: Any | None = None,
    env: Mapping[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_captures: int = DEFAULT_MAX_CAPTURES_PER_SESSION,
) -> dict[str, Any] | None:
    """Capture one PreToolUse event. Fail-open; returns envelope or None.

    * ``off`` — no-op.
    * ``shadow`` — build/redact envelope but do not write.
    * ``enforce`` — write through the memory-store interface when available,
      then purge older lifecycle_capture turns beyond ``max_captures``.
    """
    try:
        mode = session_memory_capture_mode(env)
        if mode == "off":
            return None
        envelope = build_envelope(payload, max_bytes=max_bytes)
        if mode == "shadow":
            LOG.debug(
                "session memory capture shadow: tool=%s session=%s",
                envelope.get("tool_name"),
                envelope.get("session_id"),
            )
            return envelope
        # enforce
        if store is None:
            store = _default_store()
        if store is None:
            LOG.debug("session memory capture: no store available")
            return envelope
        session_id = str(envelope.get("session_id") or "unknown")
        content = json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str)
        store.append_turn(
            "session",
            session_id,
            "system",
            content,
            meta={
                "kind": "lifecycle_capture",
                "version": ENVELOPE_VERSION,
                "tool_name": envelope.get("tool_name"),
            },
        )
        _purge_lifecycle_captures(store, session_id, keep=max(1, max_captures))
        return envelope
    except Exception:  # noqa: BLE001 — never block PreToolUse
        LOG.debug("session memory capture failed open", exc_info=True)
        return None


def _purge_lifecycle_captures(store: Any, session_id: str, *, keep: int) -> None:
    """Drop oldest lifecycle_capture turns beyond ``keep`` for one session.

    Best-effort and fail-open: purge errors never propagate to the hook path.
    """
    try:
        # In-memory test stores expose a turns list.
        turns = getattr(store, "turns", None)
        if isinstance(turns, list):
            lifecycle = [
                t
                for t in turns
                if str(t.get("scope_type")) == "session"
                and str(t.get("scope_id")) == session_id
                and str((t.get("meta") or {}).get("kind") or "") == "lifecycle_capture"
            ]
            if len(lifecycle) <= keep:
                return
            # Prefer chronological order of append; keep the newest ``keep``.
            overflow = lifecycle[: len(lifecycle) - keep]
            drop_ids = {id(t) for t in overflow}
            store.turns[:] = [t for t in turns if id(t) not in drop_ids]
            return

        purge = getattr(store, "purge_turns_by_meta", None)
        if callable(purge):
            purge(
                "session",
                session_id,
                kind="lifecycle_capture",
                keep=keep,
            )
            return

        connection = getattr(store, "_connection", None)
        if connection is None:
            inner = getattr(store, "_store", None)
            if inner is not None:
                connection = getattr(inner, "_connection", None)
        if connection is None:
            return
        # Keep the newest ``keep`` lifecycle_capture rows; delete the rest.
        connection.execute(
            """
            DELETE FROM conversations
            WHERE id IN (
                SELECT id FROM conversations
                WHERE scope_type = ?
                  AND scope_id = ?
                  AND json_extract(meta_json, '$.kind') = 'lifecycle_capture'
                ORDER BY seq DESC
                LIMIT -1 OFFSET ?
            )
            """,
            ("session", session_id, keep),
        )
        try:
            connection.commit()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        LOG.debug("lifecycle capture purge failed open", exc_info=True)


def _default_store() -> Any | None:
    try:
        from omniagentos.contracts import default_db_path
        from omniagentos.db.store import SqliteStore
        from omniagentos.memory.store import ConversationStore

        return ConversationStore(SqliteStore(default_db_path()))
    except Exception:  # noqa: BLE001
        return None
