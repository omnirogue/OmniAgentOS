I explored the codebase against the brief. Here is the design document.

---

# Chat v2 + Kanban Chat — Design Document

**Author:** Opus designer · **Target:** OmniAgentOS dashboard (`dashboard/`, Next 15, :3002) + FastAPI (`omniagentos/`, :8485)
**Binding:** `FINAL-PLAN.md` §A (Design Charter) and §B (pinned contracts).

---

## 0. Ground truth — what I verified in code before designing

The chat surface described in the brief as "just built" is **wired but not connected**. Seven defects are live today; every one of them is a contract mismatch, not a missing feature. This design is shaped around fixing them, because no amount of layout work makes a chat that never answers feel good.

| # | Defect | Evidence |
|---|---|---|
| **D1** | **The agent never replies.** `safe_persist_agent_turn` is called from exactly one place — `omniagentos/runner/core.py:2744`, gated on `RunState.COMPLETED` — and is called **without** `board_task_id`. Chats dispatch with `execute="session"` (`routes/chats.py:326`), which spawns a live session and creates **no run**, so the runner path never fires and the fallback `_resolve_board_task_id_from_run` (`memory/runner_hook.py:102`) finds nothing. `supervisor._finish` (`sessions/supervisor.py:2526`) harvests metacog memories on COMPLETED but writes no conversation turn. | `grep safe_persist_agent_turn` → 1 call site |
| **D2** | **`chat.turn.delta` / `chat.turn.completed` are never emitted.** Only `chat.turn.started` exists (`routes/chats.py:311`). `useChatThread` appends the agent message *only* on `completed`, so `turnState` sticks at `queued` forever. | `grep -rn "chat.turn.delta" omniagentos/` → docstring only |
| **D3** | **Every chat lands in Inbox.** `ChatStore._chat_dict` returns `{..., meta: {...}}`; the route returns it raw; the TS `Chat` type expects flat `folder` / `preferred_model`. `chat.folder` is always `undefined`. | `chats/store.py:44` vs `chatApi.ts:32` |
| **D4** | **The user's own message renders blank.** `POST /api/chats/{id}/messages` returns `{"message": …, "dispatch": …}` (`routes/chats.py:334`); `chatApi.sendMessage` types the whole envelope as `ChatMessage`, so `msg.id`/`msg.content` are `undefined`. | `chatApi.ts:523` |
| **D5** | **Per-chat model change silently no-ops.** The client PATCHes `{model}`; `UpdateChatRequest` (`routes/chats.py:125`) has only `title/status/folder/meta`. Pydantic drops it. |  |
| **D6** | **Attachment pills never render.** `ConversationStore._message` returns `meta` (parsed dict); `ChatThread.parseAttachments` reads `message.meta_json` (string). | `conversations/store.py:46` |
| **D7** | **`/board?project=` shows an empty board.** `board_tasks` has **no `project_id` column** (verified across every `ALTER TABLE board_tasks` migration), `GET /api/board` (`intake.py:1364`) accepts only `archived`, and `filterBoardTasks` (`filters.ts:60`) compares `task.project_id !== filters.projectId` — always true. Selecting any project blanks the board. |  |

Two structural problems compound these:

- **Two competing taxonomies.** Chats group by `meta.folder` (free-text) *and* by `project_id` (FK). The sidebar reads one, the board reads the other, and neither agrees. This is the root cause of D3 and half of D7.
- **`/api/board` excludes `origin='chat'`** (`intake.py:1375`) — correct for companion tasks, but it also hides every spawned sub-task, so "Fan out" produces work that is invisible everywhere.

---

## 1. North star and the five opinionated calls

> **The thread is the product. Projects are the only grouping. Everything else is a drawer.**

**Call 1 — Delete folders. Projects are the sole taxonomy.**
The owner asked for projects to be first-class and for chats to be dragged into them. Keeping `meta.folder` alongside guarantees permanent drift. `meta.folder` migrates once into project membership; `GET /api/chats/folders` returns `{"folders": []}` for one release, then is removed. This resolves D3 by deletion rather than by patching.

**Call 2 — One centered column, not a pane.**
Thread column caps at `46rem` and centers in the main area. The sidebar is `15rem` (down from `18rem`). The workspace drawer becomes an **overlay** (`position: absolute; inset-block: 0; right: 0`), never a flex sibling — today it squeezes the thread when opened, which is exactly the "cramped" failure the plan set out to fix.

**Call 3 — One composer control for model *and* orchestration.**
Requirements 2 and 4 are the same control. A `RunMode` segmented control — **Chat · Plan · Swarm** — sits in the composer next to the model picker. It replaces the "Fan out" header button, the `/spawn` slash command, and the future "plan toggle" with a single primitive that renders identically on `/chats` and on the board. `Plan` maps onto machinery that already exists: `POST /api/intake/plan` → poll `GET /api/intake/plan/{job_id}` → `POST /api/intake/plan/{job_id}/confirm`.

**Call 4 — "Tell it what models to use" is a routing *policy*, not a model id.**
Per-chat `routing = {mode, model, allow[], deny[], effort, speed}` persists on the chat and threads into `dispatch_spec(pins=…)`. The picker's primary control picks a model; a "Routing…" row in its popover expresses preferences ("prefer Anthropic lineage", "never OpenAI", "speed: ultra").

**Call 5 — One `<ChatSurface>` component, mounted in three places.**
`/chats`, the board task drawer, and (later) project pages render the *same* component with a `variant` prop. The charter's "one conversation primitive" is only real if it is one component.

---

## 2. Information architecture

### 2.1 `/chats`

```
┌───────────────┬──────────────────────────────────────────────┬─────────┐
│  ChatSidebar  │              ChatSurface                     │ Drawer  │
│    15rem      │                                              │(overlay)│
│               │  ChatHeader  ── title · project chip · ⋯     │  22rem  │
│ ⌕ Search      │ ┌──────────────────────────────────────────┐ │         │
│               │ │      centered thread column, 46rem       │ │ Plan    │
│ ▸ Platform  4 │ │                                          │ │ Files   │
│   ▸ API Layer │ │  [suggestion bar — "Looks like Platform"]│ │ Activity│
│ ▸ Growth    2 │ │  ┌ user ─────────────────────────────┐   │ │ Terminal│
│               │ │  └ agent ──────────────── grok-4.5 ──┘   │ │         │
│ Recents       │ │  ┌ PlanCard  [Approve & run]         ┐   │ │         │
│   Quick q…  2m│ │  └ SpawnCard 3 agents → board        ┘   │ │         │
│   Cascade…  1h│ │                                          │ │         │
│               │ ├──────────────────────────────────────────┤ │         │
│  + New chat   │ │ ChatComposer                             │ │         │
│               │ │ [Chat|Plan|Swarm]  [Grok 4.5 ▾]   [Send] │ │         │
└───────────────┴─┴──────────────────────────────────────────┴─┴─────────┘
```

**Sidebar structure** (top → bottom): search input · project groups (from `/api/projects/tree`, collapsible, one level of nesting, drop targets) · **Recents** (project-less chats, time-bucketed: Today / Yesterday / Previous 7 days / Older) · Archived (collapsed, count only) · `+ New chat`.

Chats are dragged onto a project group header → `PATCH /api/chats/{id} {project_id}`. Dropping on **Recents** unassigns (`project_id: null`).

**Header** carries only what cannot live elsewhere: inline-editable title, project chip (click → move menu), status badge, `⋯` overflow (Rename · Move to project · Promote to Board · Archive · Delete · Copy transcript), and the drawer toggle. **Promote** is promoted out of `⋯` into a visible secondary button *only when* the chat has ≥1 agent turn and `status !== "promoted"` — contextual, so chrome recedes on a fresh chat.

**Right drawer** (overlay, `Esc` closes): tabs Plan · Files · Activity · Terminal. Bound to the chat's companion `board_task_id`.

### 2.2 `/board` — the chat panel and the task detail

Selecting a card opens a **right-hand overlay drawer** at `/board?task=<id>` with six tabs. This replaces navigating away to `/activity/[taskId]`; that page remains as the deep-linkable full-page rendering of the *same* components.

| Tab | Content | Source (all existing unless noted) |
|---|---|---|
| **Overview** | progress ring (`work.steps_done/steps_total`), stage stepper, ETA, agents-involved list with model + state, acceptance criteria, blockers, remaining line | `/api/board` · `GET /api/board/{id}/sessions` · `GET /api/board/{id}/longhaul` (`acceptance`) |
| **Plan** | planner brief / TaskContract as components, checklist with per-item status, workbook | `longhaul.workbook_content` · **new** `planner_brief` on `/api/board` |
| **Chat** | `<ChatSurface variant="panel">` — same model picker, same RunMode control | `/api/chats?board_task_id=` · steering turns from `GET /api/board/{id}/conversation` |
| **Files** | existing `BoardFilesDrawer` content, inlined | `GET /api/board/{id}/files` |
| **Runs** | attempt timeline (seq, harness, model, account, duration, end reason) | `longhaul.attempts` |
| **Approvals** | pending approval with the real command, Approve / Reject inline | `pending_approval` on the card · `POST /api/approvals/{id}/decision` |

**ETA** — I am specifying this rather than leaving it to the implementer, because "ETA" invites invention. `eta_at` is computed **client-side** in `features/board/eta.ts`:
`remaining_steps × median(step duration of the last 5 completed steps of this run)`, floored at 30s. If `steps_total` is 0/null or fewer than 2 steps have completed, render `—` and the label "Estimating…". Never render a fabricated ETA.

### 2.3 Nav

No change. `NAV_SECTIONS` in `AppShell.tsx` already reads Chat / Board / Loops / Connections / Observatory / Approvals. Chat stays first.

---

## 3. Component breakdown

### 3.1 Frontend — `dashboard/src/features/chats/`

| File | Status | Responsibility |
|---|---|---|
| `ChatSurface.tsx` | **new** | The one primitive. Props: `{chatId, variant: "page" \| "panel", boardTaskId?}`. Owns header + thread + composer composition. `variant="panel"` drops the header to a single title line and caps the column at `38rem`. |
| `ChatSidebar.tsx` | **rewrite** | Project groups from `useProjectTree()`, Recents time-buckets, search, drag-to-project, keyboard list nav (`↑/↓/Enter`). Folder code deleted. Replace `prompt()`/`confirm()` (currently at lines 306/327) with `Dialog` + `Toast` — native dialogs are a charter violation and block the e2e suite. |
| `ChatHeader.tsx` | **modify** | Strip model picker (moves to composer), strip Fan out (becomes RunMode). Add project chip, `⋯` menu, contextual Promote. |
| `ChatComposer.tsx` | **modify** | Add `RunModeControl` + `ModelPicker`. Remove the dead `handleModelShiftClick` (lines 252–260 — a no-op today). Remove the `/spawn` branch (lines 190–205) which currently does nothing different from a normal send. |
| `RunModeControl.tsx` | **new** | Segmented `Chat · Plan · Swarm`. Swarm reveals a count stepper (1–10, matching `SpawnRequest.count`'s `le=10`). |
| `ModelPicker.tsx` | **new** | Popover: grouped by lineage, unavailable entries disabled with a reason tooltip, footer toggle **"Just this message"** (default off → persists to chat). Replaces the bare `Select`; `⌘J` opens it. |
| `ChatThread.tsx` | **modify** | Read `message.meta` not `meta_json` (**D6**). Add `PlanCard`, `SpawnCard`, `ProjectSuggestionBar`. Anchored auto-scroll: only scroll if already within 64px of the bottom, so streaming can't yank a user who scrolled up. |
| `PlanCard.tsx` | **new** | Renders a `ready` plan job: title, sub-projects, task list, route target, `speed` dial, project override Select, **Approve & run** / **Discard**. |
| `SpawnCard.tsx` | **new** | Renders a fan-out result: N child tasks with live state chips, deep-linking to `/board?task=`. |
| `ProjectSuggestionBar.tsx` | **new** | "Looks like **Platform**." → `Move` / `Choose…` / `Not now`. Renders only when `project_suggestion.confidence ≥ 0.5` and `project_id === null`. |
| `chatApi.ts` | **modify** | Fix `sendMessage` to return `SendResult = {message, dispatch}` (**D4**). Add `patchChat({project_id, model, routing, run_mode})`, `classifyChat`, `planFromChat`, `confirmPlan`. Drop `listFolders`. |
| `useChats.ts` | **modify** | See §4. Delete the duplicate `useModelOptions` — `features/projects/hierarchyHooks.ts:291` has a second copy; both die in favour of `features/models/useModels.ts`. |
| `chats.module.css` | **modify** | Centered column, overlay drawer, composer chrome. |
| `WorkspaceTabs.tsx` | **modify** | Add a Plan tab; hoist to `features/board/TaskDetailDrawer` usage. |

### 3.2 Frontend — board & shared

| File | Status | Responsibility |
|---|---|---|
| `features/board/TaskDetailDrawer.tsx` | **new** | The six-tab overlay of §2.2. Mounts `<ChatSurface variant="panel">`. |
| `features/board/TaskOverview.tsx` | **new** | Progress ring, stepper, agents, acceptance, remaining line. |
| `features/board/PlanView.tsx` | **new** | Planner brief + checklist + workbook as components. Replaces every `<pre>` JSON dump. |
| `features/board/AttemptTimeline.tsx` | **new** | Lifted out of `app/activity/[taskId]/page.tsx:220`, de-inlined. |
| `features/board/eta.ts` + `eta.test.ts` | **new** | The ETA rule above, pure, unit-tested. |
| `features/board/BoardKanban.tsx` | **modify** | Card face gains checklist progress bar + remaining line + `chat_origin` badge. `onClick` → open `TaskDetailDrawer`. |
| `app/board/page.tsx` | **modify** | Mount the drawer; add `?task=` handling beside existing `?files=`/`?run=`/`?project=`. |
| `features/models/useModels.ts` | **new** | The single `/api/models` hook. Both existing copies deleted. |
| `app/chats/page.tsx` | **rewrite** | Thin: layout + selection + dialogs. All logic moves into `ChatSurface`. |
| `e2e/product.spec.ts` | **modify** | Specs at lines 47–87 assert v0 behaviour ("No chats yet" empty-state copy, `New Chat` dialog flow). Update in the same PR or they go red. |

### 3.3 Backend — `omniagentos/`

| File | Status | Responsibility |
|---|---|---|
| `chats/bridge.py` | **new** | `ChatTurnBridge` — streams and closes turns. §5.1. |
| `chats/classify.py` | **new** | Project suggestion via `ShortCallClient`. §5.5. |
| `chats/store.py` | **modify** | `to_dto()` projection; `project_id` on update; `list_chats` returns `message_count` + `last_message_at` via a grouped join on `conversations`. |
| `api/routes/chats.py` | **modify** | DTO responses, `PATCH` field expansion, `/classify`, `/plan`, register with the bridge. |
| `memory/runner_hook.py` | **modify** | `safe_persist_agent_turn` gains idempotency by `meta.session_id`. |
| `sessions/supervisor.py` | **modify** | In `_finish`, on `COMPLETED`, call the bridge's `close_turn`. ~12 lines, best-effort, inside the existing try/except discipline. |
| `intake/service.py` | **modify** | `dispatch_spec` writes `project_id` onto the board card. |
| `api/routes/intake.py` | **modify** | `GET /api/board` accepts `project_id`; emits `project_id`, `chat_origin`, `planner_brief`, `checklist`. |
| `db/migrations/086_board_project_scope.sql` | **new** | §5.6. |
| `tests/chats/test_turn_bridge.py`, `test_chat_dto.py`, `test_classify.py`; `tests/collab/test_board_project_scope.py` | **new** | |

---

## 4. State management

**Three layers, no global store.** The app has no Redux/Zustand and should not gain one.

**Layer 1 — server cache (module-level, in `useChats.ts`).**
A `Map<chatId, {messages, etag, fetchedAt}>` outside React. `selectChat(id)` renders from cache **synchronously** (satisfying the brief's "opens instantly from cache"), then revalidates in the background. Cache survives navigation between `/chats` and `/board`, which is what makes the board panel feel instant.

**Layer 2 — SSE reducer.** One `useReducer` in `useChatThread` over the shared `useEventChannel(["chat.turn.started","chat.turn.delta","chat.turn.completed"])`. Actions: `TURN_STARTED | DELTA | COMPLETED | SENT | LOADED | ERROR`. This replaces the current five-`useState`-plus-three-`useRef` arrangement (`useChats.ts:210-212`), which loses deltas that arrive between the `started` event and the ref assignment.

Reducer invariants:
- `DELTA` for a `turn` lower than the current turn is dropped (out-of-order SSE).
- `COMPLETED` replaces the optimistic streaming buffer with the server message and dedupes on `meta.session_id + turn`.
- `SENT` inserts the user message optimistically with `pending: true`; the server response reconciles by id.

**Layer 3 — URL as the source of truth for selection.**
`/chats?c=<chatId>` and `/board?task=<id>`. Deep-linkable, back-button correct, and it makes the board→chat→board round trip free.

**Streaming fallback (non-negotiable).** If `chat.turn.started` fired and no `delta` arrives within **6s**, `useChatThread` starts polling `GET /api/chats/{id}/messages` every 3s until a terminal message appears or 15 minutes elapse. Under the bridge failing entirely, the chat still works — it just isn't live. Given D1/D2, shipping without this fallback means shipping a chat that can silently stop answering.

---

## 5. Backend additions — precise contracts

### 5.1 `ChatTurnBridge` — the unlock (fixes D1, D2)

`omniagentos/chats/bridge.py`. A process-local singleton owning a registry of open turns and one daemon tailer thread (started lazily, idle-exits after 60s with an empty registry).

```python
register(chat_id: str, session_id: str, task_id: str, turn: int) -> None
close_turn(session_id: str, *, final_text: str | None = None) -> None   # idempotent
```

- `POST /api/chats/{id}/messages` calls `register(...)` with `dispatch_result["session_id"]`.
- The tailer polls every **250 ms**, reading each open session's transcript delta **through the DAL/manifest directly** (the same byte-offset + rotation-guard logic as `GET /api/sessions/{id}/transcript/delta`, `routes/sessions.py:513` — extracted into `sessions/chain_read.py` so there is one implementation, not two). New assistant text is coalesced and emitted as `chat.turn.delta`, **throttled to ≤4/s per chat** per the pinned contract.
- On terminal session state, or on `supervisor._finish(COMPLETED)`, `close_turn` runs: append the full assistant text to `scope_type="chat", scope_id=<chat_id>` with `meta={"session_id":…, "turn":…}`, then emit `chat.turn.completed`.
- **Hard timeout: 15 minutes.** The turn is closed with whatever text exists and `meta.timed_out = true`; the UI renders a "Turn timed out" system chip with a Retry. An unbounded registry is a memory leak on a long-lived API process.

**Idempotency** — `close_turn` first runs
`SELECT 1 FROM conversations WHERE scope_type='chat' AND scope_id=? AND json_extract(meta_json,'$.session_id')=?`
and returns early on a hit. Both writers (bridge and supervisor) are therefore safe, in any order, in any process.

**Session → chat resolution** (`sessions` has no `board_task_id`; verified):
`session_id` → `SELECT id, origin FROM board_tasks WHERE result_ref = ?` → `origin='chat'` → `SELECT id FROM chats WHERE board_task_id = ?`. `dispatch_spec` already records the session id in `result_ref`.

### 5.2 Chat DTO (fixes D3)

All chat responses project a stable DTO. `meta` is retained for forward-compat; the flat fields are authoritative.

```jsonc
// GET /api/chats  ->  ChatDTO[]        GET|PATCH|POST /api/chats/{id} -> ChatDTO
{
  "id": "cht_…", "title": "…", "status": "active|archived|promoted|deleted",
  "project_id": "prj_…|null", "project_name": "Platform|null",
  "board_task_id": "btk_…",
  "preferred_model": "grok-4.5|null",
  "run_mode": "chat|plan|swarm",
  "routing": {"mode":"auto|pinned","model":null,"allow":[],"deny":[],
              "effort":"low|medium|high|null","speed":"fast|auto|ultra|null"},
  "project_suggestion": {"project_id":"prj_…","name":"Platform",
                         "confidence":0.82,"rationale":"…"} | null,
  "message_count": 8, "last_message_at": "ISO|null",
  "promoted_at": null, "created_at": "ISO", "updated_at": "ISO",
  "meta": { … }
}
```

`message_count` / `last_message_at` come from one grouped join in `list_chats`, not N+1 queries:
`LEFT JOIN (SELECT scope_id, COUNT(*) c, MAX(created_at) m FROM conversations WHERE scope_type='chat' GROUP BY scope_id)`.

### 5.3 `PATCH /api/chats/{id}` (fixes D5)

```jsonc
// request — every field optional; omitted = no change
{ "title": "…", "status": "active|archived",
  "project_id": "prj_…|null",          // null unassigns; unknown id -> 404
  "model": "grok-4.5|null",            // null = auto
  "routing": { … }, "run_mode": "chat|plan|swarm", "meta": {…} }
// 200 -> ChatDTO   404 {"error":{"code":"not_found",…}}   400 validation
```

`project_id` is written to the `chats.project_id` **column** (it exists, migration 073) and mirrored onto the companion board task's new `project_id` column so the board scope filter agrees.

### 5.4 `POST /api/chats/{id}/messages` (fixes D4)

Response shape unchanged — the **client** is wrong, not the server. Pinned here so it stops drifting:

```jsonc
201 -> { "message": { "id":"cnv_…","seq":3,"role":"user","content":"…",
                      "model":"grok-4.5|null","created_at":"ISO","meta":{…} },
         "dispatch": { "session_id":"ses_…","board_task":{…},"task_id":"…","run_id":null } }
```

Request gains `run_mode` and `count`:
`{"content": str, "model": str|null, "run_mode": "chat|plan|swarm", "count": 1..10, "meta": {"attachments":[…]}}`.
`run_mode="swarm"` routes to the existing spawn path and returns `{"message":…, "task_ids":[…]}`.

### 5.5 `POST /api/chats/{id}/classify` — project auto-classify (requirement 3)

Fire-and-forget from the **first** user message; also callable directly.

```jsonc
// 200
{ "project_id":"prj_…"|null, "name":"Platform"|null,
  "confidence":0.0-1.0, "rationale":"mentions cascade routing and the API layer" }
```

Implementation: `ShortCallClient` (budgeted, per `FINAL-PLAN §E.5`) given `(id, name, description)` for every project in `/api/projects/tree` plus the first message; `response_format={"type":"json_object"}`; `purpose="chat_project_classify"`. Result is written to `meta.project_suggestion` — **never** to `project_id`. Failure writes nothing and logs at debug; the bar simply does not appear. Confidence `< 0.5` is discarded server-side.

The existing orgdims classification of the companion task (`POST /api/orgdims/classify/board_task`) is unchanged and continues to run.

### 5.6 Board project scope (fixes D7)

`omniagentos/db/migrations/086_board_project_scope.sql` (086 is the next free — 085 is `lab_jobs`):

```sql
ALTER TABLE board_tasks ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_board_tasks_project ON board_tasks(project_id);
-- Backfill via the only link that exists today: board_tasks -> runs -> tasks.
UPDATE board_tasks SET project_id = (
  SELECT t.project_id FROM runs r JOIN tasks t ON t.id = r.task_id
  WHERE r.id = board_tasks.run_id
) WHERE run_id IS NOT NULL AND project_id IS NULL;
-- Chat companion tasks inherit their chat's project.
UPDATE board_tasks SET project_id = (
  SELECT c.project_id FROM chats c WHERE c.board_task_id = board_tasks.id
) WHERE origin = 'chat' AND project_id IS NULL;
```

Read-time resolution alone is insufficient: session-mode cards (every chat card) have no run, so the join yields nothing. `dispatch_spec` already receives `project_id` and must write it onto the card at creation.

`GET /api/board` gains `project_id: str | None = None` and, when set, filters server-side. Response gains four fields already declared in `features/collab/types.ts` but never emitted:

```jsonc
{ …existing…,
  "project_id": "prj_…|null",
  "chat_origin": {"chat_id":"cht_…","title":"…"} | null,   // join chats.board_task_id
  "planner_brief": "markdown|null",
  "checklist": {"done": 3, "total": 7} | null }
```

Spawned sub-tasks stay excluded from the top-level board (the `origin != 'chat'` filter is correct) but are returned by a new `GET /api/board?parent_task_id=<id>` for the detail drawer's subtask disclosure — otherwise Fan out produces invisible work.

### 5.7 Plan mode (requirement 4) — no new endpoints

`Plan` mode reuses `POST /api/intake/plan` `{goal, execute:"session", speed}` → `202 {job_id}` → poll `GET /api/intake/plan/{job_id}` until `status:"ready"` → render `PlanCard` → `POST /api/intake/plan/{job_id}/confirm` `{project_override, speed}` → `201`. `project_override` is `"auto"`, an existing project id, or `"new:<name>"`, driven by the card's project Select. Confirm is already idempotent per job id.

The only addition is a thin `POST /api/chats/{id}/plan` that seeds the goal from the thread and records `job_id` in `meta.plan_job_id`, so a reload re-attaches to a running plan job instead of orphaning it.

---

## 6. Keyboard map

| Key | Scope | Action |
|---|---|---|
| `⌘N` | global | New chat (creates immediately, focuses composer — **no dialog**; the title comes from the first message) |
| `⌘K` | global | Command palette |
| `⌘J` | chat | Open model picker |
| `⌘⇧M` | chat | Cycle RunMode Chat → Plan → Swarm |
| `Enter` | composer | Send |
| `⇧Enter` | composer | Newline |
| `↑` | empty composer | Edit last user message |
| `Esc` | composer | Clear mention popover → clear draft → blur |
| `Esc` | drawer/dialog | Close |
| `⌘\` | chat | Toggle sidebar |
| `⌘I` | chat | Toggle workspace drawer |
| `⌘F` | chat | Focus sidebar search |
| `↑ / ↓` | sidebar | Move selection · `Enter` opens · `⌫` archives (with undo toast) |
| `⌘⇧P` | chat | Promote to Board |
| `Tab / ↑ ↓` | mention popover | Navigate · `Enter` accepts |

**`⌘N` changes behaviour deliberately.** The current dialog (`app/chats/page.tsx:385`) asks for a title and a folder before you can type — the opposite of "chat front and center." Create silently with title `"New chat"`, then `PATCH` the title from the first message's first ~60 chars (server-side, in the message POST, only when the title is still the default).

One conflict to note: `⌘N` is the browser's new-window shortcut. `preventDefault` works in Chrome/Edge for `⌘N`? No — it is reserved in some browsers. The existing handler already claims it; keep it, and **also** bind `⌘⇧O` (ChatGPT's binding) as an alias so the shortcut is reachable everywhere. Document both in the palette.

---

## 7. Empty, loading, and error states

Every state below uses `EmptyState` / `ErrorState` / `Loading variant="skeleton"` from `@/design`. No bare spinners; no unstyled error text.

| Surface | Empty | Loading | Error |
|---|---|---|---|
| Sidebar | "No chats yet — press ⌘N to start." + primary `New chat` | 6 skeleton rows at final height (no layout shift) | `ErrorState` with `onRetry`; sidebar stays interactive |
| Project group | "Drop a chat here to add it to Platform." (drop-target styling persists) | — | — |
| Thread (no selection) | "Select a chat, or press ⌘N." | — | — |
| Thread (new chat) | Centered: title "What are we working on?", three example prompts as buttons that fill the composer, and the current model + mode rendered as text so the routing choice is visible before the first send | 3 skeleton bubbles | `ErrorState` "Could not load this conversation" + Retry; composer **stays enabled** so the user can still send |
| Streaming | — | Agent bubble with a 1ch blinking caret + `running…` chip. After 6s with no delta: chip becomes `working — no live output` and the poll fallback engages | On `chat.turn` error frame: inline system bubble "The agent stopped: `<reason>`" + Retry, which re-sends the last user message |
| Composer | — | Send button → "Sending…", textarea stays editable | Inline `composerError` below the row; the draft is **never** cleared on failure (today `handleSend` clears optimistically only on success — keep that) |
| Model picker | "No models available — check Connections." + link to `/connections` | Skeleton list | Falls back to `[{id:"auto"}]`; a `warn` Badge reads "Catalog unavailable" |
| Suggestion bar | hidden below 0.5 confidence | never shown while pending | hidden on error |
| Board detail — Plan | "No plan was recorded for this task." | skeleton | ErrorState + Retry, other tabs unaffected |
| Board detail — Runs | "This task has not been attempted yet." | skeleton | ditto |
| Board detail — Approvals | "Nothing waiting on you." | — | ditto |
| Board with `?project=` | "No cards in **Platform** yet. Promote a chat, or clear the filter." + `Clear filters` | skeleton | ditto |

---

## 8. Visual specification

**Tokens.** The brief says `var(--ds-*)`; the actual custom properties in `theme.css` are `--space-*`, `--text-*`, `--radius-*`, `--motion-*`, `--accent`, `--surface`, `--canvas`, `--border`, `--text-muted` (only `--ds-accent-pulse` and `--ds-chart-*` carry the `ds` prefix). Build against the real names. `tokens.ts` stays frozen; anything new goes into `theme.css` as a custom property, owned by S7.

**Layout.**
- Sidebar `15rem`; collapsed `0` with a `120ms var(--ease-out)` flex-basis transition.
- Thread column `max-width: 46rem; margin-inline: auto;` — panel variant `38rem`.
- Composer docks to the bottom of the **column**, `1px` top hairline, `var(--surface-raised)`, `--radius-lg`, `var(--space-3)` padding.
- Drawer is `position: absolute; right: 0; width: 22rem;` translating in over `160ms`, with a scrim only below `1280px`.

**Message bubbles.** User: right-aligned, `--surface-raised`, `--radius-lg`. Agent: full-width, no bubble fill, `--text` on `--canvas`, a `2px` `--accent` left rule only while streaming. Role label + model badge + time in `--text-micro` `--text-faint`, above the block. This is the ChatGPT/Claude convention and it reads far calmer than two competing bubbles.

**Motion.** 120–200 ms `--ease-out` on hover, disclosure, drawer, sidebar collapse. Streaming caret is a `1s step-end` opacity blink. `@media (prefers-reduced-motion: reduce)` disables the caret animation and the drawer translate. No gradients, no glassmorphism, no shadow stacks.

**Dynamic values** go through CSS custom properties per the lint rule at `eslint.config.mjs:108` — e.g. progress bars use `style={{ "--pct": \`${pct}%\` }}` with the module class consuming `var(--pct)`. That form is explicitly allowed; any other inline style is an error.

---

## 9. Build order

Each step is independently shippable and independently verifiable.

| Step | Contents | Verify |
|---|---|---|
| **0 — Unblock** (backend, ~1 d) | `ChatTurnBridge`; supervisor `close_turn`; idempotency guard; `chat.turn.delta/completed` emission | `pytest tests/chats/test_turn_bridge.py`; curl: create → send → agent turn appears in `GET /api/chats/{id}/messages` **without a reload** |
| **1 — Contracts** (~0.5 d) | ChatDTO; `PATCH` field expansion; `sendMessage` envelope fix; `meta` vs `meta_json`; delete both `useModelOptions` copies | `npx tsc --noEmit`; folder-free chat list renders with `project_id` populated |
| **2 — Chat v2** (~2 d) | `ChatSurface`, sidebar rewrite, composer + RunMode + ModelPicker, thread reducer, poll fallback, empty states, e2e spec update | `npm run lint && npm test && npm run build`; Playwright: create → send → streamed reply renders |
| **3 — Projects** (~1 d) | drag-to-project, `/classify`, `ProjectSuggestionBar`, migration 086, `GET /api/board?project_id=`, `project_id` on dispatch | `/board?project=<id>` shows exactly that project's cards (today: empty) |
| **4 — Board detail** (~1.5 d) | `TaskDetailDrawer` + six tabs, `chat_origin` / `planner_brief` / `checklist` emission, subtask disclosure, `eta.ts` | every tab renders from live data; `<pre>` dumps gone |
| **5 — Plan mode** (~1 d) | `PlanCard`, `POST /api/chats/{id}/plan`, confirm flow, `SpawnCard` | plan → approve → cards land on `/board` |

Full ladder per `TESTING.md` at each merge; `./scripts/certify-omniagentos.sh` must stay green.

---

## 10. The five highest-risk details

**R1 — The reply write-back has two writers in possibly two processes.**
The bridge lives in the API process; `supervisor._finish` may run in a worker. Both call `close_turn`. Without the `json_extract(meta_json,'$.session_id')` guard, every chat turn double-posts — and `conversations` has `UNIQUE(scope_type, scope_id, seq)` with seq allocated under a lock, so the duplicate *succeeds* and the user sees the reply twice. **Mitigation:** the existence check is inside the same `_lock`-held transaction as the append, not before it. Test explicitly: call `close_turn` twice concurrently from two threads and assert exactly one row. This is the single most likely way to ship a visibly broken chat.

**R2 — The tailer is an unbounded background thread reading files on the hot path.**
A registry that never drains leaks; a session whose transcript never terminates pins a file handle forever; 250 ms polling across many open chats competes with `run_once`, which already walks every session row on every pass. **Mitigation:** hard 15-minute per-turn timeout; registry capped at 32 concurrent turns (33rd send returns `503` with a clear message rather than degrading everyone); one shared thread, never one per chat; the tailer reads through the manifest path with the existing rotation guard, never holding a handle across polls. And the client-side poll fallback (§4) means the bridge failing degrades to slow, not broken.

**R3 — Migration 086 backfill is lossy, and the UI must not pretend otherwise.**
Cards created before 086 with no run — hand-created cards, external-session projections (`sessions/external_board.py`) — get `project_id = NULL` and vanish from every project-scoped view. Silently. **Mitigation:** the project filter's empty state names the gap explicitly ("Cards created before project tracking aren't scoped — clear the filter to see all N."), and the board toolbar shows `visible / total` (it already does). Do **not** attempt a heuristic backfill from titles. Also: `ALTER TABLE … REFERENCES projects(id)` on SQLite adds the column without enforcing the FK on existing rows — that is fine, but the code must tolerate a `project_id` pointing at a deleted project (render the id, not a crash).

**R4 — The classifier can quietly become a data-loss vector.**
An LLM suggesting a project is harmless; an LLM *assigning* one moves a chat out from under the user. The line between them is one careless refactor. **Mitigation:** `project_id` is not writable by `classify.py` — the module has no access to `update_chat`; it returns a value that only `routes/chats.py` writes into `meta.project_suggestion`. Enforce with a test that asserts `chats.project_id` is unchanged after `POST /classify` on a seeded chat. Budget: the call is `ShortCallClient` with `purpose="chat_project_classify"` and fires **once per chat**, on the first message only, guarded by `meta.classified_at` — not on every send.

**R5 — Deleting folders is a one-way migration with live user data.**
`meta.folder` is free text; projects are FKs. A folder named "Engineering" may or may not correspond to a project. Auto-creating projects from folder names could spray junk projects into the tree that then appear on the board, in orgdims, and in the portfolio view. **Mitigation:** do **not** auto-create. On first load after Step 1, a one-time dismissible banner lists distinct folder names with a per-folder action: *Create project* / *Map to existing…* / *Ignore*. Until the user acts, folder-only chats appear under **Recents** and nothing is lost — `meta.folder` is retained in `meta` for one release so the migration is reversible. `GET /api/chats/folders` returns `{"folders": []}` rather than 404ing, so any un-updated client degrades to "no folders" instead of erroring.

---

**One flag on scope.** Requirement 5 says "the backend already tracks most of this; surface it." That is true for agents, plan, checklist, files, runs, and approvals — every one maps to an existing endpoint. It is **not** true for **ETA**: nothing in the backend estimates completion. I have specified a client-side derivation with an explicit "Estimating…" state rather than leaving it undefined, but it is an estimate from step timings, not a tracked value. If the owner wants a real ETA, that is a separate backend workstream (per-step duration history keyed by task shape), and I would not fold it into this build.
