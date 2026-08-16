# SLICE-REPORT S3 — Chat Frontend

**Agent:** S3 (Chat frontend)
**Date:** 2026-07-27
**Status:** Complete — all files written, tests authored.

---

## What was built

Complete rewrite of `/chats` as an OpenCode-grade, Stripe/Apple-polished chat experience. The v0 3-pane layout (project tree → conversation list → thread + workspace) has been replaced with a **2-pane layout** (sidebar → thread) with a **collapsible right drawer** for workspace details.

### Features delivered

1. **ChatSidebar** — Folder tree with named folders and an Inbox for folderless chats. Supports Cmd+N new chat, HTML5 drag-to-folder, right-click context menu (rename/move/delete), collapse toggle.

2. **ChatHeader** — Editable title (click to rename), prominent model picker fed by `GET /api/models`, Fan out button (POST spawn), Promote to Board button (dialog → POST promote → success Toast), archive/delete, drawer toggle.

3. **ChatComposer** — `@skill` mention autocomplete from `GET /api/skills/tree` (writes attachment manifest into message meta per pinned contract), document picker via `POST /api/board/{task_id}/files/upload`, `/spawn` slash command, per-message model override (Select dropdown), Enter sends / Shift+Enter newline. Attachment chips shown above the textarea.

4. **ChatThread** — Scope-aware single-conversation primitive. Subscribes to `chat.turn.started/delta/completed` via the existing `useEventChannel` (no new EventSource). Live streaming deltas render with a blinking cursor. Tool-call cards extracted from `[tool:name]` markers. Per-turn model badge. Queued→running→done state chips. Agent turns render with accent styling.

5. **Promote to Board** — Dialog picks project ID or creates a new project title. Calls `POST /api/chats/{id}/promote`. Success Toast with message linking to `/board`.

6. **Fan out** — Dialog captures goal description. Calls `POST /api/chats/{id}/spawn`. Success Toast shows task count.

7. **Right drawer (WorkspaceTabs)** — Collapsible right panel showing Activity Log and Live Terminal for the chat's companion task. Not a permanent third pane.

8. **Keyboard-first** — Cmd+N new chat (global + sidebar), Esc closes dialogs, Enter sends, Shift+Enter newline, focus management.

9. **Fixture fallback** — All API calls use `NEXT_PUBLIC_USE_CHATS_FIXTURES=true` convention. Rich fixture data includes 4 chats, messages, models, skills, folders.

10. **useModelOptions rewritten** — The hardcoded 5-entry list in `hierarchyHooks.ts` now fetches from `GET /api/models` per the pinned contract. Signature unchanged — existing callers in `HierarchyViews.tsx` keep working.

---

## Files changed (disjoint ownership)

### New files

| File | Purpose |
|---|---|
| `dashboard/src/features/chats/chatApi.ts` | API client with fixture fallback for all chat endpoints |
| `dashboard/src/features/chats/skillMention.ts` | Pure @skill mention extraction, search, and application |
| `dashboard/src/features/chats/useChats.ts` | React hooks: useModelOptions, useChatList, useChatThread, etc. |
| `dashboard/src/features/chats/ChatSidebar.tsx` | Sidebar: folder tree + Inbox + drag-to-folder + rename/delete |
| `dashboard/src/features/chats/ChatHeader.tsx` | Header: editable title + model picker + actions |
| `dashboard/src/features/chats/skillMention.test.ts` | Vitest unit tests for mention logic |
| `dashboard/src/features/chats/chatApi.test.ts` | Vitest unit tests for fixture API path |

### Rewritten files

| File | Changes |
|---|---|
| `dashboard/src/app/chats/page.tsx` | Full rewrite: 2-pane layout, dialogs, drawer, keyboard shortcuts |
| `dashboard/src/features/chats/ChatComposer.tsx` | Full rewrite: @skill, file upload, model override, slash commands |
| `dashboard/src/features/chats/ChatThread.tsx` | Full rewrite: SSE streaming, state chips, tool calls, agent styling |
| `dashboard/src/features/chats/chats.module.css` | Full rewrite: 2-pane layout, CSS Modules, var(--ds-*) tokens, no inline styles |

### Modified files

| File | Changes |
|---|---|
| `dashboard/src/features/projects/hierarchyHooks.ts` | `useModelOptions()` rewritten from hardcoded list to `GET /api/models` fetch |
| `dashboard/e2e/product.spec.ts` | `/chats` test updated: new UX (create chat dialog, state chips, thread render) |

### Untouched (owned but not modified)

- `dashboard/src/features/chats/ansi.ts` — shared by TerminalView, unchanged
- `dashboard/src/features/chats/SessionFollow.tsx` — used in workspace drawer, unchanged
- `dashboard/src/features/chats/TerminalView.tsx` — used in workspace drawer, unchanged
- `dashboard/src/features/chats/WorkspaceTabs.tsx` — composed in the drawer, unchanged

---

## Contracts consumed (from FINAL-PLAN.md §B)

| Contract | Source slice | Fallback |
|---|---|---|
| `GET /api/models` | S2 | Fixture list (6 models including `auto` default) |
| `GET /api/skills/tree` | Existing | Fixture list (5 skills) |
| `GET /api/chats` | S1 | Fixture list (4 chats) |
| `GET /api/chats/folders` | S1 | Fixture list (3 folders) |
| `POST /api/chats` | S1 | Fixture create |
| `GET /api/chats/{id}` | S1 | Fixture get |
| `PATCH /api/chats/{id}` | S1 | Fixture update |
| `DELETE /api/chats/{id}` | S1 | Fixture delete |
| `GET /api/chats/{id}/messages` | S1 | Fixture messages |
| `POST /api/chats/{id}/messages` | S1 | Fixture send |
| `POST /api/chats/{id}/spawn` | S1 | Fixture spawn (task_ids) |
| `POST /api/chats/{id}/promote` | S1 | Fixture promote (project_id + task_ids) |
| `POST /api/board/{task_id}/files/upload` | Existing | Fixture upload |
| SSE `chat.turn.*` | S1 | Graceful no-op when events don't arrive |

## Contracts emitted

None — S3 is a pure frontend consumer.

---

## Integration notes for Kimi's verification ladder

1. **S1 dependency:** All chat CRUD and streaming depends on S1's `omniagentos/api/routes/chats.py` additions. Until S1 lands, set `NEXT_PUBLIC_USE_CHATS_FIXTURES=true` to run the dashboard in offline mode.

2. **S2 dependency:** `useModelOptions` and ChatHeader model picker depend on `GET /api/models`. Until S2 lands, fixtures provide a 6-model list.

3. **Router registration:** S2 registers `/api/models` in `omniagentos/api/main.py` (Kimi's domain). S3 does not edit that file.

4. **E2E spec:** The `/chats` test in `dashboard/e2e/product.spec.ts` now creates a chat via the dialog, sends a message, and checks for state chips. The old "queued — agents read this on their next run" assertion is removed (replaced by running/queued/done state chips from the streaming integration).

5. **Design system:** All styling uses CSS Modules (`chats.module.css`) referencing `var(--ds-*)` and standard theme tokens. Zero inline styles in feature code. Dark mode safe. Subtle 120-200ms transitions. All primitives from `@/design`.

6. **Bundle discipline:** No new runtime dependencies added. All imports use existing packages from `package.json`.

---

## Acceptance checklist

- [x] Create standalone chat via Cmd+N dialog
- [x] Drag chat to folder
- [x] Pick non-default model (model picker in header)
- [x] @skill attach (autocomplete popover, writes attachment manifest)
- [x] 📎 doc upload (file input → attachment chip → upload on send)
- [x] Send message → state chip (queued/running/done)
- [x] Fan out → spawn dialog → POST spawn
- [x] Promote → dialog → POST promote → success Toast with /board link
- [x] Zero inline styles in feature code
- [x] EmptyState for no-chats / no-messages / no-chat-selected
- [x] ErrorState on fetch failure
- [x] CSS Modules with var(--ds-*) tokens
- [x] Dark-mode-safe (all colors use CSS custom properties)
- [x] Skeleton loading (thread)
- [x] Keyboard-first (Cmd+N, Esc, Enter, Shift+Enter)
- [x] useModelOptions fetches /api/models
- [x] E2E spec updated
- [x] Vitest tests for skillMention (pure functions) and chatApi (fixtures)
- [x] Subtle 120-200ms transitions only
