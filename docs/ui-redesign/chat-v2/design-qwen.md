Now I have a thorough understanding of the entire codebase. Let me write the complete design document.

---

# Chat Screen v2 + Kanban Chat — Complete Design Document

**Author:** Qwen Designer  
**Date:** 2026-07-27  
**Status:** Draft · Reviewable  

---

## 0. Executive Summary

This document specifies the next-version chat experience for OmniAgentOS. The thesis is simple: **the thread IS the product**. Every other surface — folders, projects, model orchestration, kanban, task detail — exists to support, extend, and contextualise the conversation. The design elevates the thread to ChatGPT/Claude-grade immediacy, wires projects as first-class sidebar citizens with auto-classification, exposes model and orchestration control without cluttering the composer, and creates one unified ChatThread primitive that appears identically on `/chats`, inside `/board` cards, and in `/activity` task pages.

Three core moves:

1. **Chat front and center** — The thread occupies ~70% of the viewport. Chrome recedes. No third pane unless explicitly opened. Sidebar collapses to icons. Header shrinks to a slim bar. Composer floats.
2. **Projects are first-class** — Real projects from `/api/projects/tree` populate the sidebar as folder-like containers. Drag chats into a project. Auto-classify new chats via the existing `orgdims` classifier; user confirms or overrides.
3. **Same chat, everywhere** — One `ChatThread` component with one `ChatComposer` renders on `/chats`, inside `/board` task panes, and on `/activity` pages. The model picker, Plan mode toggle, and SSE streaming are identical.

---

## 1. Information Architecture

### 1.1 `/chats` — The Front Door

```
┌─────────────────────────────────────────────────────────────────┐
│ [Shell: Logo · ⌘K palette · ⚙]                                 │
├────────┬────────────────────────────────────────────────────────┤
│        │ ┌────────────────────────────────────────────────────┐ │
│ Side   │ │ ChatHeader: [title ⌫] [model ▾] [orch ▾] [⋯]     │ │
│ bar    │ ├────────────────────────────────────────────────────┤ │
│        │ │                                                    │ │
│ ├ Pro- │ │                                                    │ │
│ │ jects│ │              ChatThread                            │ │
│ │ ─────│ │         (SSE-streamed, scope-aware)                │ │
│ │ ├ 📁 │ │                                                    │ │
│ │ └ 📁 │ │                                                    │ │
│ ├ Inbox│ │                                                    │ │
│ │ ├ ...│ │                                                    │ │
│ │ └ ...│ ├────────────────────────────────────────────────────┤ │
│ └ +New │ │ ChatComposer: [@skill ▾] [📎] [mode ▾] [Send ▸]  │ │
│        │ └────────────────────────────────────────────────────┘ │
├────────┴────────────────────────────────────────────────────────┤
│                    (Workspace drawer: hidden by default)         │
└─────────────────────────────────────────────────────────────────┘
```

**Sidebar** — two-tier hierarchy: **Projects** (from `/api/projects/tree`) at the top, then **Inbox** (folderless chats). Each project acts as a folder: drag a chat onto it to classify. The existing folder names (`meta_json.folder`) become *sub-folders within a project*. A flat "Inbox" section captures chats not yet classified.

**Main area** — the thread. No permanent right pane. A collapsible drawer reveals Workspace (Activity, Terminal) on demand via a small button in the header.

### 1.2 `/board` — Kanban Chat Panel

When a card expands (the existing `/activity/{id}?kind=board` deep link), the detail page now embeds the same `ChatThread` in a bottom panel below the existing stage stepper and progress indicators. The model picker and Plan mode toggle appear in the card's chat panel header, identical to `/chats`.

### 1.3 `/activity` — Task Detail (Everything Visible)

The `/activity/[taskId]` page gains five new sections (existing content preserved):

| Section | Source | What's Rendered |
|---------|--------|-----------------|
| **Progress** | `BoardWork` (existing `run_progress.steps_done/total`) | Visual progress bar (Stripe-style: accent fill on track, percentage label, `current_step` caption) |
| **Plan** | `planner_brief` (existing on `LiveBoardTask`) | Planner brief / TaskContract rendered as structured markdown, never `<pre>` JSON |
| **Checklist** | `checklist` (existing `BoardChecklist.done/total`) | Checklist progress bar (n/m done) with remaining items listed |
| **Agents** | `session.model`, `session.provider`, `claimed_by` | Agent involvement: name, model, status, cost |
| **Chat** | ChatThread component (this design) | The same ChatThread, with the same model picker and Plan mode toggle |
| **ETA** | Computed from `run.steps` average duration | Estimated time remaining |
| **Files** | `BoardFilesResponse` (existing `fetchBoardFiles`) | Tree view with download links, reuse `BoardFilesDrawer` |
| **Run History** | `TaskAttempt[]` from `LonghaulTaskDetail` | Timeline table: attempt seq, session, model, duration, outcome |
| **Approvals** | `pending_approval` (existing on `LiveBoardTask`) | Approval card with command, risk class, approve/reject actions |
| **Acceptance Criteria** | Extracted from `planner_brief` structured JSON | Bulleted list |

---

## 2. Component Breakdown

### 2.1 New Files

| File | Purpose |
|------|---------|
| `dashboard/src/features/chats/projectSidebar.tsx` | Project-aware sidebar tree: renders `/api/projects/tree` as the top tier, with chats nested; supports drag-to-project. |
| `dashboard/src/features/chats/orchestrationPicker.tsx` | Orchestration pattern selector (Solo / Plan-first / Swarm). Feeds `dispatch_spec(..., swarm_planner=...)`. |
| `dashboard/src/features/chats/planModeToggle.tsx` | Plan mode vs Regular mode toggle badge — used in both `/chats` header and board card chat panel. |
| `dashboard/src/features/chats/autoClassify.ts` | Client-side hook: calls `POST /api/chats/{id}/classify` on new chat creation; returns suggested project ID. |
| `dashboard/src/features/chats/chatPanel.tsx` | Reusable chat panel wrapping ChatThread + ChatComposer, designed to be embedded in `/activity` and `/board` card expansions. Accepts `taskId`, `chatId`, `scope` and renders the identical experience. |
| `dashboard/src/features/board/taskChatPanel.tsx` | Board-card-specific embedding of `chatPanel.tsx` with Plan mode toggle, model picker, and progress context. |
| `dashboard/src/features/activity/progressBar.tsx` | Visual progress bar (stripe-style: accent fill, percentage, current step caption). |
| `dashboard/src/features/activity/taskDetailSections.tsx` | All five new sections composed as a tabbed accordion: Plan, Checklist, Agents, ETA, Run History, Approvals. |
| `omniagentos/api/routes/models.py` | New route: `GET /api/models` (S2 slice). |
| `omniagentos/chats/classifier.py` | Auto-classify new chats into projects using the existing `orgdims` classifier. |
| `omniagentos/api/routes/classify.py` | `POST /api/chats/{id}/classify` — returns `{project_id, confidence, suggestion_label}`. |

### 2.2 Modified Files

| File | Changes |
|------|---------|
| `dashboard/src/app/chats/page.tsx` | Replace `ChatSidebar` with `ProjectSidebar`; add orchestration picker to header; add auto-classify hook on chat creation; add Plan mode state; add workspace drawer as overlay (not side pane). |
| `dashboard/src/features/chats/ChatHeader.tsx` | Add `OrchestrationPicker`, `PlanModeToggle`; remove the standalone spawn/promote buttons (become secondary in `⋯` menu); compact the header line. |
| `dashboard/src/features/chats/ChatComposer.tsx` | Replace model override Select with compact badge trigger; add Plan mode context awareness (in Plan mode: composer shows checklist hints instead of free-form); remove the `/spawn` slash command (orchestration picker handles this). |
| `dashboard/src/features/chats/ChatThread.tsx` | Make `chatId` optional (supports `taskId`-scoped threads for board); accept `embedded` prop for compact rendering in card expansions; add Plan mode awareness (render plan step status inline during streaming). |
| `dashboard/src/features/chats/ChatSidebar.tsx` | Rename to `projectSidebar.tsx`; replace folder logic with project-tree logic; retain Inbox for unclassified chats. |
| `dashboard/src/features/chats/useChats.ts` | Add `useAutoClassify(chatId)` hook; add `useOrchestrationOptions()` hook (reads cascade config for available patterns); add `usePlanMode(chatId)` hook with SSE persistence. |
| `dashboard/src/features/chats/chatApi.ts` | Add `classifyChat(chatId)`, `updatePlanMode(chatId, mode)`, `fetchOrchestrationOptions()`; add `RoutingPreferences` type. |
| `dashboard/src/features/chats/chats.module.css` | Add styles for: project sidebar tree, auto-classify suggestion bar, orchestration picker popover, plan mode toggle, compact embedded thread, progress bar. |
| `dashboard/src/features/chats/WorkspaceTabs.tsx` | Extend with Plan, Checklist, and Files tabs (reusing existing `progressBar.tsx` and `BoardFilesDrawer`). |
| `dashboard/src/app/activity/[taskId]/page.tsx` | Add `taskDetailSections` with the 5 new sections; embed `chatPanel.tsx` at the bottom of the detail page. |
| `dashboard/src/app/board/page.tsx` | No structural changes — cards link to `/activity` which now shows everything. The board stays lean. |
| `dashboard/src/features/board/BoardKanban.tsx` | Add compact checklist progress bar on card face (reuse `BoardProgress` pattern); add "from chat" provenance badge. |
| `omniagentos/api/routes/chats.py` | Add `POST /api/chats/{id}/classify` endpoint; add `routing_preferences` to `UpdateChatRequest`; add `plan_mode` to `CreateChatRequest`. |
| `omniagentos/chats/store.py` | Add `classify_chat()` method; add `plan_mode` to chat meta; add `routing_preferences` to chat meta. |

---

## 3. State Management

### 3.1 State Ownership

```
/chats page
├── useChatList()                    — chat[] list (existing, from chatApi)
├── useProjectTree()                 — project hierarchy (existing, from hierarchyHooks)
├── useChatFolders()                 — folder names within projects (existing)
├── useModelOptions()                — model catalog (existing, from chatApi)
├── useAutoClassify(chatId)          — new: fires on chat creation
├── useOrchestrationOptions()        — new: reads cascade config for patterns
├── usePlanMode(chatId)              — new: persisted per-chat via PATCH
└── selectedChatId (local)           — current selection
```

**Key principle:** No new global state. All state is either a hook (server-derived, SSE-refreshed) or local component state. The existing `useChatThread` hook already handles streaming + message CRUD; we extend it with `planMode` and `routingPreferences`.

### 3.2 Thread Cache Strategy

- **Initial load:** `GET /api/chats/{id}/messages` hydrates the message list.
- **Streaming:** `chat.turn.*` SSE events append to the live messages array in `useChatThread`.
- **Re-entry:** When returning to a chat previously viewed in the session, the hook serves from its React state (no re-fetch unless `updated_at` changed, checked via `GET /api/chats/{id}` lightweight probe).
- **Board/chat cross-pollination:** Board updates (`board.updated` SSE) trigger `refresh()` on the chat list only — messages stay cached until the user switches chats.

### 3.3 Plan Mode Persistence

Plan mode is a per-chat boolean stored at `chats.meta_json.plan_mode`. It persists via `PATCH /api/chats/{id}` with `{meta: {plan_mode: true}}`. The `usePlanMode(chatId)` hook reads from the chat object and writes via `updateChat()`. When Plan mode is on, the composer prompts for acceptance criteria instead of free-form messages, and the thread renders plan steps as structured checkpoint cards.

### 3.4 Routing Preferences

A per-chat JSON blob at `chats.meta_json.routing_preferences`:

```typescript
interface RoutingPreferences {
  preferred_models?: string[];      // ordered preference list
  preferred_orchestration?: "solo" | "plan_first" | "swarm";
  budget_hint?: "low" | "medium" | "high";
  skill_pins?: string[];            // always-attach skills
}
```

Passed to `dispatch_spec()` as the `pins` parameter (already accepted).

---

## 4. Backend Additions

### 4.1 `POST /api/chats/{id}/classify`

Auto-classify a chat into a suggested project using the existing `orgdims` classifier.

**Request:**
```json
{}  // no body required — reads the chat's thread messages
```

**Response:**
```json
{
  "project_id": "prj_abc123",
  "project_name": "API Layer Redesign",
  "confidence": 0.87,
  "suggested_title": "Architecture review for API layer"
}
```

**Implementation** (`omniagentos/chats/classifier.py`, ~60 lines):
- Read `conv_store.read("chat", chat_id)` for thread content.
- Call the existing `omniagentos.orgdims.classify.classify_task()` with the thread text.
- Map the returned `company_slug` / `product_slug` / `initiative_id` to a project via `ProjectStore.find_by_slugs()`.
- Return the match with confidence.

**Wire into create flow:** After `POST /api/chats` succeeds, the frontend calls `POST /api/chats/{id}/classify` asynchronously. If confidence ≥ 0.7, auto-set `project_id` via PATCH. Otherwise show a suggestion chip: "Suggest: API Layer Redesign (87%) — Apply".

### 4.2 `PATCH /api/chats/{id}` — Extended Fields

Add to `UpdateChatRequest`:
```python
class UpdateChatRequest(BaseModel):
    title: str | None = None
    status: str | None = None
    folder: str | None = None
    model: str | None = None
    plan_mode: bool | None = None           # stored in meta_json.plan_mode
    routing_preferences: dict | None = None  # stored in meta_json.routing_preferences
    meta: dict[str, Any] | None = None
```

### 4.3 `POST /api/chats` — Extended Fields

Add to `CreateChatRequest`:
```python
class CreateChatRequest(BaseModel):
    title: str = Field(..., min_length=1)
    project_id: str | None = None
    model: str | None = None
    plan_mode: bool = False                  # new: start in plan mode
    meta: dict[str, Any] | None = None
```

### 4.4 `GET /api/models` (S2 — reference only)

Pinned contract from FINAL-PLAN.md §B:
```json
{
  "models": [
    {"id": "auto", "label": "Auto — router decides", "provider": "router", "tier": null, "available": true, "lineage": null},
    ...
  ]
}
```

### 4.5 `GET /api/orchestration/options`

New lightweight endpoint returning available orchestration patterns:

**Response:**
```json
{
  "patterns": [
    {"id": "solo", "label": "Solo Agent", "description": "One model, one task. Fast, cheap.", "available": true},
    {"id": "plan_first", "label": "Plan First", "description": "Generate a plan, then execute step by step.", "available": true},
    {"id": "swarm", "label": "Swarm Fan-Out", "description": "Decompose into parallel sub-agents.", "available": true, "requires_swarm_config": true}
  ]
}
```

**Implementation** (~40 lines in `routes/chats.py` or a new `routes/orchestration.py`): reads `configs/cascade.yaml` to determine which patterns are configured; checks swarm config availability.

### 4.6 SSE Events — No Changes Required

The existing `chat.turn.started/delta/completed` events on `/api/events` already cover live streaming. No new event types needed.

---

## 5. Component Designs (Detailed)

### 5.1 ProjectSidebar

Replaces `ChatSidebar`. Two-tier tree:

```
┌──────────────────┐
│ CHATS    [+ New] │
├──────────────────┤
│ ▸ API Redesign   │ ← project from /api/projects/tree
│   ├ Architecture │ ← chat (folder: "Architecture")
│   └ Routes       │
│ ▸ Dashboard v2   │
│   ├ Sidebar UX   │
│   └ Kanban Chat  │
│ ▸ Inbox (3)      │ ← chats without project_id
│   ├ Quick Q...   │
│   ├ Cascade...   │
│   └ Memory...    │
├──────────────────┤
│   + New Chat     │
└──────────────────┘
```

- **Drag-to-project:** Dropping a chat onto a project row calls `PATCH /api/chats/{id}` with `project_id`.
- **Auto-classify suggestion:** When `useAutoClassify` returns a suggestion, show a dismissible chip under the chat: "Suggested: API Redesign (87%) · Apply · Dismiss".
- **Search/filter:** A text input at the top filters chats across all projects (client-side filter on title).
- **Collapsed mode:** Shows only project initials icons (1.5rem) + hover tooltip with name.

### 5.2 OrchestrationPicker

A popover triggered from the header's `orch ▾` dropdown:

```
┌─────────────────────────────────┐
│ Orchestration Pattern          │
├─────────────────────────────────┤
│ ● Solo Agent                   │
│   One model, one task. Fast.   │
├─────────────────────────────────┤
│ ○ Plan First                   │
│   Generate plan, then execute. │
├─────────────────────────────────┤
│ ○ Swarm Fan-Out                │
│   Decompose into parallel      │
│   sub-agents.                  │
└─────────────────────────────────┘
```

- Selected pattern is stored in `routing_preferences.preferred_orchestration`.
- When "Swarm" is selected, the composer's send button changes to "Plan & Dispatch" and asks for a goal description rather than a message.
- When "Plan First" is selected, Plan mode auto-enables.

### 5.3 PlanModeToggle

A segmented toggle in the header (next to the model picker):

```
[Regular] [Plan ●]
```

- **Regular mode:** Free-form composer, messages flow as conversations.
- **Plan mode:** Composer switches to structured input:
  - Acceptance criteria (textarea)
  - Constraints / non-goals (optional)
  - Budget hint (Low / Medium / High segmented control)
  - Send button → "Create Plan"

When a plan is active, the thread renders each plan step as a structured card with:
- Step number + title
- Status chip (pending / running / done / skipped)
- Progress bar when running
- Acceptance criteria checklist
- ETA from remaining steps

### 5.4 ChatComposer — v2

Redesigned for calm density:

```
┌────────────────────────────────────────────────────────────┐
│ [📎] [@skill ▾]                                           │
│                                                            │
│ Message…                      ⏎ send · ⇧⏎ newline        │
│                                                            │
│ [model: Grok 4 ▾]  [mode: Regular ▾]  [▸ Send]          │
└────────────────────────────────────────────────────────────┘
```

Key changes from v1:
- **Attachments row moved above textarea** — skills/files appear as pills above the draft, not below.
- **Model picker is a compact badge** — shows current model name, click to change. No full Select dropdown in the toolbar.
- **Mode picker** — Regular / Plan (compact segmented control).
- **Removed:** `/spawn` slash command (orchestration picker handles this in the header).
- **Removed:** Shift+click model override (the header model picker sets per-chat; per-message override is exposed via the `@model` mention syntax: `@model sonnet-4 tell me about...`).
- **Added:** `@model` mention autocomplete (reusing `skillMention.ts` pattern, filtering `modelOptions`).

### 5.5 ChatThread — v2

The same component, scope-adapted:

| Prop | `/chats` | `/board` card | `/activity` page |
|------|---------|---------------|------------------|
| `chatId` | ✓ | ✓ (from chat's `board_task_id` reverse lookup, or the promote link) | ✓ |
| `taskId` | ✗ | ✓ | ✓ |
| `embedded` | `false` | `true` | `false` |
| `compact` | `false` | `true` | `false` |
| `showModelPicker` | ✓ | ✓ | ✓ |
| `showPlanToggle` | ✓ | ✓ | ✓ |

In `embedded=true` mode:
- Max height 24rem with scroll.
- Composer is a single-line input (expand on focus).
- Header is hidden (the card's own header serves as the context).
- Agent bubbles are slightly more compact (reduced padding).

### 5.6 Task Detail — New Sections (`/activity/[taskId]`)

The page reorganizes into an **accordion layout**:

```
┌─────────────────────────────────────────────────────────┐
│ Stage Stepper:  [1 Queued] — [2 Planning] — [3 Running] │
│ Progress:       ████████████░░░░ 67% · ~4m remaining    │
│ Current step:   Implementing the API routes...          │
├─────────────────────────────────────────────────────────┤
│ ▸ Plan (3 steps, 2 complete)                            │
│ ▸ Checklist (8/12 items done, 4 remaining)              │
│ ▸ Agents (2 involved)                                   │
│ ▸ ETA (~4 minutes)                                      │
│ ▸ Files (3 files)                                       │
│ ▸ Run History (2 attempts)                              │
│ ▸ Approvals (0 pending)                                 │
│ ▸ Chat — the conversation ▾                             │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ [ChatHeader: model ▾ · Plan/Regular ▾]              │ │
│ │ ┌───────────────────────────────────────────────┐   │ │
│ │ │ [ChatThread — agent streaming reply]          │   │ │
│ │ └───────────────────────────────────────────────┘   │ │
│ │ [Composer: @skill · 📎 · Send]                      │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

Each accordion section is lazy-loaded on expansion (fetches data only when opened), except Chat and Progress which are always loaded as they're the primary content.

### 5.7 ProgressBar Component

Stripe-inspired, minimal:

```tsx
// dashboard/src/features/activity/progressBar.tsx
interface ProgressBarProps {
  done: number;
  total: number;
  currentStep?: string | null;
  eta?: string | null;
  tone?: "accent" | "ok" | "warn";
}
```

Renders:
- Full-width track (`height: 6px`, `border-radius: 999px`, `background: var(--border)`).
- Fill segment (`background: var(--accent)`, animated width transition `var(--motion-base)`).
- Label: `${done}/${total}` right-aligned, `font-size: var(--text-micro)`, `color: var(--text-muted)`.
- Optional `currentStep` caption below the bar, truncated with ellipsis.
- Optional `eta` right-aligned next to the count.

---

## 6. Keyboard Map

| Shortcut | Scope | Action |
|----------|-------|--------|
| `⌘N` | Global | Create new chat (opens New Chat dialog with title + project picker) |
| `⌘K` | Global | Open command palette |
| `⌘J` | Chat active | Open model picker popover |
| `⌘⇧P` | Chat active | Toggle Plan mode |
| `Enter` | Composer focused | Send message |
| `⇧Enter` | Composer focused | Insert newline |
| `Esc` | Any drawer/dialog | Close current overlay |
| `↑/↓` | Sidebar | Navigate chat list |
| `Enter` | Sidebar item focused | Select chat |
| `⌘⇧F` | Chat active | Focus composer (from anywhere) |
| `⌘.` | Thread | Toggle workspace drawer |
| `@` | Composer | Trigger skill mention autocomplete |
| `@model` | Composer | Trigger model mention autocomplete |

---

## 7. Empty / Loading / Error States

### 7.1 Empty States

| Surface | EmptyState Component |
|---------|---------------------|
| `/chats` — no chats | **title:** "Start a conversation" **message:** "Press ⌘N to create your first chat. Your agents are ready." **action:** `<Button>New Chat</Button>` |
| `/chats` — project with no chats | **title:** "No chats in this project" **message:** "Drag a chat here, or start a new one scoped to this project." |
| Thread — no messages | **title:** "No messages yet" **message:** "Type a message below to start. Use @ to attach skills, @model to pick a model for this message." |
| Plan mode — no plan | **title:** "No plan yet" **message:** "Describe your goal and acceptance criteria, then click Create Plan." |
| Thread — auto-classify returned nothing | (Silent — no badge shown. The chat stays in Inbox.) |
| Task detail — no plan | **title:** "No plan" **message:** "This task was created without a structured plan." |
| Task detail — no checklist | (Section hidden completely when `checklist` is null.) |
| Task detail — no run history | **title:** "No attempts yet" **message:** "Run history appears when the task is executed." |
| `/activity` — no session | **title:** "No active session" **message:** "Terminal will stream here when an agent session starts." |

### 7.2 Loading States

| Surface | Pattern |
|---------|---------|
| Sidebar loading | Skeleton list: 5 rows of `height: 2rem`, `border-radius: var(--radius-md)`, `background: var(--border)`, staggered animation |
| Thread loading | Skeleton messages: 3 alternating user/agent skeleton bubbles (user right-aligned 60% width, agent left-aligned 80% width) |
| Composer (sending) | Send button shows "Sending…" with disabled state; textarea is disabled |
| Thread (streaming) | Agent bubble appears with streaming cursor (`streamCursor` class already implemented), model badge updates live |
| Auto-classify | Inline skeleton chip under the newly created chat: `height: 1.25rem`, `width: 12rem`, pulsing |
| Progress bar | Track renders immediately (100% unfilled), animated fill transition when data arrives |
| Task detail accordion | Each section shows a 2-line skeleton on expansion; lazy-loaded data replaces it |

### 7.3 Error States

| Surface | ErrorState Component |
|---------|---------------------|
| Chat list fails | **title:** "Could not load chats" **message:** (backend error) **action:** `<Button onRetry={refresh}>Retry</Button>` |
| Thread fails | **title:** "Could not load this conversation" **action:** `<Button onRetry={refresh}>Retry</Button>` |
| Send fails | Inline error below composer: `color: var(--danger)`, `font-size: var(--text-small)`. Composer stays populated so the user can retry. |
| SSE disconnect | Reconnecting banner at the top of the thread (reuses the existing `/board` reconnecting pattern). |
| Auto-classify fails | Silent degradation — no suggestion shown, chat stays in Inbox. |
| `POST /api/chats/{id}/classify` 500 | Toast: "Auto-classification unavailable — your chat is in Inbox." |

---

## 8. The 5 Highest-Risk Details

### Risk 1: Plan Mode vs Regular Mode State Divergence

**Problem:** If a user switches to Plan mode mid-conversation, the thread has both regular messages and plan checkpoints. Toggling back to Regular doesn't erase the plan — but the plan steps may have been executed partially. The backend's `dispatch_spec` may have already spawned sessions for plan steps. Switching modes doesn't cancel running work.

**Mitigation:**
- Plan mode is an **additive overlay**, not a replacement. The thread always shows all messages. Plan steps appear as checkpoint cards *above* the message that triggered them.
- Switching from Plan → Regular while steps are running shows a warning toast: "Plan steps are still running. Switching to Regular won't cancel them."
- Switching from Regular → Plan mid-thread extracts the last 3 messages as plan context (LLM-assisted, same budget used in `_extract_action_items`).
- `plan_mode` defaults to `false` on existing chats (migration-free). Only new chats created with `plan_mode=true` or chats where the user explicitly toggles it.

### Risk 2: Auto-Classify Latency on Chat Creation

**Problem:** `POST /api/chats` creates the chat synchronously. The auto-classify call (`POST /api/chats/{id}/classify`) runs the orgdims classifier on the thread — but the thread is *empty* at creation time. The classifier needs message content to work.

**Mitigation:**
- Auto-classify fires on the **first user message**, not on chat creation. When `POST /api/chats/{id}/messages` fires and the chat has no `project_id`, the backend enqueues the classify call asynchronously (via the existing `dispatch_spec(..., async_orchestrate=True)` pattern).
- The frontend shows the suggestion chip after the first agent reply arrives (the thread now has enough context).
- Fallback: if the classifier returns `confidence < 0.5`, no suggestion is shown. The chat stays in Inbox, and the user can manually drag it.
- The classify endpoint accepts an optional `force: true` to re-classify after more messages accumulate.

### Risk 3: The Same ChatThread Everywhere — Scope Identity

**Problem:** The `ChatThread` component currently uses `chatId` as its identity. On `/activity/[taskId]`, the thread is scoped to the task (via `scope_type="task"` in `ConversationStore`). On `/chats`, it's scoped to the chat (`scope_type="chat"`). These are different conversation stores. The promoted-chat flow links a chat to a board task via `board_task_id`, but the task's conversation log is separate from the chat's.

**Mitigation:**
- `chatPanel.tsx` accepts a `scope: "chat" | "task"` prop.
- When `scope="task"`, it fetches messages from `GET /api/chats/{id}/messages` (chat scope) AND from the task's conversation log (existing `GET /api/collab/board/{task_id}/turns`), merging them by `seq` and `created_at`.
- The reply write-back mechanism (dual-write to `scope_type="chat"` from `runner_hook.py`) ensures agent turns appear in both scopes — so the merged view is consistent.
- Deduplication key: `(session_id, turn_index)` — the same mechanism used by the existing write-back code.

### Risk 4: Orchestration Pattern Changes Mid-Thread

**Problem:** A user starts a conversation in Solo mode, exchanges 5 messages, then changes the orchestration pattern to Swarm Fan-Out. The next message dispatches to `dispatch_spec(..., swarm_planner=...)` — which creates a swarm run with root cards and child tasks. The existing thread's companion task is now the parent of a swarm run. The SSE events change from `chat.turn.*` to swarm events.

**Mitigation:**
- Changing orchestration mid-thread is **allowed but scoped to the next message only**. The header shows the current pattern as a badge: `Solo ●`. Clicking it opens the picker but does NOT retroactively change previous messages.
- The per-message model override (via `@model` mention) and the chat-level orchestration pattern are independent: a chat can be in Swarm mode while individual messages route to specific models.
- When Swarm is activated mid-thread, the dispatch creates sub-tasks as **children of the companion task** (existing S1 behavior) — keeping the board clean.
- The thread renders a "Swarm dispatched" system message with links to the new sub-task cards (deep links to `/activity/{sub_task_id}`).

### Risk 5: Board Card Chat Panel — Identity Binding

**Problem:** When a card on the board expands to show the chat panel, the chat needs to know which task it belongs to. Currently, `chat_origin` on `LiveBoardTask` links back to the promoting chat. But many board tasks don't originate from chats (they're created directly). The chat panel on the `/activity` page must handle both: "this card came from a chat" and "this card has no chat — show a standalone thread."

**Mitigation:**
- If `chat_origin.chat_id` exists → render the chat thread (existing conversation).
- If `chat_origin` is null → render a new, empty chat panel with a "Start a conversation about this task" empty state. When the user sends the first message, `POST /api/chats` is called with `board_task_id` linking back to the card, creating a new bidirectional relationship.
- The new `POST /api/chats` endpoint accepts `board_task_id` optional parameter (additive to existing `CreateChatRequest`), stored as the chat's `board_task_id` (no migration needed — the column already exists).
- This means any board card can grow a chat. The card face then shows a small "💬" badge (like the existing "Swarm" badge pattern) indicating "this card has a conversation."

---

## 9. CSS Architecture

### 9.1 New CSS Custom Properties

No frozen token edits. Add to `theme.css`:

```css
/* Chat v2 — additive, not replacing existing vars */
:root {
  /* Plan mode accent (teal) — distinct from --accent (blue) */
  --ds-plan: #3fb9a8;
  --ds-plan-bg: color-mix(in srgb, var(--ds-plan) 8%, transparent);
  --ds-plan-border: color-mix(in srgb, var(--ds-plan) 24%, transparent);

  /* Embedded thread compact sizing */
  --ds-thread-compact-max-height: 24rem;
  --ds-thread-compact-msg-padding: var(--space-2) var(--space-3);

  /* Progress bar */
  --ds-progress-track: var(--border);
  --ds-progress-fill: var(--accent);
  --ds-progress-height: 6px;
}

[data-theme="light"] {
  --ds-plan: #0f7b6a;
  --ds-plan-bg: color-mix(in srgb, var(--ds-plan) 6%, transparent);
  --ds-plan-border: color-mix(in srgb, var(--ds-plan) 20%, transparent);
}
```

### 9.2 Module Convention

All new styles go into `chats.module.css` (existing, additive). Board additions go into `collab.module.css` (existing). Activity additions go into a new `activity/activity.module.css`.

Zero inline styles in feature code. No `style={{}}` outside of the `BoardKanban`'s existing `--column-tone` pattern.

---

## 10. Implementation Priority (Ship Order)

| Priority | Slice | Effort | Depends On |
|----------|-------|--------|------------|
| **P0** | Backend: classify endpoint, extended PATCH/POST, orchestration options | 1 day | Nothing |
| **P1** | ProjectSidebar (replaces ChatSidebar) | 1.5 days | P0 |
| **P2** | OrchestrationPicker + PlanModeToggle + Composer v2 | 2 days | P0, P1 |
| **P3** | ChatThread embedded mode (for /activity and /board) | 1.5 days | P0 |
| **P4** | Task detail new sections (Progress, Checklist, Agents, ETA, Plan, Run History, Approvals) | 2 days | P3 |
| **P5** | Auto-classify UI flow (suggestion chip, drag-to-project) | 1 day | P0, P1 |

**Total: ~9 days serial; ~4–5 calendar days with parallelism.**

---

## 11. Verification Plan

### 11.1 Unit Tests

| File | Tests |
|------|-------|
| `skillMention.test.ts` (extend) | Model mention extraction, `@model` syntax parsing |
| `chatApi.test.ts` (extend) | `classifyChat`, `updatePlanMode`, `fetchOrchestrationOptions` against fixtures |
| `autoClassify.test.ts` (new) | Hook behavior: fires on first message, shows chip ≥0.7 confidence, silent <0.5 |
| `progressBar.test.ts` (new) | Percentage calculation, ETA formatting, tone variants |

### 11.2 Integration Tests

| Scenario | Steps |
|----------|-------|
| New chat auto-classify | Create chat → send message → wait for SSE → verify suggestion chip appears → click Apply → verify chat moves to project |
| Plan mode toggle | Toggle plan mode → verify composer changes → send plan → verify plan step cards render in thread |
| Orchestration switch | Switch to Swarm → send message → verify "Swarm dispatched" system message → verify sub-task links rendered |
| Board card chat panel | Open /activity/{taskId} → verify chat panel loads → send message → verify it appears in thread AND on the board card |
| Chat from board card | Open card with no `chat_origin` → click "Start conversation" → send message → verify bidirectional link created |

### 11.3 E2E Tests (Playwright)

| Test | Assertion |
|------|-----------|
| Chat creation flow | ⌘N → fill title → select project → create → thread visible |
| Auto-classify | Create chat → send message → suggestion chip visible → Apply → chat moves to project sidebar |
| Plan mode | Toggle plan → composer changes → enter criteria → Create Plan → plan steps render |
| Thread on board | Navigate to /board → click card → /activity loads → chat panel visible → send message |
| Model picker | ⌘J → model popover → select → badge updates |

---

## 12. Open Questions (None — Decisions Made)

I've resolved all ambiguities by grounding the design in verified code:

1. **Folders vs Projects:** Projects ARE the folders. The existing `meta_json.folder` becomes project-scoped sub-foldering (backward compatible — old folder-named chats appear under Inbox until classified).
2. **Auto-classify timing:** Fires on first message, not creation (thread is empty at creation).
3. **Plan mode:** Additive overlay, not a replacement for the thread.
4. **Scope identity:** Merged view for chat+task threads with dedup key.
5. **Board card without chat:** Can grow one on demand via the "Start a conversation" empty state.

---

*This document is concrete enough to build from. Every component is specified with exact file paths, every backend addition has a request/response contract, every state has a rendering path. Ship P0–P2 first — that alone transforms `/chats` from a functional chat interface into a governed, project-aware, orchestration-aware control plane.*