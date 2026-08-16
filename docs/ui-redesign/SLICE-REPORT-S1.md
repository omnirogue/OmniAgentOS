# S1 — Chat Backend Core: Slice Report

**Agent:** Grok (S1)  
**Status:** COMPLETE  
**Date:** 2026-07-27

---

## Summary

S1 delivers the critical backend unlock for the UI redesign: agent reply write-back to chat scope, folders, model selection, soft delete, spawn/promote orchestration, SSE chat events, and attachment validation. All changes are in owned files only.

## Files Changed

| File | Change |
|---|---|
| `omniagentos/chats/store.py` | Extended: `list_chats` (folder filter, include_deleted), `list_folders`, `soft_delete_chat`, `find_chat_by_board_task_id`, `create_spawn_task` |
| `omniagentos/memory/runner_hook.py` | Extended: `safe_persist_agent_turn` (dual-write via `board_task_id`), new `safe_persist_chat_agent_turn`, new private resolvers |
| `omniagentos/api/routes/chats.py` | Rewritten: 10 endpoints (folders, model, delete, spawn, promote, attachments, SSE emit) |
| `tests/chats/test_reply_writeback.py` | New: 8 tests covering dual-write, run-chain resolution, empty-content guard, multi-turn ordering |
| `tests/chats/test_routes.py` | Extended: 8 new test functions (model, folders, delete, spawn, promote, empty-promote, attachments) |
| `tests/chats/test_store.py` | Extended: 8 new test functions (folders, folder filter, soft delete, find-by-board-task, spawn, deleted-excluded) |

## Deliverables

### 1. Agent Reply Write-Back ✅

`safe_persist_agent_turn` now dual-writes to `scope_type="chat"` when the board task originated from a chat:

- **Explicit path:** caller passes `board_task_id` → lookup `chats.board_task_id` → write to chat scope
- **Runner path:** `board_task_id` omitted → resolve via `runs.task_id → board_tasks.run_id → chats.board_task_id`
- **Session path:** new `safe_persist_chat_agent_turn(store, chat_id=, ...)` for session-mode dispatches

Dedupe: conversations table's `UNIQUE(scope_type, scope_id, seq)` + atomic seq allocation prevents duplicates.

### 2. Folders ✅

- `GET /api/chats/folders` → `{"folders": [string]}` (distinct non-empty values)
- `GET /api/chats?folder=<name>` — filter by folder; `folder=""` selects no-folder chats
- `PATCH /api/chats/{id}` with `{"folder": "..."}` updates `meta_json.folder`
- Deleted chats excluded from folder list

### 3. Model Selection ✅

- `model` on `CreateChatRequest` → stored as `meta.preferred_model`
- `model` on `CreateMessageRequest` → per-message override
- Resolution: message model > chat `preferred_model` > `None` (router default)
- Threaded to `dispatch_spec(..., model=...)` call

### 4. Soft Delete ✅

- `DELETE /api/chats/{id}` → sets `status='deleted'` + `updated_at`
- Excluded from `GET /api/chats` (default) and `GET /api/chats/folders`
- `find_chat_by_board_task_id` excludes deleted chats
- `GET /api/chats/{id}` still returns the deleted chat

### 5. Spawn ✅

- `POST /api/chats/{id}/spawn` — body `{"goal": str, "count": int}` → `{"task_ids": [...]}`
- Each spawned task: `origin='chat'`, `org_json.parent_task_id=companion_task_id`
- Hidden from live board (`/api/board` already filters `origin='chat'`)
- Each dispatched via `dispatch_spec(execute="session", board_task_id=spawned_id)`

### 6. Promote ✅

- `POST /api/chats/{id}/promote` — body `{"project_id": str|null, "new_project_title"?: str}` → `{"project_id": str, "task_ids": [str]}`
- Uses `ShortCallClient` (budgeted LLM) to extract action items from thread
- Heuristic fallback if LLM unavailable (one task per user message)
- Creates project if `new_project_title` provided and no `project_id`
- Each item dispatched via `dispatch_spec(execute="readonly")` as board tasks
- Chat stamped `status='promoted'` + `promoted_at`

### 7. SSE Chat Events ✅

- `chat.turn.started` emitted on `/api/events` bus via `store.insert_event` when message dispatched
- Payload: `{"chat_id", "task_id", "turn", "model", ...}`
- Delta/completed events to be emitted by session/runner completion hook calling `safe_persist_chat_agent_turn` + `_emit_chat_event` (integration point documented for S6/Kimi)

### 8. Attachment Manifest ✅

- Validated on `CreateMessageRequest.meta.attachments`
- Schema: `[{"kind": "skill"|"file"|"url", "ref": string, "label": string}]`
- Invalid kind, missing ref, or missing label → 400
- Cleaned meta threaded to `ConversationStore.append(meta=...)`

## Pinned Contracts Consumed/Emitted

| Contract | S1 Role |
|---|---|
| `GET /api/chats/folders` → `{"folders": [string]}` | **EMITS** |
| `GET /api/chats?folder=<name>` | **EMITS** |
| `POST /api/chats/{id}/spawn` → `{"task_ids": [...]}` | **EMITS** |
| `POST /api/chats/{id}/promote` → `{"project_id", "task_ids"}` | **EMITS** |
| `chat.turn.started/.delta/.completed` SSE events | **EMITS** (started from route; delta/completed from hook integration) |
| `GET /api/models` | **CONSUMES** (model value passed through to `dispatch_spec`) |
| Message attachment manifest schema | **VALIDATES** |

## Integration Notes

1. **Router registration:** `omniagentos/api/routes/chats.py` exposes `router` — already registered in `api/main.py` by Kimi (existing line).

2. **Session completion hook:** When the session bridge finalizes a chat-originated session, it should call `safe_persist_chat_agent_turn(store, chat_id=..., content=..., board_task_id=...)` + emit `chat.turn.completed` via `store.insert_event`. This wire-up is in `omniagentos/sessions/dal.py` (NOT owned by S1) — Kimi to integrate during merge.

3. **Runner hook integration:** The existing `runner/core.py` call to `safe_persist_agent_turn` at line 2746 is backward-compatible — the function's signature gained an optional `board_task_id` kwarg with default `None`. No caller change required.

4. **Spawn task visibility:** Spawned tasks carry `origin='chat'`, so the live board at `omniagentos/api/routes/intake.py:1376` (`task.get("origin") != "chat"`) correctly hides them from the kanban.

5. **No migrations needed:** All new data stored via existing columns (`meta_json` on chats, `org_json` on board_tasks).

## Test Coverage

- `tests/chats/test_reply_writeback.py` (8 tests): dual-write, no-chat guard, no-btk guard, run-chain resolution, empty content, multi-turn ordering, chat-only write, chat+task write
- `tests/chats/test_routes.py` (10 tests total): existing lifecycle + model, folders, soft-delete, spawn, promote, promote-empty, attachments
- `tests/chats/test_store.py` (12 tests total): existing lifecycle + folders, folder filter, soft delete, delete-nonexistent, find-by-board-task, spawn, spawn-nonexistent, deleted-excluded-from-folders
