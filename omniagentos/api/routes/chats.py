"""HTTP surface for chats, companion board tasks, and chat orchestration.

Endpoints:
  POST   /api/chats              — create a chat workspace (+ optional board_task_id link)
  GET    /api/chats              — list chats (ChatDTO[]; project/folder filters)
  GET    /api/chats/folders      — folder registry {folders:[{name,color,chat_count}]}
  POST   /api/chats/folders/{name}/rename — rename/merge a folder (moves member chats)
  POST   /api/chats/folders/{name}/color  — set/register a folder color token
  DELETE /api/chats/folders/{name}        — delete a folder (chats fall back to Inbox)
  GET    /api/chats/{id}         — ChatDTO
  PATCH  /api/chats/{id}         — update title/status/project/model/orch/plan/routing/meta
  DELETE /api/chats/{id}         — soft delete (status='deleted')
  POST   /api/chats/{id}/messages — send a user message + dispatch/steer/fan-out
  GET    /api/chats/{id}/messages — full conversation log (user + agent turns),
                                    CHRONOLOGICAL — the same order
                                    ``GET /api/board/{task_id}/conversation``
                                    now returns, so a component that renders
                                    both does not have to know which it holds
  POST   /api/chats/{id}/classify — project suggestion (never writes project_id)
  POST   /api/chats/{id}/intent/suggest — chat|project|loop intent + promotion (INTENT-1)
  POST   /api/chats/{id}/plan    — plan-mode seed (records meta.plan_job_id)
  POST   /api/chats/{id}/spawn   — fan out sub-agents as children of companion task
  POST   /api/chats/{id}/promote — extract action items into board tasks via LLM
  POST   /api/chats/{id}/promote_item — promote one extracted item by index

All chat responses are projected to the ChatDTO (§3.1): flat authoritative
fields (project_id, folder, preferred_model, orch_mode, plan_mode, routing,
project_suggestion, message_count, last_message_at) + ``meta`` retained.
``folder`` is projected out of ``meta_json`` on the way out (see
``_with_folder``); storage is unchanged.

SSE: chat.turn.started is emitted here on send; chat.turn.delta /
chat.turn.completed are emitted by the ChatTurnBridge only (§3.7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from omniagentos.api.deps import PolicyDep, StoreDep
from omniagentos.api.routes.collab import CollabStoreDep
from omniagentos.api.routes.control import fail
from omniagentos.api.routes.intake import (
    HierarchyDep,
    PlannerLLMDep,
    RouterLLMDep,
    _plan_job_create,
    _run_plan_job,
)
from omniagentos.chats import (
    BoardTaskConflictError,
    ChatError,
    ChatStore,
    UnknownBoardTaskError,
    UnknownFolderError,
    UnknownProjectError,
)
from omniagentos.chats.bridge import ChatBridgeFull, get_chat_turn_bridge
from omniagentos.chats.store import PROJECT_UNSET
from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.conversations.store import ConversationError, ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import ExecuteMode, dispatch_spec
from omniagentos.swarm.prompt_safety import fence_data_block
from omniagentos.workfs import WorkfsError
from omniagentos.workfs import ensure as workfs_ensure

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["chats"])

# -- attachment manifest validation ------------------------------------------
_VALID_ATTACHMENT_KINDS = frozenset({"skill", "file", "url"})
_VALID_ORCH_MODES = frozenset({"auto", "solo", "fanout"})
_VALID_SPEEDS = frozenset({"fast", "auto", "ultra"})
_VALID_EFFORTS = frozenset({"low", "medium", "high"})
_VALID_PROMOTE_EXECUTES = frozenset({"readonly", "tools", "session"})
CHAT_HISTORY_MAX_TURNS = int(os.getenv("CHAT_HISTORY_MAX_TURNS", "40"))
CHAT_HISTORY_MAX_TOKENS = int(os.getenv("CHAT_HISTORY_MAX_TOKENS", "1000"))
_CHAT_ROLE_TAG_RE = re.compile(r"<(\/?)(Operator|Agent)>")
# workfs scope is exactly three levels: company → department → subfolder.
_MAX_WORK_FOLDER_DEPTH = 3
_ACTIVE_SESSION_STATES = frozenset(
    {"queued", "planning", "starting", "running", "awaiting_approval", "resuming"}
)

_SESSIONS_DALS: dict[str, Any] = {}
_SESSIONS_DALS_LOCK = threading.Lock()


def _chat_store(store: StoreDep) -> ChatStore:
    """Chat DAL over the process-wide store (a SqliteStore at runtime, see api.deps)."""
    return ChatStore(cast(SqliteStore, store))


def _conversation_store(store: StoreDep) -> ConversationStore:
    """Conversation DAL over the process-wide store (a SqliteStore at runtime)."""
    return ConversationStore(cast(SqliteStore, store))


def _sanitize_chat_content(content: str) -> str:
    """Make role-looking tags in chat content visibly data, not structure."""

    return _CHAT_ROLE_TAG_RE.sub(r"< \1\2 >", content)


def _estimated_tokens(value: str) -> int:
    """Estimate rendered tokens without adding a tokenizer dependency."""

    return max(1, int(len(value.split()) * 1.3)) if value else 0


def _build_bounded_chat_history(
    messages: list[dict[str, Any]], max_turns: int, max_tokens: int
) -> tuple[str, int]:
    """Render recent chat turns within hard turn and estimated-token limits."""

    if max_turns < 0 or max_tokens < 0:
        raise ValueError("chat history caps must be non-negative")

    selected: list[str] = []
    for message in reversed(messages):
        if len(selected) >= max_turns:
            break
        role_label = "Operator" if message.get("role") == "user" else "Agent"
        content = _sanitize_chat_content(str(message.get("content", "")))
        rendered = f"<{role_label}>\n{content}\n</{role_label}>"
        candidate = "\n\n".join([*selected, rendered])
        if _estimated_tokens(candidate) > max_tokens:
            break
        selected.append(rendered)

    return "\n\n".join(selected), len(messages) - len(selected)


def _store_db_path(store: StoreDep) -> str:
    return str(getattr(store, "_db_path", None) or default_db_path())


def _sessions_dal_for(store: StoreDep) -> Any | None:
    """SessionsDal bound to the API store's own database (hermetic in tests)."""
    db_path = _store_db_path(store)
    with _SESSIONS_DALS_LOCK:
        dal = _SESSIONS_DALS.get(db_path)
        if dal is not None:
            return dal
    try:
        from omniagentos.sessions.dal import SessionsDal

        dal = SessionsDal(db_path)
    except Exception:  # noqa: BLE001
        _LOG.debug("sessions DAL unavailable for %s", db_path, exc_info=True)
        return None
    with _SESSIONS_DALS_LOCK:
        return _SESSIONS_DALS.setdefault(db_path, dal)


def _emit_chat_event(
    store: StoreDep,
    event_type: str,
    *,
    chat_id: str,
    task_id: str,
    turn: int,
    payload: dict[str, Any] | None = None,
) -> None:
    """Write a chat SSE event to the events table so the hub tailer fans it out."""
    event_payload = {
        "chat_id": chat_id,
        "task_id": task_id,
        "turn": turn,
        **(payload or {}),
    }
    try:
        store.insert_event(
            event_type,
            "api",
            "chat_turn",
            target_type="chat",
            target_id=chat_id,
            payload=event_payload,
        )
    except Exception:  # noqa: BLE001
        _LOG.debug("failed to emit chat event %s for %s", event_type, chat_id, exc_info=True)


def _emit_chat_updated(store: StoreDep, chat_id: str) -> None:
    """Emit ``chat.updated`` so clients learn about server-side mutations.

    Pinned shape (FINAL-PLAN.md §B / chat-v2 SPEC PART 3):
    ``{type: 'chat.updated', chat_id: str, ts: str}``.

    The frontend's ``useChatRefreshSignal`` refreshes the chat list when an
    event with ``target_type === "chat"`` arrives. Without this emission,
    title-from-first-message and classify results are invisible to other
    tabs until a manual refresh.
    """
    try:
        store.insert_event(
            "chat.updated",
            "api",
            "chat_updated",
            target_type="chat",
            target_id=chat_id,
            payload={"chat_id": chat_id, "ts": utc_now_iso()},
        )
    except Exception:  # noqa: BLE001
        _LOG.debug("failed to emit chat.updated for %s", chat_id, exc_info=True)


def _validate_attachments(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate attachment manifest on message meta; return cleaned meta or raise."""
    if meta is None:
        return None
    attachments = meta.get("attachments")
    if attachments is None:
        return meta
    if not isinstance(attachments, list):
        raise ChatError("attachments must be a list")
    cleaned = []
    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            raise ChatError(f"attachment[{i}] must be an object")
        kind = att.get("kind")
        ref = att.get("ref")
        label = att.get("label")
        if kind not in _VALID_ATTACHMENT_KINDS:
            raise ChatError(
                f"attachment[{i}].kind must be one of {sorted(_VALID_ATTACHMENT_KINDS)}"
            )
        if not ref or not isinstance(ref, str):
            raise ChatError(f"attachment[{i}].ref is required")
        if not label or not isinstance(label, str):
            raise ChatError(f"attachment[{i}].label is required")
        cleaned.append({"kind": kind, "ref": ref, "label": label})
    return {**meta, "attachments": cleaned}


def _validate_routing(routing: dict[str, Any]) -> dict[str, Any]:
    """Validate the pinned routing blob (§3.2); returns a normalized copy."""
    out: dict[str, Any] = {"allow": [], "deny": [], "speed": None, "effort": None, "hint": None}
    for key in ("allow", "deny"):
        raw = routing.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ChatError(f"routing.{key} must be a list of strings")
        out[key] = list(raw)
    speed = routing.get("speed")
    if speed is not None and speed not in _VALID_SPEEDS:
        raise ChatError(f"routing.speed must be one of {sorted(_VALID_SPEEDS)} or null")
    out["speed"] = speed
    effort = routing.get("effort")
    if effort is not None and effort not in _VALID_EFFORTS:
        raise ChatError(f"routing.effort must be one of {sorted(_VALID_EFFORTS)} or null")
    out["effort"] = effort
    hint = routing.get("hint")
    if hint is not None and not isinstance(hint, str):
        raise ChatError("routing.hint must be a string or null")
    out["hint"] = hint
    return out


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateChatRequest(BaseModel):
    title: str = Field(..., min_length=1)
    project_id: str | None = None
    model: str | None = None
    board_task_id: str | None = None
    meta: dict[str, Any] | None = None


class UpdateChatRequest(BaseModel):
    title: str | None = None
    status: str | None = None
    project_id: str | None = None
    model: str | None = None
    orch_mode: str | None = None
    plan_mode: bool | None = None
    routing: dict[str, Any] | None = None
    folder: str | None = None
    meta: dict[str, Any] | None = None


class CreateMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    model: str | None = None
    orch_mode: str | None = None
    count: int = Field(default=1, ge=1, le=10)
    meta: dict[str, Any] | None = None


class SpawnRequest(BaseModel):
    goal: str = Field(..., min_length=1)
    count: int = Field(default=1, ge=1, le=10)


class RenameFolderRequest(BaseModel):
    new_name: str = Field(..., min_length=1)


class FolderColorRequest(BaseModel):
    color: str = Field(..., min_length=1)


class PromoteRequest(BaseModel):
    project_id: str | None = None
    new_project_title: str | None = None
    execute_mode: str | None = None
    item_index: int | None = Field(default=None, ge=0)


class ClassifyRequest(BaseModel):
    force: bool = False


class PlanSeedRequest(BaseModel):
    goal: str | None = None
    speed: str | None = None


class IntentSuggestRequest(BaseModel):
    """Optional message override for intent classification (INTENT-1)."""

    message: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_chat(
    body: CreateChatRequest,
    store: StoreDep,
) -> dict[str, Any]:
    """Create a new chat workspace and its companion board task.

    With ``board_task_id`` (§3.3 grow-a-chat) the chat links to an EXISTING
    board card instead: no companion is created, the card keeps its origin,
    and the chat inherits the card's project when none is passed.
    """
    chat_store = _chat_store(store)
    meta = dict(body.meta or {})
    if body.model:
        meta["preferred_model"] = body.model
    try:
        chat = chat_store.create_chat(
            title=body.title,
            project_id=body.project_id,
            meta=meta or None,
            board_task_id=body.board_task_id,
        )
        dto = chat_store.get_chat_dto(chat["id"])
        assert dto is not None
        return _with_folder(dto)
    except UnknownBoardTaskError as exc:
        fail(404, "not_found", str(exc))
    except BoardTaskConflictError as exc:
        fail(409, "conflict", str(exc))
    except ChatError as exc:
        fail(400, "validation", str(exc))


def _with_folder(dto: dict[str, Any]) -> dict[str, Any]:
    """Project ``meta.folder`` up to a first-class ``folder`` field on the DTO.

    A chat's folder is a first-class concept in this product — it has its own
    registry table, its own routes (rename/color/delete) and its own query
    parameter on this very endpoint — but on the wire it was buried inside the
    free-form ``meta`` blob, so every client had to know that ``folder`` lives
    at ``meta.folder`` while ``project_id`` lives at the top level.

    Read-only projection: ``meta_json`` remains the storage (no schema change,
    no migration) and ``meta`` still carries the raw value, so nothing that
    reads it today breaks. Unfiled chats report ``null``, never ``""`` — one
    absent value, not two.
    """
    raw = dto.get("meta")
    meta: dict[str, Any] = raw if isinstance(raw, dict) else {}
    folder = meta.get("folder")
    folder = folder.strip() if isinstance(folder, str) else ""
    return {**dto, "folder": folder or None}


@router.get("")
def list_chats(
    store: StoreDep,
    project_id: str | None = None,
    folder: str | None = None,
) -> list[dict[str, Any]]:
    """List all chat workspaces as ChatDTOs, optionally filtered.

    Each DTO carries ``folder`` as a first-class field (projected from
    ``meta.folder``; see :func:`_with_folder`), so a client can render the
    folder a chat is filed in without parsing ``meta``.
    """
    chat_store = _chat_store(store)
    return [
        _with_folder(dto)
        for dto in chat_store.list_chat_dtos(project_id=project_id, folder=folder)
    ]


def _folder_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a store folder entry to the wire shape {name, color, chat_count}."""
    return {
        "name": entry["name"],
        "color": entry["color"],
        "chat_count": entry["chat_count"],
    }


@router.get("/folders")
def list_folders(
    store: StoreDep,
) -> dict[str, Any]:
    """Folder registry with colors (088).

    Contract: ``{"folders": [{name, color, chat_count}]}`` — color is a
    palette token name (gray|red|orange|yellow|green|teal|blue|violet),
    never hex. Union of the registry and folders found on active chats,
    ordered by manual position (NULLs last) then name; registered folders
    appear even while empty.
    """
    chat_store = _chat_store(store)
    return {"folders": [_folder_payload(f) for f in chat_store.list_folder_registry()]}


@router.post("/folders/{name}/rename")
def rename_folder(
    name: str,
    body: RenameFolderRequest,
    store: StoreDep,
) -> dict[str, Any]:
    """Rename a folder — every member chat moves to the new name and the
    registry row is re-keyed (color/position kept). Renaming onto an
    existing folder merges into it; the target keeps its color.

    Contract: body ``{"new_name": string}`` → ``{"folder": {name, color,
    chat_count}}``. 404 unknown folder; 400 invalid name.
    """
    chat_store = _chat_store(store)
    try:
        members = [
            c["id"]
            for c in chat_store.list_chats(folder=name.strip())
        ]
        entry = chat_store.rename_folder(name, body.new_name)
    except UnknownFolderError as exc:
        fail(404, "not_found", str(exc))
    except ChatError as exc:
        fail(400, "validation", str(exc))
    for chat_id in members:
        _emit_chat_updated(store, str(chat_id))
    return {"folder": _folder_payload(entry)}


@router.post("/folders/{name}/color")
def set_folder_color(
    name: str,
    body: FolderColorRequest,
    store: StoreDep,
) -> dict[str, Any]:
    """Set (or register) a folder's color token. Upsert: an unknown name
    creates the registry row — this is the new-folder creation path.

    Contract: body ``{"color": token}`` → ``{"folder": {name, color,
    chat_count}}``. 400 invalid color token or name.
    """
    chat_store = _chat_store(store)
    try:
        entry = chat_store.set_folder_color(name, body.color)
    except ChatError as exc:
        fail(400, "validation", str(exc))
    return {"folder": _folder_payload(entry)}


@router.delete("/folders/{name}")
def delete_folder(
    name: str,
    store: StoreDep,
) -> dict[str, Any]:
    """Delete a folder — member chats fall back to the Inbox (folder
    cleared); the registry row is removed.

    Contract: ``{"deleted": name, "chats_moved": int}``. 404 unknown folder.
    """
    chat_store = _chat_store(store)
    try:
        members = [
            c["id"]
            for c in chat_store.list_chats(folder=name.strip())
        ]
        moved = chat_store.delete_folder(name)
    except UnknownFolderError as exc:
        fail(404, "not_found", str(exc))
    except ChatError as exc:
        fail(400, "validation", str(exc))
    for chat_id in members:
        _emit_chat_updated(store, str(chat_id))
    return {"deleted": name.strip(), "chats_moved": moved}


@router.get("/{chat_id}")
def get_chat(
    chat_id: str,
    store: StoreDep,
) -> dict[str, Any]:
    """Get a chat workspace's ChatDTO.

    Same shape as a list element, ``folder`` included — a detail read that
    dropped a field the list carries would just push the caller back to the
    list.
    """
    chat_store = _chat_store(store)
    chat = chat_store.get_chat_dto(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")
    return _with_folder(chat)


@router.patch("/{chat_id}")
def update_chat(
    chat_id: str,
    body: UpdateChatRequest,
    store: StoreDep,
) -> dict[str, Any]:
    """Update a chat workspace (§3.2). Omitted fields are unchanged.

    ``model`` → ``meta.preferred_model`` (null = auto); ``orch_mode`` /
    ``plan_mode`` / ``routing`` → their meta keys; ``project_id`` moves the
    chat AND mirrors onto the companion board card in one transaction.
    ``folder`` is back-compat for one release; the UI never sends it.
    """
    chat_store = _chat_store(store)
    if chat_store.get_chat(chat_id) is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")
    fields_set = body.model_fields_set
    meta = dict(body.meta or {})
    if body.folder is not None:
        meta["folder"] = body.folder if body.folder else ""
    if "model" in fields_set:
        # explicit null clears to auto; a value pins the chat's model
        meta["preferred_model"] = body.model
    if body.orch_mode is not None:
        if body.orch_mode not in _VALID_ORCH_MODES:
            fail(400, "validation", f"orch_mode must be one of {sorted(_VALID_ORCH_MODES)}")
        meta["orch_mode"] = body.orch_mode
    if body.plan_mode is not None:
        meta["plan_mode"] = body.plan_mode
    if body.routing is not None:
        try:
            meta["routing"] = _validate_routing(body.routing)
        except ChatError as exc:
            fail(400, "validation", str(exc))
    project_patch: Any = PROJECT_UNSET
    if "project_id" in fields_set:
        project_patch = body.project_id
    try:
        chat_store.update_chat(
            chat_id=chat_id,
            title=body.title,
            status=body.status,
            meta=meta or None,
            project_id=project_patch,
        )
        dto = chat_store.get_chat_dto(chat_id)
        assert dto is not None
        return _with_folder(dto)
    except UnknownProjectError as exc:
        fail(404, "not_found", str(exc))
    except ChatError as exc:
        fail(400, "validation", str(exc))


@router.delete("/{chat_id}")
def delete_chat(
    chat_id: str,
    store: StoreDep,
) -> dict[str, Any]:
    """Soft-delete a chat workspace (status='deleted', excluded from lists)."""
    chat_store = _chat_store(store)
    try:
        chat_store.soft_delete_chat(chat_id)
        dto = chat_store.get_chat_dto(chat_id)
        assert dto is not None
        return _with_folder(dto)
    except ChatError as exc:
        fail(404, "not_found", str(exc))


def _spawn_subtasks(
    chat_store: ChatStore,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
    chat: dict[str, Any],
    goal: str,
    count: int,
) -> list[str]:
    """Fan out sub-agents under the chat's companion task; returns task ids.

    Children inherit the chat's ``project_id`` (ChatStore.create_spawn_task,
    §3.8) so they land visible in their project on the board (P0-9).
    """
    chat_id = str(chat["id"])
    task_ids: list[str] = []
    model = chat.get("meta", {}).get("preferred_model")
    for i in range(count):
        title = f"Spawn: {goal}" if count == 1 else f"Spawn {i + 1}/{count}: {goal}"
        description = (
            f"Sub-agent spawned from chat {chat_id}.\n\n"
            f"Goal: {goal}\n\n"
            f"Parent: companion task {chat['board_task_id']}"
        )
        try:
            task_id = chat_store.create_spawn_task(
                chat_id=chat_id,
                title=title,
                description=description,
            )
        except ChatError as exc:
            fail(400, "validation", str(exc))

        spec = RefinedSpec(title=title, description=goal)
        try:
            dispatch_spec(
                store=store,
                collab_store=collab_store,
                policy_cfg=policy_cfg,
                spec=spec,
                project_id=chat["project_id"],
                execute="session",
                board_task_id=task_id,
                model=model,
            )
        except Exception as exc:
            _LOG.warning(
                "spawn dispatch failed for sub-task %s of chat %s: %s",
                task_id, chat_id, exc,
            )

        task_ids.append(task_id)
    return task_ids


def _active_session_for_task(store: StoreDep, board_task_id: str | None) -> tuple[Any, dict[str, Any]] | tuple[None, None]:
    """Return (dal, session) when the card's result_ref is an ACTIVE session."""
    if not board_task_id:
        return None, None
    try:
        row = cast(SqliteStore, store)._connection.execute(
            "SELECT result_ref FROM board_tasks WHERE id = ?", (board_task_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None, None
    ref = str(row["result_ref"]) if row and row["result_ref"] else ""
    if not ref.startswith("ses_"):
        return None, None
    dal = _sessions_dal_for(store)
    if dal is None:
        return None, None
    try:
        session = dal.get_session(ref)
    except Exception:  # noqa: BLE001
        return None, None
    if session is None or str(session.get("state")) not in _ACTIVE_SESSION_STATES:
        return None, None
    return dal, session


def _routing_pins(routing: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Render a chat's routing prefs into dispatch ``pins`` + ``speed``."""
    pins: dict[str, Any] = {}
    if routing.get("hint"):
        pins["hint"] = routing["hint"]
    if routing.get("allow"):
        pins["allow"] = list(routing["allow"])
    if routing.get("deny"):
        pins["deny"] = list(routing["deny"])
    speed = routing.get("speed") or None
    return (pins or None), speed


def _first_message_hooks(store: StoreDep, chat: dict[str, Any], content: str) -> None:
    """Title-from-first-message + fire-and-forget project classify (§3.4.6).

    Both branches emit ``chat.updated`` so other SSE subscribers learn about
    server-side mutations without polling (Q-FIX-01).
    """
    chat_store = _chat_store(store)
    title_changed = False
    try:
        if str(chat.get("title") or "") == "New chat":
            title = content.strip().replace("\n", " ")[:60].strip()
            if title:
                chat_store.update_chat(chat["id"], title=title)
                title_changed = True
    except ChatError:
        _LOG.debug("title hook failed for %s", chat.get("id"), exc_info=True)

    if title_changed:
        _emit_chat_updated(store, str(chat["id"]))

    meta = chat.get("meta") or {}
    if (
        not chat.get("project_id")
        and not meta.get("project_suggestion")
        and not meta.get("classified_at")
    ):
        def _classify() -> None:
            try:
                from omniagentos.chats.classify import classify_chat_project

                classify_chat_project(cast(SqliteStore, store), str(chat["id"]))
                _emit_chat_updated(store, str(chat["id"]))
            except Exception:  # noqa: BLE001
                _LOG.debug("classify hook failed for %s", chat.get("id"), exc_info=True)

        threading.Thread(target=_classify, name="chat-classify", daemon=True).start()


@router.post("/{chat_id}/messages", status_code=201)
def create_message(
    chat_id: str,
    body: CreateMessageRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    """Post a message to a chat and route the agent turn (§3.4).

    Orchestration resolves as ``body.orch_mode > chat orch_mode > "solo"``:

    * ``fanout`` — routes to the spawn path; returns ``task_ids``.
    * ``solo``/``auto`` — when the linked card's ``result_ref`` session is
      active, the message STEERS the live session (steer-when-live, §2.5) and
      the bridge registers on that same session; otherwise it dispatches
      (``execute="session"`` for solo, ``execute=None`` for auto — the
      dispatch's documented auto-default). ``chat.turn.started`` fires on
      send; ``delta``/``completed`` come from the ChatTurnBridge.
    """
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    try:
        meta = _validate_attachments(body.meta)
    except ChatError as exc:
        fail(400, "validation", str(exc))

    # Resolve model: per-message override > chat-level preferred > None (default)
    model = body.model or chat.get("meta", {}).get("preferred_model")

    conv_store = _conversation_store(store)
    try:
        user_turn = conv_store.append(
            scope_type="chat",
            scope_id=chat_id,
            role="user",
            content=body.content,
            model=model,
            meta=meta,
        )
    except ConversationError as exc:
        fail(400, "validation", str(exc))

    # INTENT-1: agreement log far side (events plumbing). Failure never blocks send.
    _maybe_log_intent_agreement(store, chat_id, meta)

    # Resolve orchestration: per-send override > chat-level > solo
    chat_orch = chat.get("meta", {}).get("orch_mode")
    orch = body.orch_mode or (chat_orch if chat_orch in _VALID_ORCH_MODES else None) or "solo"
    if orch not in _VALID_ORCH_MODES:
        fail(400, "validation", f"orch_mode must be one of {sorted(_VALID_ORCH_MODES)}")

    board_task_id = chat["board_task_id"]
    turn_seq = user_turn.get("seq", 0)

    if orch == "fanout":
        task_ids = _spawn_subtasks(
            chat_store, store, collab_store, policy_cfg, chat, body.content, body.count
        )
        _first_message_hooks(store, chat, body.content)
        return {"message": user_turn, "task_ids": task_ids}

    # Build the prompt with chat history as context
    messages = conv_store.read("chat", chat_id)
    bounded_history, dropped = _build_bounded_chat_history(
        messages, CHAT_HISTORY_MAX_TURNS, CHAT_HISTORY_MAX_TOKENS
    )
    # This block shares the downstream prompt-composition path with intake's
    # _compose_prompt; keep chat history fenced as untrusted data at this seam.
    chat_history_prompt = fence_data_block("CHAT_HISTORY", bounded_history)

    scope = _promotion_context(store, chat, chat.get("project_id"))
    spec = RefinedSpec(
        title=chat["title"],
        description=_promotion_description(chat_history_prompt, scope),
        metadata={"chat_turns_dropped": dropped},
    )

    # Emit chat.turn.started SSE event
    _emit_chat_event(
        store,
        "chat.turn.started",
        chat_id=chat_id,
        task_id=board_task_id,
        turn=turn_seq,
        payload={"model": model},
    )

    bridge = get_chat_turn_bridge()

    # Steer-when-live (§2.5/§3.4.4): an active session on the linked card
    # takes the message as steering instead of spawning a second agent.
    dal, live_session = _active_session_for_task(store, board_task_id)
    if dal is not None and live_session is not None:
        live_session_id = str(live_session["id"])
        try:
            dal.enqueue_message(live_session_id, body.content.strip())
            from omniagentos.sessions.steering_marker import mark_steering_pending

            mark_steering_pending(live_session_id)
        except Exception as exc:
            _LOG.exception("steer enqueue failed for chat %s", chat_id)
            fail(500, "server_error", f"failed to steer live session: {exc}")
        try:
            bridge.register(
                chat_id,
                live_session_id,
                board_task_id,
                turn_seq,
                store=cast(SqliteStore, store),
                model=model,
                dal=dal,
            )
        except ChatBridgeFull as exc:
            fail(503, "capacity", str(exc))
        _first_message_hooks(store, chat, body.content)
        return {
            "message": user_turn,
            "dispatch": {
                "session_id": live_session_id,
                "task_id": board_task_id,
                "run_id": None,
                "board_task": None,
                "steered": True,
            },
        }

    pins, speed = _routing_pins(chat.get("meta", {}).get("routing") or {})

    # Dispatch the agent turn on the companion task
    try:
        dispatch_result = dispatch_spec(
            store=store,
            collab_store=collab_store,
            policy_cfg=policy_cfg,
            spec=spec,
            project_id=chat["project_id"],
            # solo == today's chat turn; auto == dispatch_spec's documented
            # auto-default (an omitted execute keeps the platform decision).
            execute="session" if orch == "solo" else None,
            board_task_id=board_task_id,
            model=model,
            pins=pins,
            speed=speed,
        )
    except Exception as exc:
        _LOG.exception("Failed to dispatch agent turn for chat %s", chat_id)
        fail(500, "server_error", f"failed to trigger agent turn: {exc}")

    session_id = dispatch_result.get("session_id")
    if session_id:
        try:
            bridge.register(
                chat_id,
                str(session_id),
                board_task_id,
                turn_seq,
                store=cast(SqliteStore, store),
                model=model,
                dal=_sessions_dal_for(store),
            )
        except ChatBridgeFull as exc:
            fail(503, "capacity", str(exc))

    _first_message_hooks(store, chat, body.content)

    return {
        "message": user_turn,
        "dispatch": {
            "session_id": session_id,
            "task_id": dispatch_result.get("task_id"),
            "run_id": dispatch_result.get("run_id"),
            "board_task": dispatch_result.get("board_task"),
            "steered": False,
        },
    }


@router.get("/{chat_id}/messages")
def list_messages(
    chat_id: str,
    store: StoreDep,
) -> list[dict[str, Any]]:
    """List all messages in a chat workspace (user + agent turns)."""
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    conv_store = _conversation_store(store)
    return conv_store.read("chat", chat_id)


@router.post("/{chat_id}/classify")
def classify_chat(
    chat_id: str,
    store: StoreDep,
    body: ClassifyRequest | None = None,
) -> dict[str, Any]:
    """Classify the chat to a suggested project (§3.5).

    Suggestion-only: stores ``meta.project_suggestion`` + ``classified_at``;
    NEVER writes ``chats.project_id``. Confidence < 0.5 is discarded
    server-side and returns nulls.
    """
    chat_store = _chat_store(store)
    if chat_store.get_chat(chat_id) is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")
    from omniagentos.chats.classify import classify_chat_project

    suggestion = classify_chat_project(
        cast(SqliteStore, store), chat_id, force=bool(body and body.force)
    )
    if suggestion is None:
        return {"project_id": None, "name": None, "confidence": 0.0, "rationale": ""}
    return {
        "project_id": suggestion["project_id"],
        "name": suggestion["name"],
        "confidence": suggestion["confidence"],
        "rationale": suggestion["rationale"],
    }


@router.post("/{chat_id}/intent/suggest")
def _suggest_chat_intent(
    chat_id: str,
    store: StoreDep,
    body: IntentSuggestRequest | None = None,
) -> dict[str, Any]:
    """Suggest chat vs project vs loop intent (INTENT-1).

    Returns ``{intent, confidence, promotion}`` where ``promotion`` is
    ``shadow|promoted`` from the D4 evaluator over logged agreement — never
    from classifier confidence (F12). Suggestion-only: does not write
    ``project_id`` and never creates or enables a routine.

    Private symbol: wired solely as the FastAPI route handler (GLA-11).
    """
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    from omniagentos.chats.intent import suggest_intent

    message = (body.message if body is not None else None) or None
    result = suggest_intent(
        cast(SqliteStore, store),
        chat_id,
        message=message,
        title=str(chat.get("title") or ""),
    )
    return {
        "intent": result["intent"],
        "confidence": result["confidence"],
        "promotion": result["promotion"],
    }


def _maybe_log_intent_agreement(
    store: StoreDep,
    chat_id: str,
    meta: dict[str, Any] | None,
) -> None:
    """Log ``{suggested_intent, chosen_intent}`` when a send carries both.

    Rides existing events plumbing (zero new schema). Never raises.
    """
    if not meta:
        return
    suggested = meta.get("suggested_intent")
    chosen = meta.get("chosen_intent")
    if not suggested or not chosen:
        return
    try:
        from omniagentos.chats.intent import log_agreement

        log_agreement(
            store,
            chat_id=chat_id,
            suggested_intent=str(suggested),
            chosen_intent=str(chosen),
        )
    except Exception:  # noqa: BLE001
        _LOG.debug("intent agreement hook failed for %s", chat_id, exc_info=True)


@router.post("/{chat_id}/plan", status_code=202)
def seed_plan(
    chat_id: str,
    body: PlanSeedRequest,
    background_tasks: BackgroundTasks,
    store: StoreDep,
    hierarchy: HierarchyDep,
    planner_llm: PlannerLLMDep,
    router_llm: RouterLLMDep,
) -> dict[str, Any]:
    """Plan-mode seed (§3.6): delegate to the existing plan-job machinery.

    ``goal`` defaults to the last user message + thread title. The job id is
    recorded to ``meta.plan_job_id`` so a reload re-attaches to a running job;
    poll/confirm use the existing ``/api/intake/plan`` endpoints unchanged.
    """
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    goal = (body.goal or "").strip()
    if not goal:
        conv_store = _conversation_store(store)
        messages = conv_store.read("chat", chat_id)
        last_user = next(
            (m for m in reversed(messages) if m["role"] == "user"), None
        )
        seed = (last_user["content"] if last_user else "") or str(chat.get("title") or "")
        goal = f"{chat.get('title') or 'Chat plan'}\n\n{seed}".strip()
    if not goal:
        fail(400, "validation", "goal is required")
    speed = body.speed if body.speed in _VALID_SPEEDS else None

    job_id = _plan_job_create(goal, None, "session", speed)
    background_tasks.add_task(
        _run_plan_job,
        job_id,
        goal,
        hierarchy,
        planner_llm,
        router_llm,
        speed,
    )
    try:
        chat_store.update_chat(chat_id, meta={"plan_job_id": job_id})
    except ChatError:
        _LOG.debug("could not record plan_job_id for %s", chat_id, exc_info=True)
    return {"job_id": job_id, "status": "running"}


@router.post("/{chat_id}/spawn", status_code=201)
def spawn_agents(
    chat_id: str,
    body: SpawnRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    """Fan out sub-agents from a chat thread.

    Each spawned agent becomes a board task nested under the chat's companion
    task (``origin='chat'``, parent link in ``org_json``). They are real work:
    visible on the board in the chat's project (P0-9).

    Pinned contract: body ``{"goal": string, "count"?: int}`` →
    ``{"task_ids": [string]}``.
    """
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    task_ids = _spawn_subtasks(
        chat_store, store, collab_store, policy_cfg, chat, body.goal, body.count
    )
    return {"task_ids": task_ids}


def _normalized_identity_text(text: str) -> str:
    """Whitespace-normalised text — the basis of a promoted item's identity."""
    return " ".join(str(text).split())


def _promoted_item_key(chat_id: str, kind: str, text: str, occurrence: int = 0) -> str:
    """Content-derived identity for one promoted item.

    Keyed on the SOURCE MESSAGE an item was extracted from — never on the
    extractor's own prose (an LLM re-words the same ask on every call) and never
    on a positional index (a growing thread, or a deleted message, shifts every
    index after it). ``kind`` separates the message-derived namespace from the
    item-derived fallback so the two can never collide. ``occurrence``
    disambiguates a second/third item extracted from the SAME message; it is
    omitted from the digest for the first one, so the common one-item-per-message
    case hashes exactly ``sha256(chat_id \\0 msg \\0 content)``.

    Exactly what it does and does not survive (all asserted in
    ``tests/chats/test_promote.py``):

    * survives — LLM re-wording, the thread growing, another message being
      deleted, and whitespace-only re-spacing of the same ask.
    * deliberately does NOT survive — an EDIT to the source message: an edited
      ask is a different ask, so a re-promote makes a new card rather than
      re-pointing the old one at prose the operator has since changed.
    * the chat id is inside the digest, so identical text in two chats never
      collides; the digest is over BYTES, so NFC and NFD spellings of the same
      text are different identities (documented, not normalised away).
    """
    parts = [chat_id, kind, _normalized_identity_text(text)]
    if occurrence:
        parts.append(f"#{occurrence}")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _coerce_message_index(raw: Any, message_count: int) -> int | None:
    """Validate an extractor-supplied message index: int, in range, or ``None``.

    ``bool`` is rejected explicitly (it is an ``int`` subclass). Out-of-range and
    unparseable values return ``None`` so the caller can fall back — never to 0,
    which would silently key every item to the first message.
    """
    index: int | None = None
    if isinstance(raw, bool):
        index = None
    elif isinstance(raw, int):
        index = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            index = int(text)
    if index is None or not (0 <= index < message_count):
        return None
    return index


@dataclass
class _ItemGate:
    """One in-process gate for a single ``(chat_id, item_key)`` promotion."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    waiters: int = 0


_PROMOTE_GATES: dict[tuple[str, str], _ItemGate] = {}
_PROMOTE_GATES_GUARD = threading.Lock()


@contextmanager
def _promote_item_gate(chat_id: str, item_key: str) -> Iterator[None]:
    """Serialise lookup → reserve → dispatch for ONE item of ONE chat.

    An OPTIMISATION, not the correctness boundary. Correctness lives in
    ``ChatStore.reserve_promoted_card``, whose single
    ``INSERT … SELECT … WHERE NOT EXISTS`` creates the card and takes the item
    claim together, before any executor exists — so the loser of a race never
    reaches ``dispatch_spec`` whether or not this gate is held. That is asserted
    with the gate stubbed out, in-process and across two real processes
    (``tests/chats/test_promote.py``).

    What the gate buys: two threads promoting the same item do not both run the
    dedupe lookup, the workfs ensure and a doomed INSERT. It is keyed on the item,
    never on the chat or the process, so distinct items and distinct chats stay
    fully parallel and the heavy dispatch is only ever serialised against a
    genuine duplicate of itself.
    """
    key = (chat_id, item_key)
    with _PROMOTE_GATES_GUARD:
        gate = _PROMOTE_GATES.get(key)
        if gate is None:
            gate = _ItemGate()
            _PROMOTE_GATES[key] = gate
        gate.waiters += 1
    gate.lock.acquire()
    try:
        yield
    finally:
        gate.lock.release()
        with _PROMOTE_GATES_GUARD:
            gate.waiters -= 1
            if gate.waiters <= 0:
                _PROMOTE_GATES.pop(key, None)


def _card_has_executor(card: dict[str, Any]) -> bool:
    """Whether a board card already points at a run or a session.

    ``result_ref`` carries a SESSION id on the session path (``intake/service.py``
    writes it immediately after ``spawner.spawn``, including from its own failure
    handler), ``run_id`` carries a run on the readonly/tools path.
    """
    if card.get("run_id"):
        return True
    ref = card.get("result_ref")
    return isinstance(ref, str) and ref.startswith("ses_")


def _is_stranded_reservation(card: dict[str, Any]) -> bool:
    """A reservation whose dispatch never attached anything — safe to retry onto.

    Reservations are inserted ``pending`` and only become ``open`` when
    ``dispatch_spec`` reuses the card, so ``pending`` + no executor + not archived
    means the process died between the two. Without this check every later promote
    would answer "already promoted" and the ask would be stranded forever.
    """
    return (
        str(card.get("status") or "") == BoardTaskStatus.PENDING.value
        and not _card_has_executor(card)
        and card.get("archived_at") is None
        and not card.get("claimed_by")
    )


def _settle_failed_dispatch(
    store: StoreDep,
    collab_store: CollabStoreDep,
    chat_store: ChatStore,
    task_id: str,
) -> None:
    """Decide what a reservation whose dispatch RAISED is allowed to leave behind.

    ``dispatch_spec`` can start an executor and then fail after it: session mode
    spawns before recording ``result_ref``, run mode creates the run before
    attaching ``run_id``. Releasing blindly would archive the card out of sight
    while its session kept working the operator's repo, and would free the item so
    the next retry started a SECOND executor — the same "a cancelled card is not a
    cancelled executor" defect that reserve-before-dispatch exists to kill.

    So: release ONLY when the card proves nothing was attached. When something was
    attached, ask the live work to stop (``pause_board_task_work`` — idempotent,
    best-effort) and KEEP the item held by a visible card, so a retry returns that
    card instead of racing a second executor against the survivor.
    """
    card = collab_store.get_board_task(task_id) or {}
    if not _card_has_executor(card):
        chat_store.release_promotion_reservation(task_id)
        return
    try:
        from omniagentos.intake.service import pause_board_task_work

        paused_run, paused_session = pause_board_task_work(
            cast(SqliteStore, store), card, sessions_dal=_sessions_dal_for(store)
        )
    except Exception:  # noqa: BLE001
        paused_run = paused_session = None
        _LOG.exception("promote: could not pause work on failed dispatch %s", task_id)
    _LOG.error(
        "promote dispatch failed AFTER starting work on card %s (run=%s session=%s); "
        "cancellation requested (run=%s session=%s). The item stays held by this card "
        "so a retry cannot start a second executor.",
        task_id,
        card.get("run_id"),
        card.get("result_ref"),
        paused_run,
        paused_session,
    )


def _resolve_item_identity(
    chat_id: str,
    items: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> list[tuple[str, int | None]]:
    """Map every extracted item onto ``(item_key, source_message_index|None)``.

    Runs over the WHOLE extraction (never a single selected item) so the
    occurrence counter is identical whether the operator promotes the thread or
    one item out of it.
    """
    seen: dict[str, int] = {}
    resolved: list[tuple[str, int | None]] = []
    for item in items:
        index = _coerce_message_index(item.get("source_message_index"), len(messages))
        if index is None:
            if item.get("source_message_index") is not None:
                _LOG.warning(
                    "promote: unusable source_message_index %r for %r; "
                    "keying on the item's own content instead",
                    item.get("source_message_index"),
                    item.get("title"),
                )
            kind = "item"
            text = f"{item.get('title', '')}\n{item.get('description', '')}"
        else:
            kind = "msg"
            text = str(messages[index].get("content", ""))
        base = _promoted_item_key(chat_id, kind, text)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        resolved.append((_promoted_item_key(chat_id, kind, text, occurrence), index))
    return resolved


@dataclass(frozen=True)
class _PromotionScope:
    """Operator-chosen company + Work-folder scope for a chat's promoted work."""

    company_name: str | None
    #: Normalised display form of ``meta.work_folder`` (e.g. ``"Acme/Product"``).
    work_folder: str | None
    #: The same value split into workfs scope components.
    work_segments: tuple[str, ...]


def _company_name_from_meta(store: StoreDep, meta: dict[str, Any]) -> str | None:
    """Resolve the company the operator chose in the composer (P0 — CB-PROMOTE).

    Lane C persists ``meta.org_company_id`` (the org company id) and
    ``meta.company_slug`` on the chat itself and never sets ``project_id`` on a
    fresh chat, so a project lookup alone cannot see the operator's choice.
    """
    connection = cast(SqliteStore, store)._connection
    company_id = meta.get("org_company_id")
    if isinstance(company_id, str) and company_id.strip():
        row = connection.execute(
            "SELECT name FROM org_companies WHERE id = ?", (company_id.strip(),)
        ).fetchone()
        if row is not None:
            return str(row["name"])
    slug = meta.get("company_slug")
    if isinstance(slug, str) and slug.strip():
        row = connection.execute(
            "SELECT name FROM org_companies WHERE slug = ?", (slug.strip(),)
        ).fetchone()
        if row is not None:
            return str(row["name"])
        # The operator explicitly picked this company; a stale registry row is no
        # reason to drop their choice from the brief.
        return slug.strip()
    return None


def _company_name_from_project(store: StoreDep, project_id: str | None) -> str | None:
    """Resolve the company via the chat's project (the pre-composer path)."""
    if not project_id:
        return None
    from omniagentos.projects import ProjectStore

    project = ProjectStore(cast(SqliteStore, store)).get_project(project_id)
    company_id = project.get("org_company_id") if project else None
    if not company_id:
        return None
    row = cast(SqliteStore, store)._connection.execute(
        "SELECT name FROM org_companies WHERE id = ?", (company_id,)
    ).fetchone()
    return None if row is None else str(row["name"])


def _promotion_context(
    store: StoreDep, chat: dict[str, Any], project_id: str | None
) -> _PromotionScope:
    """Resolve the operator-chosen company + Work folder for a promotion brief.

    ``meta.folder`` is deliberately NOT consulted: migration 088 defines it as a
    free-text, colour-coded CHAT LABEL. It is a display grouping for the sidebar
    and must never become a filesystem path or a working directory.
    """
    meta = chat.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    company_name: str | None = None
    try:
        company_name = _company_name_from_meta(store, meta)
        if company_name is None:
            company_name = _company_name_from_project(store, project_id)
    except Exception:  # noqa: BLE001
        _LOG.debug("company context unavailable for chat %s", chat.get("id"), exc_info=True)

    raw_folder = meta.get("work_folder")
    segments: tuple[str, ...] = ()
    if isinstance(raw_folder, str):
        segments = tuple(part.strip() for part in raw_folder.split("/") if part.strip())
    return _PromotionScope(
        company_name=company_name,
        work_folder="/".join(segments) if segments else None,
        work_segments=segments,
    )


def _promotion_description(description: str, scope: _PromotionScope) -> str:
    """Prefix an extracted task with its durable operator-facing chat context."""
    preamble_lines: list[str] = []
    if scope.company_name:
        preamble_lines.append(f"**Company**: {scope.company_name}")
    if scope.work_folder:
        preamble_lines.append(f"**Folder**: {scope.work_folder}")
    if not preamble_lines:
        return description
    return "\n".join([*preamble_lines, "", description])


def _ensure_promotion_workfs(scope: _PromotionScope) -> str | None:
    """Best-effort real directory creation for downstream promoted-task sessions.

    ``meta.work_folder`` is a WORK-root-RELATIVE path emitted by
    ``workfs.read_tree`` (``"Acme/Product"``), so its segments map onto the
    workfs scope levels directly; passing the whole slash-joined string as one
    component is rejected by ``scope._validate_component``. With no work folder
    the operator's company choice still gets its own directory. With neither,
    nothing is created — promotion never invents a folder out of a chat label.
    """
    segments = scope.work_segments
    if segments:
        if len(segments) > _MAX_WORK_FOLDER_DEPTH:
            _LOG.warning(
                "work_folder %r is %d levels deep; workfs scope is %d "
                "(company → department → subfolder). Skipping directory creation.",
                scope.work_folder,
                len(segments),
                _MAX_WORK_FOLDER_DEPTH,
            )
            return None
        company = segments[0]
        department = segments[1] if len(segments) > 1 else None
        subfolder = segments[2] if len(segments) > 2 else None
    elif scope.company_name:
        company, department, subfolder = scope.company_name, None, None
    else:
        return None
    try:
        return workfs_ensure(
            company=company, department=department, subfolder=subfolder
        ).path
    except WorkfsError as exc:
        _LOG.warning(
            "workfs ensure failed for chat promotion (company=%r folder=%r): %s",
            company,
            scope.work_folder,
            exc,
        )
        return None


def _promote_action_items(
    *,
    chat_id: str,
    body: PromoteRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
    single_item_required: bool,
) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    """Extract, deduplicate, and dispatch selected chat action items."""
    chat_store = _chat_store(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        fail(404, "not_found", f"chat {chat_id!r} not found")

    messages = _conversation_store(store).read("chat", chat_id)
    if not messages:
        fail(400, "validation", "cannot promote an empty chat thread")

    project_id = body.project_id if body.project_id is not None else chat.get("project_id")
    if body.new_project_title and project_id is None:
        try:
            from omniagentos.projects import ProjectStore

            new_project = ProjectStore(cast(SqliteStore, store)).create_project(
                {"name": body.new_project_title.strip()}
            )
            project_id = str(new_project["id"])
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("failed to create project for promotion: %s", exc)

    execute_mode = "readonly" if body.execute_mode is None else body.execute_mode.strip().lower()
    if execute_mode not in _VALID_PROMOTE_EXECUTES:
        fail(
            400,
            "validation",
            f"execute_mode must be one of {sorted(_VALID_PROMOTE_EXECUTES)}",
        )

    action_items = _extract_action_items(messages, chat["title"])
    # Identity is resolved over the FULL extraction before any selection, so a
    # single-item promote produces exactly the key the thread promote would.
    identities = _resolve_item_identity(chat_id, action_items, messages)
    if single_item_required and body.item_index is None:
        fail(400, "validation", "item_index is required for promote_item")
    if body.item_index is not None:
        if body.item_index >= len(action_items):
            fail(400, "validation", f"item_index {body.item_index} is out of range")
        action_items = [action_items[body.item_index]]
        identities = [identities[body.item_index]]

    scope = _promotion_context(store, chat, project_id)
    promoted: list[tuple[str, dict[str, Any]]] = []
    for item, (item_key, source_msg_idx) in zip(action_items, identities, strict=True):
        title = str(item.get("title", "")).strip()
        description = str(item.get("description", "")).strip()
        if not title:
            continue
        # RESERVE, THEN DISPATCH. The card and its item claim are created by ONE
        # statement before any executor exists, so a racing promote — in this
        # process or another one — never reaches dispatch_spec at all. Claiming
        # after dispatch (the previous shape) left the loser holding a live
        # session that outlived the card it cancelled.
        with _promote_item_gate(chat_id, item_key):
            existing = chat_store.find_promoted_task_by_item_key(chat_id, item_key)
            task_id: str | None = None
            if existing is None:
                task_id = chat_store.reserve_promoted_card(
                    chat_id=chat_id,
                    parent_task_id=str(chat["board_task_id"]),
                    item_key=item_key,
                    title=title,
                    description=_promotion_description(description, scope),
                    project_id=project_id,
                    source_message_index=source_msg_idx,
                    working_dir=_ensure_promotion_workfs(scope),
                )
                if task_id is None:
                    # Lost the reservation: nothing was created and nothing was
                    # dispatched. Answer with the winner's card.
                    existing = chat_store.find_promoted_task_by_item_key(chat_id, item_key)
                    _LOG.warning(
                        "promote raced on item %s of chat %s; returning the held card %s",
                        item_key[:12],
                        chat_id,
                        (existing or {}).get("id", "<unknown>"),
                    )
            elif _is_stranded_reservation(existing):
                # A crash between the reservation and its executor left this card
                # holding the item with nothing running. Adopt it (CAS, so only one
                # retry can) and dispatch onto it rather than answering "done".
                if chat_store.adopt_stranded_reservation(str(existing["id"])):
                    task_id = str(existing["id"])
                    _LOG.warning(
                        "promote: re-dispatching stranded reservation %s for item %s",
                        task_id,
                        item_key[:12],
                    )

            if task_id is None:
                if existing is None:
                    continue
                held_id = str(existing["id"])
                promoted.append((held_id, collab_store.get_board_task(held_id) or existing))
                continue

            try:
                result = dispatch_spec(
                    store=store,
                    collab_store=collab_store,
                    policy_cfg=policy_cfg,
                    spec=RefinedSpec(
                        title=title,
                        description=_promotion_description(description, scope),
                    ),
                    project_id=project_id,
                    execute=cast(ExecuteMode, execute_mode),
                    # Attach the executor to the card we already own.
                    board_task_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("promote dispatch failed for %r: %s", title, exc)
                _settle_failed_dispatch(store, collab_store, chat_store, task_id)
                continue
            board_task = result.get("board_task") or {}
            promoted.append((task_id, collab_store.get_board_task(task_id) or board_task))

    try:
        # Only a whole-thread promote closes the chat. Promoting one item out of a
        # live conversation must leave the operator able to keep working in it.
        chat_store.update_chat(
            chat_id,
            status="promoted" if body.item_index is None else None,
            meta={"promoted_project_id": project_id or ""},
        )
    except ChatError:
        pass
    return project_id, promoted


@router.post("/{chat_id}/promote", status_code=201)
def promote_chat(
    chat_id: str,
    body: PromoteRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    """Extract action items from the chat thread into board tasks.

    Uses the budgeted short-call LLM client (``omniagentos.llm.client.ShortCallClient``)
    to analyse the thread, falling back to a per-user-message heuristic. Every item
    is keyed by the content of the message it came from, so re-promoting a thread
    that has grown adds only the new asks, and the chat is marked
    ``status='promoted'`` (with ``promoted_at``) only when the WHOLE thread is
    promoted — passing ``item_index`` leaves the conversation open.

    Pinned contract: body ``{"project_id": str|null, "new_project_title"?: str,
    "execute_mode"?: "readonly"|"tools"|"session", "item_index"?: int}``
    → ``{"project_id": str, "task_ids": [str]}``.
    """
    project_id, promoted = _promote_action_items(
        chat_id=chat_id,
        body=body,
        store=store,
        collab_store=collab_store,
        policy_cfg=policy_cfg,
        single_item_required=False,
    )
    return {"project_id": project_id or "", "task_ids": [task_id for task_id, _ in promoted]}


@router.post("/{chat_id}/promote_item", status_code=201)
def _promote_chat_item(
    chat_id: str,
    body: PromoteRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    """Promote exactly one extracted action item selected by zero-based index.

    Underscore-prefixed on purpose: FastAPI calls this, nothing in the codebase
    does, and the reachability gate refuses a NEW module-level public function
    with no named production caller (house idiom — see ``workfs._tree``). Its
    sibling ``promote_chat`` stays public because it already ships on main with a
    published ``operationId`` that generated clients key on; this route is new, so
    it costs nothing to be private from birth.

    Idempotent on the item's content-derived identity, so re-promoting the same
    item returns the existing card. The chat stays open.

    Pinned contract: body ``{"item_index": int, "project_id"?: str|null,
    "execute_mode"?: "readonly"|"tools"|"session"}``
    → ``{"task_id": str, "task": BoardTask}``.
    """
    _project_id, promoted = _promote_action_items(
        chat_id=chat_id,
        body=body,
        store=store,
        collab_store=collab_store,
        policy_cfg=policy_cfg,
        single_item_required=True,
    )
    if not promoted:
        fail(400, "validation", "selected action item has no title")
    task_id, task = promoted[0]
    return {"task_id": task_id, "task": task}


def _extract_action_items(
    messages: list[dict[str, Any]], chat_title: str
) -> list[dict[str, Any]]:
    """Use the budgeted LLM client to extract action items from a chat thread.

    Every returned item carries ``source_message_index``: the zero-based index of
    the thread message it was derived from. That index is what
    :func:`_resolve_item_identity` turns into a content-derived promotion key, so
    BOTH branches must produce it — an item without a real source is
    indistinguishable from every other item and collapses on dedupe.

    Falls back to a simple heuristic (one task per user message) when the LLM
    client is unavailable or fails — promote must never hard-fail on extraction.
    """
    numbered_thread = "\n".join(
        f"[{index}] {'User' if m['role'] == 'user' else 'Agent'}: {m['content']}"
        for index, m in enumerate(messages)
    )

    try:
        from omniagentos.llm.client import ShortCallClient

        client = ShortCallClient()
        prompt = (
            f"Extract actionable tasks from this chat thread about '{chat_title}'.\n"
            f"Each thread line starts with its zero-based message index in square brackets.\n"
            f"Return a JSON array of objects with 'title', 'description' and "
            f"'source_message_index' — the bracketed index of the message the task came from.\n"
            f"Only include concrete, actionable items — not observations or questions.\n"
            f"If there are no actionable items, return an empty array [].\n\n"
            f"Thread:\n{numbered_thread}"
        )
        raw = client.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1024,
            purpose="chat_promote_extract",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(raw)
        raw_items = parsed if isinstance(parsed, list) else parsed.get("items", parsed.get("tasks", []))
        if isinstance(raw_items, list):
            extracted: list[dict[str, Any]] = []
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                entry: dict[str, Any] = {
                    "title": str(it.get("title", "")),
                    "description": str(it.get("description", "")),
                }
                # Never trust the model's index: validate it against the thread we
                # actually sent before it is allowed to become an identity.
                index = _coerce_message_index(it.get("source_message_index"), len(messages))
                if index is None:
                    _LOG.warning(
                        "promote extraction returned an unusable source_message_index "
                        "%r for %r (thread has %d messages)",
                        it.get("source_message_index"),
                        entry["title"],
                        len(messages),
                    )
                else:
                    entry["source_message_index"] = index
                extracted.append(entry)
            return extracted
    except Exception:  # noqa: BLE001
        _LOG.debug("LLM extraction failed; falling back to heuristic", exc_info=True)

    # Heuristic fallback: one task per user message that looks actionable. The
    # enumerate() index is the real position of the message in the thread — it is
    # the item's provenance, not a display counter.
    items: list[dict[str, Any]] = []
    for index, msg in enumerate(messages):
        if msg["role"] != "user":
            continue
        content = msg["content"].strip()
        if len(content) < 10:
            continue
        # Take the first sentence as title, rest as description
        first_line = content.split("\n")[0].strip()
        title = first_line[:120] if len(first_line) > 120 else first_line
        items.append({"title": title, "description": content, "source_message_index": index})
    return items


__all__ = ["router"]
