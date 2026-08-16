"""POST /api/session-events/hook — attributed attention from Claude Code hooks.

A Claude Code Notification / permission / Stop / SessionEnd hook POSTs here.
The route locates (or creates) the session row, writes last-write-wins
attention fields, lets ``session.updated`` synthesize from ``updated_at``
(SSE-only, never persisted), then fire-and-forget pushes to ``OMNI_NTFY_URL``.

Auth is the same scoped hook-eval mechanism (``X-Session-Hook-Token`` or the
full control-plane token). The push step is presentation: any failure is
logged and swallowed so the hook caller always sees the row/SSE outcome.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from queue import Full, Queue
from typing import Any

from fastapi import APIRouter, Header
from pydantic import Field

from omniagentos.api.models import ApiModel
from omniagentos.api.routes.sessions import _hook_eval_authorized, get_sessions_dal
from omniagentos.api.services import fail
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.busy import execute_write_transaction
from omniagentos.sessions.notify import ntfy_url_from_env

router = APIRouter(prefix="/api/session-events", tags=["session-events"])
logger = logging.getLogger(__name__)

_NEEDS_INPUT = frozenset({"notification", "permission_request"})
_HOOK_EVENTS = frozenset({"notification", "permission_request", "stop", "session_end"})
_ATTENTION_COLUMNS = (
    ("attention_state", "TEXT"),
    ("attention_reason", "TEXT"),
    ("attention_since", "TEXT"),
)
#: Operator vault. Same file notify-owner.sh / run-loop.sh read for OMNI_NTFY_URL.
_CONNECTIONS_ENV_PATH = Path.home() / ".config" / "omni" / "connections.env"
_PUSH_TIMEOUT_S = 3.0
_TEXT_CAP = 2000
_NTFY_DEDUP_S = 60.0
#: Connections that have already had the attention_* PRAGMA/ALTER applied.
_ATTENTION_COLUMNS_READY: set[int] = set()
_ATTENTION_COLUMNS_LOCK = threading.Lock()
_NTFY_DEDUP: dict[tuple[str, str], float] = {}
_NTFY_DEDUP_LOCK = threading.Lock()
_NTFY_DEDUP_MAX = 256
_PUSH_QUEUE: Queue[tuple[str, str]] = Queue(maxsize=100)
_PUSH_WORKER_STARTED = False
_PUSH_WORKER_LOCK = threading.Lock()


class SessionHookEventRequest(ApiModel):
    session_id: str | None = None
    session_ref: str | None = None
    event: str
    cwd: str = Field(min_length=1)
    message: str | None = None
    title: str | None = None


class SessionHookEventResponse(ApiModel):
    ok: bool = True
    session_id: str


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clip_text(value: str | None, cap: int = _TEXT_CAP) -> str | None:
    if value is None:
        return None
    if len(value) <= cap:
        return value
    return value[:cap]


def _attention_state_for(event: str) -> str | None:
    """Map a hook event to attention_state. ``stop`` clears (None).

    ``session_end`` never writes an attention state: terminalization clears
    attention in the same transaction, so a transient "finished" value would
    be written and NULLed within one request (observed) — it has no reader.
    """
    if event in _NEEDS_INPUT:
        return "needs_input"
    if event in ("stop", "session_end"):
        return None
    raise ValueError(f"unmapped attention event: {event}")


def _ensure_attention_columns(dal: Any) -> None:
    """Idempotently add attention_* columns (migration 136 + in-process fallback).

    Numbered migration 136 is the store's normal schema path. This PRAGMA +
    ALTER TABLE guard matches SessionsDal._ensure_orchestrator_marker /
    _has_cost_estimate_columns: additive, no extra version bump, safe on a
    FakeSessionsDal that has no connection and on a pre-136 scratch DB.
    SQLite has no ADD COLUMN IF NOT EXISTS (see 118's header), so we probe.
    Cached per connection so a live API process only pays the PRAGMA once.
    """
    conn = getattr(dal, "_connection", None)
    if conn is None:
        return
    key = id(conn)
    if key in _ATTENTION_COLUMNS_READY:
        return
    with _ATTENTION_COLUMNS_LOCK:
        if key in _ATTENTION_COLUMNS_READY:
            return
        try:
            info = conn.execute("PRAGMA table_info(sessions)").fetchall()
        except Exception:  # noqa: BLE001 -- unreadable schema, stay silent
            return
        names: set[str] = set()
        for row in info:
            try:
                names.add(str(row["name"]))
            except (KeyError, IndexError, TypeError):
                names.add(str(row[1]))
        missing = [(name, decl) for name, decl in _ATTENTION_COLUMNS if name not in names]
        if missing:

            def _add_columns(connection: Any) -> None:
                for name, decl in missing:
                    try:
                        connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
                    except Exception as exc:  # noqa: BLE001
                        # Only the duplicate-column race is benign. Any other
                        # failure (read-only DB, disk) must PROPAGATE so the
                        # connection is not cached as ready with the columns
                        # still missing (every later request would 500).
                        if "duplicate column" in str(exc).lower():
                            logger.debug("attention column %s already present", name)
                            continue
                        raise

            try:
                # The DAL serializes every other access to this connection via
                # dal._lock; the busy seam opens with rollback(), so running
                # unlocked here could discard a concurrent writer's txn.
                execute_write_transaction(
                    conn,
                    _add_columns,
                    op="session_attention_columns",
                    lock=getattr(dal, "_lock", None),
                )
            except Exception:  # noqa: BLE001
                logger.debug("attention column write failed", exc_info=True)
                # Do NOT cache failure as success: a later attempt must retry.
                return
        _ATTENTION_COLUMNS_READY.add(key)


def _lookup_by_ref(dal: Any, ref: str) -> dict[str, Any] | None:
    getter = getattr(dal, "get_session", None)
    if callable(getter):
        row = getter(ref)
        if row is not None:
            return row
    by_ref = getattr(dal, "get_session_by_ref", None)
    if callable(by_ref):
        row = by_ref(ref)
        if row is not None:
            return row
    return None


def _locate_session(
    dal: Any, session_id: str | None, session_ref: str | None
) -> dict[str, Any] | None:
    """Resolve by session_id then session_ref. No cwd fallback (cross-session trap)."""
    if session_id:
        row = _lookup_by_ref(dal, session_id)
        if row is not None:
            return row
    if session_ref:
        row = _lookup_by_ref(dal, session_ref)
        if row is not None:
            return row
    return None


def _is_integrity_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    name = type(exc).__name__
    return name == "IntegrityError" or "unique" in str(exc).lower()


def _create_external_session(
    dal: Any, *, cwd: str, session_id: str | None, session_ref: str | None, title: str | None
) -> dict[str, Any]:
    now = utc_now_iso()
    canonical_id = new_id("ses")
    ref = session_ref or session_id or f"ext:{canonical_id}"
    existing = _lookup_by_ref(dal, ref)
    if existing is not None:
        return existing
    row = {
        "id": canonical_id,
        "source": "external",
        "project_dir": cwd,
        "provider": "claude",
        "session_ref": ref,
        "state": "running",
        "pid": None,
        "model": None,
        "title": title,
        "budget_usd_max": None,
        "cost_usd": None,
        "kill_requested": 0,
        "last_activity_at": now,
        "created_at": now,
        "updated_at": now,
    }
    try:
        dal.create_session(row)
    except Exception as exc:
        if not _is_integrity_error(exc):
            raise
        raced = _lookup_by_ref(dal, ref)
        if raced is not None:
            return raced
        raise
    getter = getattr(dal, "get_session", None)
    if callable(getter):
        created = getter(canonical_id)
        if created is not None:
            return created
    return row


def _write_attention(
    dal: Any,
    session_id: str,
    *,
    attention_state: str | None,
    attention_reason: str | None,
    attention_since: str | None,
) -> bool:
    _ensure_attention_columns(dal)
    setter = getattr(dal, "set_session_attention", None)
    if callable(setter):
        result = setter(
            session_id,
            attention_state=attention_state,
            attention_reason=attention_reason,
            attention_since=attention_since,
        )
        return True if result is None else bool(result)
    updater = getattr(dal, "_update_fields", None)
    fields = {
        "attention_state": attention_state,
        "attention_reason": attention_reason,
        "attention_since": attention_since,
    }
    if callable(updater):
        result = updater(session_id, fields)
        return True if result is None else bool(result)
    store = getattr(dal, "sessions", None)
    if isinstance(store, dict) and session_id in store:
        store[session_id].update(fields)
        if attention_since is not None:
            store[session_id]["updated_at"] = attention_since
        return True
    return False


def _terminalize_ended(dal: Any, canonical_id: str) -> bool:
    """Match ingest_session: SessionEnd closes the row so board cards stop respawning."""
    term = getattr(dal, "terminalize_session", None)
    if callable(term):
        return bool(term(canonical_id, "completed", void_note="claude code session ended"))
    store = getattr(dal, "sessions", None)
    if isinstance(store, dict) and canonical_id in store:
        store[canonical_id]["state"] = "completed"
        return True
    return False


def _ntfy_url_from_connections_env(path: Path | None = None) -> str:
    """Last ``OMNI_NTFY_URL=`` line from connections.env (same as notify-owner.sh / run-loop)."""
    target = path if path is not None else _CONNECTIONS_ENV_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    value = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        if line.startswith("OMNI_NTFY_URL="):
            value = line.split("=", 1)[1].strip().strip("'\"")
    return value


def ntfy_url() -> str:
    """``OMNI_NTFY_URL`` from the environment, else ``~/.config/omni/connections.env``.

    Never logs the resolved value — the URL is a secret topic.
    """
    env = ntfy_url_from_env()
    if env:
        return env
    return _ntfy_url_from_connections_env()


def _latin1_header(value: str) -> str:
    """urllib/http.client headers must be latin-1 and single-line."""
    cleaned = value.replace("\r", " ").replace("\n", " ")
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def _push_attention_ntfy(title: str, body: str) -> None:
    endpoint = ntfy_url()
    if not endpoint:
        logger.info("attention push skipped -- OMNI_NTFY_URL unset")
        return
    data = (body or "").encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _latin1_header(title[:200]),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_PUSH_TIMEOUT_S) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
        if not isinstance(status, int) or not (200 <= status < 300):
            logger.warning("attention push returned non-2xx")
    except Exception:  # noqa: BLE001 -- presentation never fails the hook
        logger.warning("attention push failed", exc_info=True)


def _ntfy_should_push(session_id: str, state: str) -> bool:
    now = time.monotonic()
    key = (session_id, state)
    with _NTFY_DEDUP_LOCK:
        expired = [item for item, stamp in _NTFY_DEDUP.items() if now - stamp >= _NTFY_DEDUP_S]
        for item in expired:
            del _NTFY_DEDUP[item]
        last = _NTFY_DEDUP.get(key)
        if last is not None and now - last < _NTFY_DEDUP_S:
            return False
        _NTFY_DEDUP[key] = now
        while len(_NTFY_DEDUP) > _NTFY_DEDUP_MAX:
            oldest = min(_NTFY_DEDUP, key=lambda k: _NTFY_DEDUP[k])
            del _NTFY_DEDUP[oldest]
    return True


def _push_worker() -> None:
    global _PUSH_WORKER_STARTED
    try:
        while True:
            try:
                title, body = _PUSH_QUEUE.get()
                _push_attention_ntfy(title, body)
            except Exception:  # noqa: BLE001 -- keep serving; one bad item never kills the worker
                logger.warning("attention push worker failed", exc_info=True)
    finally:
        # Reached only on interpreter teardown or a non-Exception raise: reset
        # the flag so the next _ensure_push_worker() can respawn a worker
        # instead of stranding every future alert in the queue.
        with _PUSH_WORKER_LOCK:
            _PUSH_WORKER_STARTED = False


def _ensure_push_worker() -> None:
    global _PUSH_WORKER_STARTED
    with _PUSH_WORKER_LOCK:
        if _PUSH_WORKER_STARTED:
            return
        thread = threading.Thread(target=_push_worker, daemon=True, name="attention-ntfy-worker")
        thread.start()
        _PUSH_WORKER_STARTED = True


def _schedule_attention_push(title: str, body: str, session_id: str, state: str | None) -> None:
    if state is None:
        return
    if not _ntfy_should_push(session_id, state):
        return
    try:
        _ensure_push_worker()
        _PUSH_QUEUE.put_nowait((title, body))
    except Full:
        logger.warning("attention push queue full -- dropping")
    except Exception:  # noqa: BLE001 -- presentation never fails the hook
        logger.warning("attention push schedule failed", exc_info=True)


def _push_title(cwd: str, session: dict[str, Any]) -> str:
    cwd_name = Path(cwd).name or cwd or "session"
    label = _opt_str(session.get("title")) or str(session.get("id") or "session")
    return _latin1_header(f"{cwd_name} — {label}")


@router.post("/hook", response_model=SessionHookEventResponse)
def hook_session_event(
    body: SessionHookEventRequest,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
    x_session_hook_token: str | None = Header(None, alias="X-Session-Hook-Token"),
) -> dict[str, Any]:
    """Record attributed attention for one Claude Code hook event."""
    session_id = _opt_str(body.session_id)
    session_ref = _opt_str(body.session_ref)
    cwd = body.cwd.strip()

    dal = get_sessions_dal()
    session = _locate_session(dal, session_id, session_ref)
    if not _hook_eval_authorized(session, x_session_token, x_session_hook_token):
        fail(401, "unauthorized", "invalid session token")

    event = (body.event or "").strip()
    if event not in _HOOK_EVENTS:
        fail(400, "invalid_event", "unknown hook event", {"event": body.event})

    message = _clip_text(_opt_str(body.message))
    title = _clip_text(_opt_str(body.title))

    if session is None:
        session = _create_external_session(
            dal,
            cwd=cwd,
            session_id=session_id,
            session_ref=session_ref,
            title=title,
        )

    canonical_id = str(session.get("id") or "")
    if not canonical_id:
        fail(500, "internal", "session row is missing an id")

    state = _attention_state_for(event)
    if event == "stop" and session.get("attention_state") in (None, ""):
        return {"ok": True, "session_id": canonical_id}

    if event == "session_end":
        # Terminalize clears attention in the same transaction; a separate
        # attention write here would be redundant, and a session ending is not
        # an operator interruption — no push. Same 409-on-dropped-write
        # contract as _write_attention (F5's propagation to its sibling).
        if not _terminalize_ended(dal, canonical_id):
            fail(
                409,
                "invalid_state",
                "terminalize did not land (row already terminal?)",
                {"id": canonical_id},
            )
        return {"ok": True, "session_id": canonical_id}

    since = None if state is None else utc_now_iso()
    reason = None if state is None else message
    landed = _write_attention(
        dal,
        canonical_id,
        attention_state=state,
        attention_reason=reason,
        attention_since=since,
    )
    if not landed:
        fail(
            409,
            "invalid_state",
            "attention write did not land",
            {"id": canonical_id},
        )
    # session.updated is SSE-synthesized from sessions.updated_at (eventbus
    # tailer). set_session_attention -> _update_fields stamps updated_at.
    # last_activity_at is deliberately not touched (idle reaper key).

    _schedule_attention_push(_push_title(cwd, session), message or "", canonical_id, state)

    return {"ok": True, "session_id": canonical_id}
