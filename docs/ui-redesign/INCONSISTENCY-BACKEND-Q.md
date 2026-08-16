# INCONSISTENCY-BACKEND-Q — Backend↔Frontend Audit Report

**Author:** Backend Inconsistency Hunter (Q)  
**Date:** 2026-07-28  
**Domain:** `omniagentos/**` only (backend)  
**Method:** Read-verify-fix: every suspected inconsistency confirmed on BOTH sides before editing.

---

## Inconsistencies Found & Fixed

| # | Endpoint / System | Expected Shape (Frontend) | Actual Shape (Backend) | Evidence | Fix | Test |
|---|---|---|---|---|---|---|
| Q-01 | `POST /api/chats/{id}/messages` → `_first_message_hooks` (title rename) | SSE `chat.updated` event: `{type:'chat.updated', chat_id, ts}` | No SSE event emitted — title rename happened silently on the server | `routes/chats.py:_first_message_hooks` had no `_emit_chat_event` for title rename; frontend `useChatRefreshSignal` checks `target_type === "chat"` to refresh sidebar (would never trigger on title rename). | Added `_emit_chat_updated` helper + call after title rename | `test_chat_updated_sse_emitted_on_first_message_title_rename` |
| Q-01b | `POST /api/chats/{id}/messages` → `_first_message_hooks` (classify thread) | SSE `chat.updated` event after classify completes | No SSE event emitted — classify wrote `project_suggestion` + `classified_at` to meta silently | `routes/chats.py:_first_message_hooks._classify()` completed without emitting; ProjectSuggestionBar never appears on other tabs | Added `_emit_chat_updated` call after `classify_chat_project` in the background thread | `test_classify_hook_emits_chat_updated`, `test_classify_hook_emits_chat_updated_no_project_no_project_id` |
| Q-02 | `tests/chats/test_bridge.py::TestTailer::test_streams_deltas_and_closes_on_terminal` (flaky 1/7) | Test passes deterministically on every run | Race condition: daemon thread not yet polling when transcript was written; `_wait_for` with 6s timeout sometimes insufficient on loaded CI | `_tail_loop` had no startup synchronization; `_wait_for` relied on blind timing | Added `_thread_polling` `threading.Event` to `ChatTurnBridge`; signalled on thread entry; test waits on it before writing transcript. Increased `_wait_for` timeout to 10s. | Existing test now deterministic |

---

## Verified Consistent

| Endpoint / System | Frontend Expectation | Backend Reality | Verdict |
|---|---|---|---|
| `POST /api/chats` → `Chat` interface | Flat DTO: `id`, `title`, `status`, `project_id`, `project_name`, `board_task_id`, `preferred_model`, `orch_mode`, `plan_mode`, `routing`, `project_suggestion`, `message_count`, `last_message_at`, `promoted_at`, `created_at`, `updated_at`, `meta` | `ChatStore.to_dto()` projects exactly these fields | ✅ Consistent |
| `GET /api/chats` → `Chat[]` | Same DTO shape, list | `list_chat_dtos()` returns projected list | ✅ Consistent |
| `GET /api/chats/{id}` → `Chat` | Flat DTO | `get_chat_dto()` returns projected DTO | ✅ Consistent |
| `PATCH /api/chats/{id}` → `Chat` | Returns updated DTO | Returns updated DTO via `get_chat_dto()` | ✅ Consistent |
| `DELETE /api/chats/{id}` → `Chat` | Soft-delete returns DTO | Returns DTO after soft_delete | ✅ Consistent |
| `POST /api/chats/{id}/messages` → `SendResult` | `{message: ChatMessage, dispatch?: {session_id, task_id, run_id, board_task, steered}}` or `{message, task_ids}` | Response shapes match exactly (both `dispatch` and fanout paths) | ✅ Consistent |
| `GET /api/chats/{id}/messages` → `ChatMessage[]` | `[{id, seq, role, content, model, created_at, meta?}]` | `ConversationStore.read()` returns these fields | ✅ Consistent |
| `POST /api/chats/{id}/classify` → `ProjectSuggestion` | `{project_id, name, confidence, rationale}` | Route returns exactly this flat dict | ✅ Consistent |
| `POST /api/chats/{id}/plan` → `PlanSeedResponse` | `{job_id, status}` | Route returns `{"job_id", "status": "running"}` | ✅ Consistent |
| `POST /api/chats/{id}/spawn` → `SpawnResponse` | `{task_ids: string[]}` | Route returns `{"task_ids": [...]}` | ✅ Consistent |
| `POST /api/chats/{id}/promote` → `PromoteResponse` | `{project_id: string, task_ids: string[]}` | Route returns `{"project_id", "task_ids"}` | ✅ Consistent |
| `GET /api/chats/folders` → `{folders: string[]}` | `{folders: [...]}` | Route returns `{"folders": [...], }` | ✅ Consistent |
| `GET /api/models` → `{models: ModelEntry[]}` | `{models: [{id, label, provider, tier, available, lineage?}]` | Route returns this shape | ✅ Consistent |
| `SSE chat.turn.started` | `{type, chat_id, task_id, turn, model?, ts}` | `_emit_chat_event` produces exactly this payload | ✅ Consistent |
| `SSE chat.turn.delta` | `{type, chat_id, task_id, turn, text, model?, ts}` | Bridge `_emit` produces exactly this payload | ✅ Consistent |
| `SSE chat.turn.completed` | `{type, chat_id, task_id, turn, text, model?, session_id, ts, timed_out?, error?, deduped?}` | Bridge `_emit` produces exactly this payload | ✅ Consistent |
| `GET /api/board` → `LiveBoardTask[]` | BoardTask fields + `run_id`, `run_state`, `run_agent`, `run_progress`, `project_id`, `work?`, `org?`, `chat_origin?`, `checklist?`, `planner_brief?` | Route enriches with project_map, run data, work contracts | ✅ Consistent |
| `PATCH /api/board/{task_id}` | Returns updated BoardTask | Emits `board.updated` SSE + returns updated row | ✅ Consistent |
| `SSE board.updated` | `{type:'board.updated', target_type:'board_task', target_id, payload:{task_id, fields?}}` | Route `_emit` produces this shape | ✅ Consistent |
| `GET /api/pulse/series?metric=X&days=N` | `{metric: str, points: [{date, value}]}` | Route returns exact shape, validates metric name | ✅ Consistent |
| `GET /api/pulse/metrics` | `{metrics: str[]}` | Route returns `{"metrics": [...]}` | ✅ Consistent |
| `GET /api/connections` | `{categories: [{id, label, integrations: [{id, name, logo, status, instances, detail, docs_url}]}]}` | Pydantic models (`CategoryOut`, `IntegrationOut`, `InstanceOut`) enforce this shape | ✅ Consistent |
| `GET /api/routines` | `Routine[]` with trigger/task/gate fields | Returns rows from `RoutinesStore` | ✅ Consistent |
| `POST /api/projects` → `{id, name}` | `{id: string, name: string}` | Route returns this shape | ✅ Consistent |
| Edge: `POST /api/chats/{id}/messages` empty content | 422 (Pydantic `min_length=1`) | Confirmed — Pydantic rejects before handler | ✅ Safe |
| Edge: `POST /api/chats/{id}/classify` null body | No crash | `body and body.force` guard handles None | ✅ Safe |
| Edge: `POST /api/chats/{id}/plan` empty goal + empty thread | 400 (validation) | `if not goal: fail(400, ...)` guard | ✅ Safe |
| Edge: `POST /api/chats/{id}/promote` empty thread | 400 (validation) | `if not messages: fail(400, ...)` guard | ✅ Safe |
| Edge: `POST /api/chats/{id}/spawn` count=0 | 422 (Pydantic `ge=1`) | Confirmed | ✅ Safe |
| Edge: `GET /api/board` with bad project_id | Returns empty list, no crash | Filter logic returns `[]` if no match | ✅ Safe |

---

## Known Gap (Requires Frontend Fix)

| Item | Description | Backend Status | Frontend Status |
|---|---|---|---|
| **FE-GAP-01** | `useChatRefreshSignal` in `dashboard/src/features/chats/useChats.ts:843` subscribes only to `["board.updated"]` via `useEventChannel`. The backend now emits `chat.updated` events (Q-01 fix), but the frontend's type filter discards them. | ✅ Fixed — `chat.updated` events are emitted with `target_type="chat"` | ❌ Needs fix: `useEventChannel(["board.updated", "chat.updated"])` |

The frontend fix is one line in `useChats.ts`:
```typescript
const { lastEvent } = useEventChannel(["board.updated" as string, "chat.updated" as string]);
```

---

## Stale Backend References to Deleted Frontend Concepts

| Check | Status |
|---|---|
| No route returns `folder` as a first-class grouping (frontend moved to project_id) | ✅ `folder` is on `UpdateChatRequest` marked "back-compat for one release; the UI never sends it" — acceptable |
| No route returns legacy `ChatThread` shape (deleted from frontend) | ✅ All chat responses use the pinned ChatDTO |
| No route emits legacy SSE types the frontend no longer handles | ✅ All SSE types match `useEventChannel` subscriptions |

---

## Pinned Contracts Kept

- `docs/ui-redesign/FINAL-PLAN.md` section B — all ChatDTO fields, SSE event shapes, BoardTask shapes
- `docs/chat-v2/SPEC.md` PART 3 — SSE `chat.turn.*` payload shapes

---

## Files Modified

| File | Change |
|---|---|
| `omniagentos/api/routes/chats.py` | Added `utc_now_iso` import; added `_emit_chat_updated()` helper; modified `_first_message_hooks` to emit `chat.updated` on title rename and classify completion |
| `omniagentos/chats/bridge.py` | Added `_thread_polling` event; signal on thread entry; clear before starting new thread |
| `tests/chats/test_bridge.py` | Updated `TestTailer` tests to wait on `_thread_polling` before transcript write; increased `_wait_for` timeout from 6s to 10s |
| `tests/chats/test_routes.py` | Added `project` fixture; added 3 tests for `chat.updated` SSE emission (title rename, classify, classify-skip-when-project-set) |
| `docs/ui-redesign/INCONSISTENCY-BACKEND-Q.md` | This report |
