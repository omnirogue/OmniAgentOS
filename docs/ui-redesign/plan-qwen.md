I have a thorough understanding of the codebase. Here is the complete UI/UX improvement plan.

---

# OmniAgentOS Dashboard — UI/UX Improvement Plan

**Owner's thesis**: "I haven't looked in days... I'm just using the terminal." The dashboard has drifted from being a daily-driver control plane into a graveyard of half-connected surfaces. The goal is to make the browser the place the owner reaches for first — the same muscle memory as OpenCode, with the Kanban the terminal can't give you.

---

## 1. Current-State UX Assessment

### 1.1 What exists (and is solid)

| Surface | Route | Quality | Notes |
|---|---|---|---|
| Command cockpit | `/` | **Strong** | `CommandComposer.tsx` (big "what do you want?" field, speed slider, plan-first toggle, voice) + `TaskPulse.tsx` (live work list with needs-response-first ordering, swarm-aware grouping). This is the cleanest page in the app. |
| Live board | `/board` | **Strong** | `BoardKanban.tsx` — filtering by discipline/category/swarm-run/lane, bulk archive, swarm phase overlay, needs-response queue, files drawer, matrix/portfolio view toggle. Reused by the cockpit. |
| Routines | `/routines` | **Working** | Full CRUD + enable/disable + `/routines/new`. Table with trigger, gate, hard-stop, acceptance rate, cost/accepted-change columns. |
| Improvements | `/improvements` | **Working** | Tabbed view (queue/reflection/panel_blocked/applied/monitoring/rolled_back/history), approve/reject/rollback/pull, autonomy mode switcher. |
| Skills library | `/skills` | **Working** | Tree + graph + search layout with `layout.tsx` sidebar. Detail, edits, proposals. Stats landing page. |
| System | `/system` | **Working** | Map / agents / skills / improvers / activity panels reading JSON configs on disk through `/api/system/*`. |
| Hierarchy | `/projects/overview` | **Working** | `useProjectTree` drives the folder model `/chats` already reads from. |

### 1.2 What's duplicated / rough

**Three chat surfaces, zero of them satisfying:**

1. **`/chats` — "Chats v0"** (`dashboard/src/app/chats/page.tsx`). Already says "v0" in the header. A 3-pane browser: project-folder tree → a list of conversations per folder → `ChatThread` + `WorkspaceTabs` (plan, todolist, files) split-view. The `ChatComposer.tsx` has a per-message model picker. Good bones, but can only open project-scoped or task-scoped conversations — no standalone chats, no folders you own, no new-chat button, no way to attach skills/docs, no sub-agent spawn, no promote-to-kanban.
2. **`/chat` — "Conversation log"** (`dashboard/src/app/chat/page.tsx`). A separate system entirely: channels (`direct`/`group`/`broadcast`) backed by `omniagentos/conversations/` but accessed through `features/collab/hooks`. This is the collab/handoff bus for AGENTS talking to each other. The owner rarely uses it. It has a "Dispatch as task" action via `DispatchDialog`. It does NOT read from the `/api/chats` endpoints.
3. **Embedded project/task threads** — surfaced by the `ChatThread` component inside `/chats`'s right pane. The same component that `/chats` uses, just reached through the folder tree.

**Backend reality check:**

- `/api/chats` (`omniagentos/api/routes/chats.py`) is the RIGHT foundation — `ChatStore` creates chats with an auto-provisioned companion board task (`board_task_id`), `ConversationStore` is scope-keyed (`project | task | chat`), messages support `model` column. **This is the OpenCode backend**, it just has no matching frontend yet.
- The `ChatStore.chats` table has `project_id` (nullable), `board_task_id`, `status`, `promoted_at`, `meta_json` — enough for folders (via meta), promote-to-kanban (via status="promoted"), per-chat config.
- `ConversationStore.append` accepts `model` and `meta` — so per-message model selection and skill attachments via meta are already supported at the storage layer.
- `/api/routines` has full CRUD, list-runs, record-run. Dashboard hits it via `features/routines/api.ts`.
- `/api/improvements` and `/api/reflection` cover the self-improvement log already.
- `/api/system/{map,agents,skills,improvers,agent-activity}` already exposes the growth/health signals the owner wants.

**What's genuinely missing in the backend:**

- Chat folders: no first-class folder entity. Chats link to `project_id`, and standalone chats have no home.
- Skills/documents attachment manifest on a message/chat (storage supports it via `meta_json`, but no schema or indexing).
- Sub-agent spawn from chat: `/api/intake/quick` dispatches a goal — the right primitive, but not wired into a chat turn.
- Per-chat model config: not modelled (the `chats` table has `meta_json`, so storing `preferred_model` there is free).
- Loops/execution history aggregation: `/api/routines/{id}/runs` lists runs per routine, but there's no cross-routine "all loops" aggregation endpoint.

### 1.3 Navigation sprawl

`AppShell.tsx`'s `NAV_SECTIONS` already shows the strain: 7 top-level sections (Portfolio, Home, Skills, Updates, Executions, Company, Settings) with `/chat` and `/chats` both buried inside them (Home → Health → Chats; Settings → Workspace → Chat). The owner has to remember which one is which.

---

## 2. Chat-First Redesign

The owner wants OpenCode: a fast, keyboard-driven, folder-organized, model-switchable chat that has access to everything the system knows. This is a **frontend-led** project — the backend is 90% there.

### 2.1 New route: `/messages` (or replace `/chats`)

**Recommendation**: keep `/chats` as the URL but rewrite the page. Delete `/chat` (the collab-channel log) from primary navigation — keep it accessible via a "System channels" link under a debug menu for the rare times it's needed.

New layout — a 2-pane + modal-right design (not 3-pane, which is what makes Chats v0 feel cramped):

```
┌─────────────────────────────────────────────────────────────┐
│ [Folder tree / all chats] │ [Active chat thread]           │
│                           │                                │
│ 📁 Inbox                  │  ⚙ model: gpt-5 ▾             │
│ 📁 Project X              │  ─────────────────────────     │
│   💬 Planning the API     │  [user bubble]                 │
│   💬 Design feedback      │  [agent bubble with tool use]  │
│ 📁 Grok integration       │  [streaming...]                │
│                           │  ─────────────────────────     │
│ + New chat                │  [+] [📎] [/] [@] ▸ [Send]     │
└─────────────────────────────────────────────────────────────┘
```

Key affordances, all in the composer toolbar:

| Action | Trigger | Backend |
|---|---|---|
| New chat | `Cmd+N` or big "+" button | `POST /api/chats` with `{title, project_id?, meta: {folder}}` |
| Folder filter | typeahead in sidebar | `GET /api/chats?project_id=...` + new folder tag from `meta.folder` |
| Model picker | `model:` prefix or dropdown | Stored in chat `meta.preferred_model`; `ChatComposer` already has `useModelOptions()` |
| Attach skill | `@skill-name` in compose | Read from `GET /api/skills/tree`; inject id into message `meta.skills[]`; `ConversationStore.append` already takes `meta` |
| Attach document | `📎` button, file picker | Reuse `/api/projects/{id}/files` (already token-gated through the proxy) — store ref in `meta.attachments[]` |
| Spawn sub-agent | `/spawn` command or button in thread header | `POST /api/intake/quick {goal, speed}` with the chat's history as context — reuse the exact machinery the cockpit uses |
| Promote to kanban | "Promote" button in thread header | `PATCH /api/chats/{id} {status: "promoted"}` — **`ChatStore.promote_chat` already exists** and the chat already has a `board_task_id` companion card that appears on `/board` |
| Per-message model | Shift+click model picker | `ChatComposer.tsx` already passes `model` through to `useConversation().append(role, content, model)` |

### 2.2 File-level changes (Chat-First)

**Backend (`omniagentos/`):**

| File | Change | Effort |
|---|---|---|
| `omniagentos/chats/store.py` | Add `list_folders() → list[str]` reading distinct `meta_json->>'folder'` values; add `rename_folder(old, new)`. Add `preferred_model` field helper on `update_chat`. | 0.5d |
| `omniagentos/api/routes/chats.py` | Add `GET /api/chats/folders` (or expose via existing `list_chats` + aggregation). Add optional `folder` query on `GET /api/chats`. Add `POST /api/chats/{id}/spawn` that delegates to `dispatch_spec` with chat history as context (clone what `create_message` already does but decouple from a new user turn — spawn a sub-agent on existing context). Add `GET /api/chats/{id}/attachments` listing message meta.attachments. | 1d |
| `omniagentos/conversations/store.py` | No code change — already supports `meta` on append and read. Document the attachment schema: `meta.attachments: [{kind: 'skill'|'file'|'url', ref, label}]`. | 0d |
| `omniagentos/api/routes/intake.py` | No code change required — `/api/intake/quick` is already the spawn primitive. Just document and type the contract for the dashboard. | 0d |

**Frontend (`dashboard/src/`):**

| File | Change | Effort |
|---|---|---|
| `dashboard/src/app/chats/page.tsx` | Full rewrite. 2-pane layout (sidebar thread list + main thread). Replace 3-pane layout. Reuse `ChatThread`, `ChatComposer` (move them from project-scoped to chat-scoped). | 2d |
| `dashboard/src/features/chats/ChatThread.tsx` | Make scope-aware: accept `scope: 'chat' | 'project' | 'task'` and call `/api/chats/{id}/messages` for chat scope vs the existing hierarchy hook for project/task scope. | 0.5d |
| `dashboard/src/features/chats/ChatComposer.tsx` | Add `@` mention autocomplete (skills + agents), `📎` attachment picker, `/spawn` command handler. Promote model picker from dropdown to a prominent header control. | 1.5d |
| `dashboard/src/features/chats/ChatSidebar.tsx` | **New**. Folder tree + flat "Recent" view + "All chats". Create/rename/delete folder. Drag-chat-to-folder (PATCH chat `meta.folder`). | 1d |
| `dashboard/src/features/chats/ChatHeader.tsx` | **New**. Thread header with title edit, promote-to-kanban button, spawn-sub-agent button, model picker, pin/archive. | 0.5d |
| `dashboard/src/features/chats/skillMention.ts` | **New**. Hook that queries `GET /api/skills/search?q=...` for `@`-mention autocomplete; inserts attachment into composer state. | 0.5d |
| `dashboard/src/app/chat/page.tsx` | Demote. Move link out of primary nav into the Settings → Workspace list as "System channels (debug)". Keep the page but reduce its visual weight. | 0.25d |
| `dashboard/src/design/AppShell.tsx` | Update `NAV_SECTIONS`: Home → Health → Chats becomes the primary chat entry (or promote Chats to a top-level section alongside Portfolio). Remove duplicate Chat link from Settings. | 0.25d |

---

## 3. Keep + Upgrade the Kanban (`/board`)

The board works. The owner's complaint is about **project-level visibility** — wanting plan + todolist + done/undone in one view. That's actually what the cockpit's `TaskPulse` subtask-disclosure does for swarm runs, but scoped to a project.

### 3.1 The upgrade: Project-scoped board view

Add a `?project=<id>` filter to `/board` (the route already supports `?run=`) that scopes the kanban to one project's chats and tasks. This lets a user open "my Grok integration project" and see every chat's companion board task plus its spawned sub-tasks — i.e. the plan (promoted chats), the todolist (companion tasks of active chats), what's done, what isn't.

Additionally, add a **per-card todolist disclosure** (like `TaskPulse`'s `SubtaskList`) on the kanban cards, showing the sub-tasks of a swarm run — the `BoardKanban` component already has the data via `phaseOverlay` / `runGroups`; it just needs to render the disclosure.

### 3.2 File-level changes (Kanban)

| File | Change | Effort |
|---|---|---|
| `dashboard/src/app/board/page.tsx` | Add project filter to the toolbar (Select populated from `useProjectTree`). Pass `?project=` to `useLiveBoard`/the collab client. | 0.5d |
| `dashboard/src/features/collab/hooks.ts` | `useLiveBoard` accepts an optional `projectId` and adds it to the `liveBoard` call. | 0.25d |
| `dashboard/src/features/collab/client.ts` | `collabApi.liveBoard({ archived?, projectId? })` — the backend `GET /api/board` already supports filtering; thread it through if not present, else add a project-id param to the route. | 0.25d |
| `dashboard/src/features/board/BoardKanban.tsx` | Add a `SubtaskList` disclosure per swarm-grouped card, reusing the component already in `TaskPulse.tsx`. | 0.5d |
| `dashboard/src/features/board/filters.ts` | Add `projectId: string` to `BoardFilters` and the corresponding predicate. | 0.25d |
| `omniagentos/api/routes/collab.py` | (If missing) accept `project_id` query on `GET /api/board` and filter cards whose `project_id` or whose linked chat's `project_id` matches. | 0.5d |

---

## 4. Loops Section (Routines as Loops)

`/routines` already exists and is feature-complete. The owner's phrasing "I want to see all my loops in this UI — in a separate section, I believe there's a routine section" confirms the surface is right but under-promoted and invisible day-to-day.

### 4.1 The upgrade: make the routine page the loop observatory

Two additions:

1. **Promote Routines to a top-level nav section** (currently buried under Settings → Workspace). Rename to **"Loops"** in the nav label. The page itself keeps the `routines` URL for link stability.
2. **Add a "Recent runs" panel beneath the table** showing the most recent N runs across ALL routines — giving the owner a timeline of what the system has been doing on its own. The data lives in the `routine_runs` SQLite table and is already queryable through each routine's `/runs` sub-route; we need one aggregate endpoint.

### 4.2 File-level changes (Loops)

| File | Change | Effort |
|---|---|---|
| `dashboard/src/design/AppShell.tsx` | Add a top-level "Loops" nav section between Executions and Company, linking to `/routines`. Move `/routines` out of Settings. | 0.25d |
| `dashboard/src/app/routines/page.tsx` | Add a "Recent runs across all loops" panel below the existing table. | 0.5d |
| `dashboard/src/features/routines/api.ts` | New `routinesApi.recentRuns(limit=50)` calling a new aggregate endpoint. | 0.25d |
| `dashboard/src/features/routines/RecentRunsPanel.tsx` | **New**. Compact table: routine name, run id, gate_passed, accepted, cost, finished_at. | 0.75d |
| `omniagentos/scheduler/store.py` | Add `RoutinesStore.list_recent_runs(limit)` — joins `routines` and `routine_runs`. | 0.25d |
| `omniagentos/api/routes/routines.py` | Add `GET /api/routines/runs?limit=50` aggregate endpoint. | 0.25d |
| Home cockpit (`/`) | Add a "Loops" strip to `TaskPulse` (or a new `LoopsPulse` sibling) — one line per active routine with last-run status and next-fire estimate. This is the "see it at a glance" signal. | 0.75d |

---

## 5. Skills, Data, and Self-Improvement Visibility

### 5.1 What already exists but is scattered

- `/skills` — skills library (tree, graph, versions, proposals)
- `/improvements` — queue + history of self-improvements with approval flow
- `/system` — map, agents, skills, improvers, activity panels
- `/reliability` — reliability metrics
- `/activity` — activity log
- `/briefing` — daily briefing

### 5.2 The upgrade: a "System pulse" on the home cockpit

The cockpit's `TaskPulse` shows live work. Add siblings:

1. **`LoopsPulse`** — active routines, last run status, next fire. (See §4.2.)
2. **`GrowthPulse`** — skills added/updated this week, improvements applied/rolled-back, model usage, token spend. All data already exists:
   - Skills count + recent changes: `GET /api/skills/tree` (count) + `GET /api/updates` (recent proposals) — both already used by `/skills` page.
   - Improvements applied/rolled-back: `GET /api/improvements` with status filter — already used by `/improvements`.
   - Agent activity: `GET /api/system/agent-activity` — already consumed by `/system`'s `ActivityPanel`.
3. **A "What's changed since you were gone" strip** at the top of `/` — a delta view since the user's last visit. Backend aggregates: `skills_updated_since`, `improvements_decided_since`, `routines_run_since`, `tasks_completed_since`. Each returns a count + a "view all →" deep link.

### 5.3 File-level changes (Visibility)

| File | Change | Effort |
|---|---|---|
| `dashboard/src/app/page.tsx` (cockpit) | Add `<GrowthPulse />` and `<LoopsPulse />` below `<TaskPulse />`. Add a "Since you were gone" strip at the top using a `last_seen` timestamp from localStorage. | 0.5d |
| `dashboard/src/features/cockpit/GrowthPulse.tsx` | **New**. Compact: skills Δ, improvements applied/reverted, routine runs, model cost. Read from existing endpoints. | 1d |
| `dashboard/src/features/cockpit/LoopsPulse.tsx` | **New**. 3-5 active routines × last-run status, clickable to `/routines`. | 0.5d |
| `dashboard/src/features/cockpit/SinceYouWereGone.tsx` | **New**. Reads `last_seen` from localStorage on mount; calls a new `/api/system/delta?since=...` endpoint; writes new `last_seen` on unmount. | 0.75d |
| `omniagentos/api/routes/system.py` | Add `GET /api/system/delta?since=ISO` — aggregates counts from skills, improvements, routine_runs, board_tasks (status='done') tables since that timestamp. ~50 lines. | 0.5d |
| `dashboard/src/features/improvements/` (existing page) | Minor: add a "recently applied" graph/timeline (using existing `/api/improvements?status=applied` data, rendered as a sparkline of applied-per-day). | 0.5d |

---

## 6. Phased Implementation Plan

### Phase 1 — Chat Foundation (Week 1-2, ~4d)

**Goal:** `/chats` becomes an OpenCode-tier chat — standalone chats, folders, model picking, promote-to-kanban. No `/chat` duplication.

**Backend first** (1.5d):
1. Extend `ChatStore` with `list_folders`, folder query on `list_chats`, `preferred_model` meta helper.
2. Add `/api/chats/folders`, folder query param, `POST /api/chats/{id}/spawn`, `GET /api/chats/{id}/attachments`.

**Frontend** (2.5d):
3. Rewrite `dashboard/src/app/chats/page.tsx` as 2-pane layout.
4. New `ChatSidebar.tsx` (folders + recent), `ChatHeader.tsx` (promote / spawn / model).
5. Update `ChatThread.tsx` to handle `scope: 'chat'`.
6. Update `ChatComposer.tsx` with model prominence, `@` mentions, `📎` picker.

**Verification:**
- Start backend `make api`, frontend `make dash`.
- Create a chat, attach a skill, pick a non-default model, send a message, see companion board task appear on `/board`.
- Promote → confirm the companion task's status flips on `/board`.
- Spawn sub-agent → confirm a new card appears on the board.

### Phase 2 — Kanban Project Scope (Week 3, ~1.75d)

**Goal:** `/board?project=<id>` works. Swarm cards disclose sub-tasks.

1. Add `project_id` filter to backend `GET /api/board` (0.5d).
2. Wire frontend project filter on board page (0.5d).
3. Add sub-task disclosure in `BoardKanban.tsx` (0.5d).
4. Update `filters.ts` + `useLiveBoard` (0.25d).

**Verification:**
- Open `/board?project=<id>` — only that project's cards appear.
- Swarm cards disclose sub-tasks on click.

### Phase 3 — Loops Prominence (Week 3-4, ~1.75d)

**Goal:** "Loops" is a top-level nav, dashboard shows loop activity at a glance.

1. Move `/routines` to top-level nav, label "Loops" (0.25d).
2. Backend `GET /api/routines/runs` aggregate (0.5d).
3. `RecentRunsPanel` on the routines page (1d).

**Verification:**
- Loops nav is prominent.
- Routines page shows recent runs across all routines.
- Clicking a run shows its routine and gate decision.

### Phase 4 — System Pulse & Growth Metrics (Week 4-5, ~3.25d)

**Goal:** The cockpit tells you what the system did while you were gone.

1. Backend `GET /api/system/delta?since=` (0.5d).
2. `GrowthPulse.tsx` on cockpit (1d).
3. `LoopsPulse.tsx` on cockpit (0.5d).
4. `SinceYouWereGone.tsx` strip (0.75d).
5. Sparkline on improvements page (0.5d).

**Verification:**
- Open `/` after a few days away — the "since you were gone" strip shows non-zero counts, each clicking through to the relevant list.
- `GrowthPulse` shows real skill/improvement numbers matching `/skills` and `/improvements`.

### Phase 5 — Navigation Cleanup & Collab-Chat Reconciliation (Week 5, ~0.5d)

**Goal:** No more confusion about "which chat."

1. In `AppShell.tsx`: Home → Health → Chats becomes the primary chat (or promote to top-level). Remove `/chat` from primary nav; keep page but label "System channels (debug)."
2. Update breadcrumb map + any deep links.

### Dependencies and sequencing

```
Phase 1 (Chat Foundation)
   │   ├─ ChatStore backend changes (blocks frontend)
   │   └─ Frontend rewrite (can start in parallel on stub data)
   ▼
Phase 2 (Kanban scope) ─────── independent of Phase 1, can overlap
   │
   ▼
Phase 3 (Loops) ────────────── independent, can run in parallel with Phase 2
   │
   ▼
Phase 4 (Growth pulse) ─────── depends on Phase 3's aggregate endpoint
   │
   ▼
Phase 5 (Nav cleanup) ──────── trivial, any time after Phase 1 lands
```

### Total effort: ~11 dev-days

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Rewriting `/chats` breaks the project-folder workflow users already use | The project-tree sidebar stays; we add standalone chats alongside, not replace. Chat-by-project remains the default view. |
| Chat scope proliferation (every spawn creates a card on the board) | Spawned sub-agents are children of the chat's companion task, not new board tasks. Keep the board clean by nesting. |
| `/chat` (collab channels) demotion breaks existing agent→agent handoffs | The page stays at its URL; only the nav prominence changes. No backend impact. |
| Attachment picker uploads arbitrary files to the API host | Reuse the existing project-files path (`/api/projects/{id}/files`) which is already token-gated and OS-sandboxed. Don't invent a new uploads surface. |
| `GET /api/system/delta` becomes expensive with years of data | Cap at 30 days server-side; for longer ranges, direct user to the full `/improvements`, `/skills`, `/routines` pages with time filters. |
| Model pickers drift from `useModelOptions`'s source | Keep the single source of truth (`useModelOptions` hook already exists in `features/projects/hierarchyHooks.ts`); both the chat header picker and per-message picker read from it. |

---

## What I'd ship first (a single PR)

If only one thing ships this week, it's the **promote-to-kanban path in `/chats`**:

- `ChatStore.promote_chat()` already exists and is untested end-to-end.
- A "Promote" button in the chat header calls `PATCH /api/chats/{id} {status: "promoted"}`.
- The already-existing companion board task becomes visible on `/board` with a "chat" origin badge.

That single flow — type in a chat → one click → card on the kanban — is what made the owner say "I should have a choice of a simple conversation… and the ability to promote to kanban." The backend has already laid the pipe; the frontend just needs the button.

Then wire the standalone-chat creation (a `POST /api/chats` with no `project_id`) so the user isn't forced into a project folder to start a conversation. Those two moves alone turn `/chats` from "v0" into the OpenCode-tier experience the owner is asking for — with the kanban, loops, and growth-pulse phases layering on top of a now-stable foundation.