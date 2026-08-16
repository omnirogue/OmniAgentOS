File write wasn't permitted in this session, so the spec is delivered here in full (it can be pasted into `docs/ui-redesign/chat-v2/SPEC.md`, which sits empty and ready).

# Chat v2 + Kanban Chat — FINAL IMPLEMENTATION SPEC

**Author:** Fable (chief product architect) · **Date:** 2026-07-27
**Inputs:** `design-kimi.md`, `design-qwen.md`, `design-opus.md`, `FINAL-PLAN.md` §A+§B (binding), and direct code verification of every defect claim.
**Verdict on Opus's defect table: all seven claims (D1–D7) verified in the current tree. All seven are P0.** The two structural problems (dual taxonomy, `origin='chat'` exclusion) are also confirmed, as P0-8/P0-9.

Synthesis stance: Opus's defect-first architecture and backend contracts are the spine (the only design grounded in verified code). Kimi's composer control-deck and honesty rules (suggestion-first classify, no fake ETA) shape the controls. Qwen's grow-a-chat-from-any-card and embedded-panel props shape the kanban dock. Where the designs disagreed with the owner's stated controls (Opus merged mode+orchestration into one control), the owner wins: **Mode `Plan|Regular` and Orchestration `Auto|Solo|Fan-out` are two separate controls.**

---

# PART 1 — P0 DEFECT FIXES

Every item re-verified in the working tree today; line numbers are current.

## P0-1 (D1) — The agent never replies. **VERIFIED**

**Evidence.**
- `safe_persist_agent_turn` has exactly one call site: `omniagentos/runner/core.py:2743-2751`, gated on `state == RunState.COMPLETED`, called **without** `board_task_id`.
- Chat sends dispatch `execute="session"` (`omniagentos/api/routes/chats.py:326`). Per `dispatch_spec` (`omniagentos/intake/service.py:1089`, docstring 1140-1153): session mode spawns a live session, creates **no run**, records the session id on the card's `result_ref`.
- The no-run fallback `_resolve_board_task_id_from_run` (`omniagentos/memory/runner_hook.py:103-127`) walks `runs.task_id → board_tasks.run_id` — with no run it returns `None`, so the chat write-back at `runner_hook.py:153-164` never fires.
- `sessions/supervisor.py` contains zero conversation writes (verified by grep across `_finish`/`_process_finished`).
- Damning detail: `safe_persist_chat_agent_turn` (`runner_hook.py:167-187`) was purpose-built for exactly this session-mode write-back — and **is never called from anywhere**.

**Root cause.** The reply pipeline was designed for the runner lane and shipped against the session lane. Nothing in the session lifecycle writes agent text to `scope_type='chat'` or emits streaming events.

**Fix contract (with P0-2, one work item).** New `omniagentos/chats/bridge.py` — `ChatTurnBridge`, process-local singleton:

```python
register(chat_id: str, session_id: str, task_id: str, turn: int) -> None
close_turn(session_id: str, *, final_text: str | None = None) -> None   # idempotent
```

- `POST /api/chats/{id}/messages` calls `register(...)` with `dispatch_result["session_id"]` after dispatch.
- One shared daemon tailer thread (lazy start, idle-exit after 60s empty registry) polls each open session's transcript every **250ms** through the same byte-offset + rotation-guard logic as `GET /api/sessions/{id}/transcript/delta` (`omniagentos/api/routes/sessions.py:513`) — extracted into `omniagentos/sessions/chain_read.py` so there is one implementation. New assistant text coalesces into `chat.turn.delta`, throttled **≤4/s per chat** (pinned, FINAL-PLAN §B).
- On terminal session state (tailer-observed) **or** from `supervisor._finish` on `COMPLETED` (~12 best-effort lines), `close_turn`: append the full assistant text via the existing dead function `safe_persist_chat_agent_turn(store, chat_id=…, content=…, model=…, board_task_id=…)` extended to accept `meta={"session_id": …, "turn": …}`, then emit `chat.turn.completed`.
- **Idempotency (two writers, possibly two processes):** inside the same `_lock`-held transaction as the append, `SELECT 1 FROM conversations WHERE scope_type='chat' AND scope_id=? AND json_extract(meta_json,'$.session_id')=?` → early return on hit. Test: two threads calling `close_turn` concurrently produce exactly one row.
- **Bounds:** hard 15-minute per-turn timeout (close with partial text + `meta.timed_out=true`; UI renders "Turn timed out" chip + Retry). Registry capped at 32 open turns; the 33rd send returns `503` with a clear message. One thread total; no file handle held across polls.
- **Client fallback (non-negotiable):** if `chat.turn.started` fired and no delta arrives within 6s, `useChatThread` polls `GET /api/chats/{id}/messages` every 3s until a terminal message appears or 15 min elapse. Bridge failure degrades to slow, never silent.

## P0-2 (D2) — `chat.turn.delta` / `chat.turn.completed` never emitted. **VERIFIED**

**Evidence.** Only `chat.turn.started` is emitted (`routes/chats.py:309-316`); repo-wide grep finds no other emission. The client subscribes to all three (`dashboard/src/features/chats/useChats.ts:253-255`) and appends the agent message only on `completed` (`useChats.ts:287-306`) — `turnState` sticks forever.

**Fix contract.** The bridge is the sole emitter of `delta`/`completed`, via the same `_emit_chat_event` helper. Shape pinned (§B): `{"type": …, "chat_id", "task_id", "turn", "text"?, "model"?, "ts"}`. Client: replace the five-`useState`-plus-three-`useRef` arrangement (`useChats.ts:197-212`) with one `useReducer` (`TURN_STARTED|DELTA|COMPLETED|SENT|LOADED|ERROR`); drop out-of-order deltas for a lower `turn`; `COMPLETED` replaces the streaming buffer and dedupes on `meta.session_id + turn`.

## P0-3 (D3) — Every chat lands in Inbox (nested `meta` vs flat client type). **VERIFIED**

**Evidence.** `_chat_dict` returns `{…row, meta: {…}}` (`omniagentos/chats/store.py:44-47`); routes return it raw (`routes/chats.py:169,207,228`). The TS `Chat` type expects flat `folder`/`preferred_model` (`chatApi.ts:32-44`, lines 38-39) — both always `undefined` client-side.

**Fix contract.** All chat responses project the **ChatDTO** (§3.1) with flat authoritative fields; `meta` retained for forward-compat. Implemented as `to_dto()` in `chats/store.py`; `message_count`/`last_message_at` via one grouped `LEFT JOIN` on `conversations` — no N+1.

## P0-4 (D4) — The user's own message renders blank after send. **VERIFIED**

**Evidence.** Server returns `{"message": user_turn, "dispatch": dispatch_result}` (`routes/chats.py:334-337`); client types the whole envelope as `ChatMessage` (`chatApi.ts:508-527`, mutate at :523) — `msg.id`/`msg.content` undefined.

**Fix contract.** The **server shape is correct and pinned** (it must carry `dispatch.session_id` for the bridge). Fix the client: `sendMessage` returns `SendResult = { message: ChatMessage, dispatch: { session_id, task_id, run_id, board_task } }`; `useChatThread` reconciles the optimistic user message by `message.id` and hands `dispatch.session_id` to turn state.

## P0-5 (D5) — Per-chat model change silently no-ops. **VERIFIED**

**Evidence.** Client PATCHes `{model}` (`chatApi.ts:457-484`); `UpdateChatRequest` has only `title/status/folder/meta` (`routes/chats.py:125-129`) — Pydantic drops it. Store `update_chat` (`chats/store.py:177-221`) likewise cannot touch `project_id`.

**Fix contract.** Expand `UpdateChatRequest` + store per §3.2: `model` → `meta.preferred_model` (the send path already reads it, `routes/chats.py:277`); `project_id` → the real `chats.project_id` column (exists since migration 073), mirrored onto the companion board task (P0-7).

## P0-6 (D6) — Attachment pills never render (and never persist). **VERIFIED, worse than claimed**

**Evidence.** Read side: `ConversationStore._message` pops `meta_json`, returns parsed `meta` (`omniagentos/conversations/store.py:44-47`); `ChatThread.parseAttachments` reads `message.meta_json` (`ChatThread.tsx:46`, :218) → always undefined. **Send side (new finding):** `ChatSendRequest` sends `meta_json: string` (`chatApi.ts:63-68`) while the server expects `meta: dict` (`routes/chats.py:132-135`) — attachments silently dropped **in both directions**.

**Fix contract.** One shape both directions: `meta: { attachments: [{kind: "skill"|"file"|"url", ref, label}] }` (object, matching the pinned manifest). Client: `ChatMessage.meta?`, `ChatSendRequest.meta?`, `parseAttachments(message.meta)`; delete `meta_json` from client types. Server unchanged (already validates `meta.attachments`, `routes/chats.py:88-110`).

## P0-7 (D7) — `/board?project=` blanks the board. **VERIFIED**

**Evidence.** `board_tasks` has no `project_id` column (`004_collab.sql:18-32` + every `ALTER TABLE board_tasks` migration — verified exhaustively). `GET /api/board` accepts only `archived` (`routes/intake.py:1364-1376`). Client chain: `?project=` → `projectFilter` (`app/board/page.tsx:104-109`) → `filterBoardTasks` compares `task.project_id !== filters.projectId` (`features/board/filters.ts:60`) — the field is declared (`features/collab/types.ts:115`) but never emitted, so every card fails the filter.

**Fix contract.**
1. **Migration `086_board_project_scope.sql`** (086 verified next free; 085 = `lab_jobs`) — §3.8: column + index + two backfills.
2. **Write path:** `dispatch_spec` persists its `project_id` argument onto the card at creation — read-time joins can't cover session-mode cards (no run).
3. **Read path:** `GET /api/board` gains `project_id` param, filters server-side, emits `project_id` per card (§3.9). `filterBoardTasks` stays — it becomes correct.
4. `PATCH /api/chats/{id} {project_id}` mirrors onto the companion task so chat cards move with their chat.
5. **Honesty (Opus R3):** pre-086 cards with no run and no chat stay `NULL` and drop from scoped views — the scoped empty state must say so ("Cards created before project tracking aren't scoped — clear the filter to see all N"). No heuristic title backfill. Tolerate `project_id` pointing at a deleted project (SQLite won't enforce the FK on existing rows).

## P0-8 — Two competing taxonomies: `meta.folder` vs `project_id`. **VERIFIED**

**Evidence.** Folders: free-text `meta_json.folder`, Python-filtered (`chats/store.py:146-159`), listed by `GET /api/chats/folders` (`routes/chats.py:185-194`), written via PATCH `folder` (:219-220). Projects: real FK `chats.project_id` (073) — which no UI path uses because of P0-3/P0-5. Sidebar groups by folder; board scopes by project; they cannot agree.

**Fix contract — projects are the sole taxonomy; folders retire non-destructively.**
- Sidebar renders **Projects** (`GET /api/projects/tree`, `routes/hierarchy.py:62`) + **Recents** (`project_id IS NULL`, time-bucketed). No folder section.
- UI stops writing `folder`. Server keeps accepting PATCH `folder` and serving `/api/chats/folders` (pinned shape `{"folders":[…]}`) for one release — do not delete the endpoint.
- One-time dismissible banner when legacy folders exist: per-folder *Create project* / *Map to existing…* / *Ignore*; mapping = bulk `PATCH project_id`; `meta.folder` left in place (reversible). **Never auto-create projects from folder names** (junk would leak into orgdims/portfolio).

## P0-9 — `origin='chat'` board exclusion hides spawned sub-tasks. **VERIFIED**

**Evidence.** `GET /api/board` drops every `origin='chat'` card (`routes/intake.py:1376`). But `create_spawn_task` gives spawned sub-tasks the same `origin='chat'` (`chats/store.py:252-289`, insert :282; parent recorded in `org_json` `{"parent_task_id", "chat_id", "spawned": true}`). "Fan out" creates invisible work.

**Root cause.** One flag encodes two things: hidden companion plumbing and chat-spawned real work.

**Fix contract.** Distinguish by what defines a companion — being pointed at by a chat:
- `live_board` excludes only cards whose id is in the prefetched `chats.board_task_id` set (`SELECT board_task_id FROM chats WHERE status != 'deleted'` — no schema change, no per-row query). Spawned sub-tasks become visible, in their project (they inherit the chat's `project_id` at spawn — add to `create_spawn_task`).
- New `GET /api/board?parent_task_id=<id>` returns children (match `org_json.parent_task_id`) including companions — feeds the drawer's sub-task disclosure.
- The `origin` CHECK constraint stays untouched — no table rebuild.

---

# PART 2 — V2 FEATURE SPEC

> North star (all three designs converge): **the thread is the product; projects are the only grouping; everything else is a drawer.**

## 2.1 `/chats` layout — thread-dominant

```
┌───────────────┬──────────────────────────────────────────────┬─────────┐
│  ChatSidebar  │                ChatSurface                   │ Drawer  │
│    15rem      │  ChatHeader — title · project chip · ⋯      │(overlay)│
│  ⌕ Search     │ ┌──────────────────────────────────────────┐ │  22rem  │
│  ▸ Platform 4 │ │     centered thread column, 46rem        │ │ Plan    │
│    ▸ API Layer│ │  [ProjectSuggestionBar — "Looks like…"]  │ │ Files   │
│  ▸ Growth   2 │ │  user turn (right, raised surface)       │ │Activity │
│  Recents      │ │  agent turn (full width, model badge)    │ │Terminal │
│    Quick q… 2m│ │  PlanCard · SpawnCard (structured)       │ │         │
│  + New chat   │ ├──────────────────────────────────────────┤ │         │
│               │ │ ChatComposer — the control deck          │ │         │
│               │ │ [textarea; @skill, 📎]                   │ │         │
│               │ │ [Model ▾] [Regular|Plan] [Auto|Solo|Fan] │ │         │
└───────────────┴─┴──────────────────────────────────────────┴─┴─────────┘
```

- Thread column `max-width: 46rem; margin-inline: auto` (panel variant `38rem`). Sidebar `15rem`, collapsible (`⌘\`).
- Workspace drawer becomes an **overlay** (`position:absolute; right:0; width:22rem`, 160ms translate, scrim below 1280px) — never a flex sibling; today it squeezes the thread.
- Bubbles: user right-aligned on `--surface-raised` `--radius-lg`; agent full-width, no fill, 2px `--accent` left rule only while streaming; role + model badge + time in `--text-micro`/`--text-faint`. Anchored auto-scroll: only follow streaming if within 64px of the bottom.
- Selection state in the URL: `/chats?c=<chatId>`, `/board?task=<id>` — deep-linkable, back-button-correct.
- Cache: module-level `Map<chatId, {messages, fetchedAt}>` outside React; `selectChat` renders synchronously from cache, revalidates in background; survives `/chats` ↔ `/board` navigation.

## 2.2 One primitive: `ChatSurface`

`dashboard/src/features/chats/ChatSurface.tsx` — **new**, the single conversation component mounted in both surfaces.

```ts
interface ChatSurfaceProps {
  chatId: string;
  variant: "page" | "panel";      // panel: single-line header, 38rem cap, compact padding
  boardTaskId?: string | null;    // binds workspace drawer + steering context
}
```

Owns header + thread + composer composition. `app/chats/page.tsx` becomes thin (layout + selection + dialogs). The board drawer mounts `<ChatSurface variant="panel">` — "one conversation primitive" is only real if it is literally one component.

## 2.3 Composer control deck

Row 1: auto-grow textarea (Enter send, ⇧Enter newline), `@skill` mentions (existing), 📎 attach, send button with turn state.
Row 2 (the deck), left→right:

1. **Model picker** (`ModelPicker.tsx`, new; `⌘J`): popover fed by `GET /api/models` (exists — `omniagentos/api/routes/models.py:31-35`), grouped by lineage, unavailable entries disabled with reason tooltip, footer toggle **"Just this message"** (off = persists via `PATCH {model}`; on = rides `POST messages {model}` only). Replaces the bare `Select`. Delete both duplicate hooks — `useChats.ts:52` and `hierarchyHooks.ts:291` — for one `features/models/useModels.ts`.
2. **Mode** — segmented `Regular | Plan` (`⌘.`). Regular sends a chat turn. **Plan** routes the next send through the plan-job machinery (§2.6): goal → `POST /api/chats/{id}/plan` → poll → `PlanCard` in-thread → *Approve & run* → confirm. Per-chat sticky (`meta.plan_mode`), identical on `/chats` and the board dock.
3. **Orchestration** — segmented `Auto | Solo | Fan-out` (`⌘⇧M` cycles), per-chat sticky (`orch_mode`):
   - **Auto** — omit `execute`: `dispatch_spec`'s documented auto-default, including the auto solo-vs-swarm upgrade (`intake/service.py` docstring 1188-1193).
   - **Solo** — `execute="session"`, exactly today's chat turn.
   - **Fan-out** — count stepper (1–10, matching `SpawnRequest.count` `le=10`, `routes/chats.py:138-140`); routes to the existing spawn path; renders `SpawnCard` with live child chips deep-linking to `/board?task=`.
   The `/spawn` slash command (`ChatComposer.tsx:190-205`) and the dead `handleModelShiftClick` (`ChatComposer.tsx:252-260`) are **deleted** — the deck replaces both.
4. **Routing preferences** ("Routing…" row in the model-picker popover): dialog persisting per-chat `routing` — `{allow: string[], deny: string[], speed: "fast"|"auto"|"ultra"|null, effort: "low"|"medium"|"high"|null, hint: string|null}`. `hint` is Kimi's free-text "tell it what models we want" — threaded into dispatch `pins`; `allow/deny/speed` map to `dispatch_spec(pins=…, speed=…)`.

## 2.4 Sidebar: projects first-class

- **Sections:** search → project groups (`useProjectTree()`, `hierarchyHooks.ts:84`; collapsible; drop targets) → **Recents** (project-less, buckets Today / Yesterday / Previous 7 days / Older) → `+ New chat`.
- **Create project:** `+` on the header → Dialog → `POST /api/projects` (`routes/projects.py:283`). `⌘⇧N`.
- **Drag chats into projects:** HTML5 DnD (no new deps). Drop on project → `PATCH {project_id}`; drop on Recents → `{project_id: null}`. Every drag has a menu equivalent (a11y/keyboard).
- **Auto-classify suggestion:** backend fires classify on the first user message, stores `meta.project_suggestion`. `ProjectSuggestionBar` renders when `confidence ≥ 0.5 && project_id === null`: "Looks like **Platform**." → `Move` / `Choose…` / `Not now`. **Never auto-moves**; classifier cannot write `project_id` (regression test enforces).
- **Native dialogs die:** `prompt()` at `ChatSidebar.tsx:306`, `confirm()` at `:327` — charter violations that block Playwright — replaced with `Dialog` + `Toast` (undo pattern).
- **⌘N change:** create silently titled `"New chat"`, focus composer — no dialog (current dialog flow is asserted by `e2e/product.spec.ts:47-93`; TEAM-Q updates it in the same PR). Server sets the title from the first message's first ~60 chars while still default.

## 2.5 Kanban chat dock

Selecting any card opens `TaskDetailDrawer` (right overlay, `/board?task=<id>` beside existing `?files=`/`?run=`/`?project=` — `app/board/page.tsx:92-109`). Its **Chat tab** is `<ChatSurface variant="panel">` — the same deck: model picker, `Regular|Plan`, `Auto|Solo|Fan-out`.

- **Card has a chat** (`chat_origin` per §3.9): the surface binds it; promoted cards deep-link back to the promotion point.
- **Card has no chat:** "Start a conversation about this task" → first send calls `POST /api/chats {board_task_id}` (§3.3) — links to the existing card instead of creating a companion (Qwen's grow-a-chat). Any card can grow a conversation; the card face gains a 💬 badge.
- **Steer-when-live (Kimi's rule, server-side, §3.4):** if the linked task's `result_ref` session is active, the message enqueues to that live session (the `enqueue_message` + `mark_steering_pending` path, `routes/sessions.py:609-633`) and the bridge registers on the **same** session for the streamed reply; otherwise it dispatches as today. Client stays dumb; composer shows a quiet "agent running — messages steer the live session" hint when `work.state` is active.

## 2.6 Task detail — the "everything" panel

Six tabs in `TaskDetailDrawer`; `/activity/[taskId]` remains the full-page deep link rendering the same components (its inline attempt timeline extracts to `features/board/AttemptTimeline.tsx`).

| Tab | Contents | Source (all verified real) |
|---|---|---|
| **Overview** | progress bar (`work.steps_done/steps_total` + `current_step`), stage stepper, **ETA**, agents-involved chips (name, model, state, cost → transcript links), acceptance criteria, blockers | card fields from `GET /api/board` (`_enrich_board_row`, `intake/service.py:3307-3451`) · `GET /api/board/{id}/sessions` (`routes/intake.py:1450` — "every session that touched this card") · `GET /api/board/{id}/longhaul` → `acceptance` (:1593-1611) · **new** `GET /api/board/{id}/eta` (§3.10) |
| **Plan** | planner brief + checklist + workbook as components — every `<pre>` JSON dump replaced | `longhaul.workbook` · **new** `planner_brief` + `checklist` on `/api/board` (§3.9) |
| **Chat** | `<ChatSurface variant="panel">` (§2.5) | `/api/chats` family |
| **Files** | existing `BoardFilesDrawer` content, inlined | `GET /api/board/{id}/files` (`routes/board_files.py:510`) |
| **Runs** | attempt timeline (seq, harness, model, duration, end reason); steering log; sub-task disclosure | `longhaul.attempts` · `GET /api/board/{id}/conversation` (:1438) · `GET /api/board?parent_task_id=` (§3.9) |
| **Approvals** | pending approval with real command + risk, Approve/Reject inline | `pending_approval` on the card (`intake/service.py:3422-3428`) · `POST /api/approvals/{id}/decision` (`routes/control.py:501`, prefix `/api`) |

**ETA honesty:** nothing in the backend estimates completion today, so §3.10 adds a real endpoint with `sample_size`/`confidence`/`basis`; the UI renders `—` + "Estimating…" whenever `estimate_seconds` is null. Never a fabricated number.

**Plan mode** reuses existing machinery end-to-end: `POST /api/intake/plan {goal, execute:"session", speed}` → `202 {job_id}` (`routes/intake.py:1138-1167`) → poll `GET /api/intake/plan/{job_id}` → `{status, plan, route, route_target_name, error}` (:1169-1183) → `PlanCard` → `POST /api/intake/plan/{job_id}/confirm {project_override, speed}` → `201` (:1185). Only addition: the thin seed endpoint §3.6 so a reload re-attaches to a running job.

## 2.7 Keyboard map

| Key | Scope | Action |
|---|---|---|
| `⌘N` (alias `⌘⇧O`) | global | New chat — instant, no dialog |
| `⌘⇧N` | global | New project |
| `⌘K` | global | Palette |
| `⌘J` | chat | Model picker |
| `⌘.` | chat | Mode `Regular ↔ Plan` |
| `⌘⇧M` | chat | Cycle Auto → Solo → Fan-out |
| `Enter` / `⇧Enter` | composer | Send / newline |
| `↑` | empty composer | Edit last user message |
| `⌘\` / `⌘I` | chat | Toggle sidebar / drawer |
| `⌘F` | chat | Focus sidebar search |
| `↑↓ · Enter · ⌫` | sidebar | Navigate · open · archive (undo toast) |
| `⌘⇧P` | chat | Promote to Board |
| `Esc` | anywhere | mention popover → draft → drawer/dialog |

## 2.8 Empty / loading / error states (all via `EmptyState`/`ErrorState`/skeletons)

| Surface | Empty | Loading | Error |
|---|---|---|---|
| Sidebar | "No chats yet — press ⌘N." + `New chat` | 6 skeleton rows, final height | `ErrorState` + retry; sidebar stays interactive |
| Project group | "Drop a chat here to add it to *X*." | — | — |
| Thread (new chat) | "What are we working on?" + 3 example prompts + current model/mode as text | 3 skeleton bubbles | `ErrorState` + Retry; composer stays enabled |
| Streaming | — | blinking caret + `running…` chip; after 6s `working — no live output` + poll fallback | inline "The agent stopped: `<reason>`" + Retry (re-sends last user message) |
| Composer send fail | draft **never** cleared; inline error below the row | "Sending…" | — |
| Model picker | "No models available — check Connections." | skeleton list | falls back to `[{id:"auto"}]` + warn badge |
| Suggestion bar | hidden < 0.5, hidden on error, never shown pending | — | silent |
| Board `?project=` | "No cards in **X** yet. Promote a chat, or clear the filter." + pre-086 disclosure | skeleton columns | `ErrorState` + retry |
| Drawer tabs | per-tab copy ("No plan was recorded." / "Not attempted yet." / "Nothing waiting on you.") | per-tab skeleton | per-tab `ErrorState`; other tabs unaffected |
| Turn timeout | "Turn timed out" chip + Retry | — | — |

## 2.9 Visual rules

Build against the **real** custom properties in `theme.css` (`--accent`, `--canvas`, `--surface*`, `--border`, `--space-*`, `--text-*`, `--radius-*`, `--motion-*` — verified: only `--ds-accent-pulse*`/chart tokens carry the `ds` prefix; the charter's "`var(--ds-*)`" means "theme.css custom properties," not a literal prefix). `tokens.ts` frozen. Zero inline styles except the lint-allowed CSS-variable form (progress percentages). Motion 120–200ms ease-out; `prefers-reduced-motion` disables caret + drawer translate. No gradients, glassmorphism, shadow stacks, or new deps.

---

# PART 3 — BACKEND ADDITIONS

All routes live under existing routers (`routes/chats.py` prefix `/api/chats`; `routes/intake.py` prefix `/api`, `intake.py:75`). Every touched router is already registered in `api/main.py` — no registration changes.

## 3.1 ChatDTO (all chat responses; fixes P0-3)

```jsonc
// GET /api/chats -> ChatDTO[] · GET|POST|PATCH /api/chats/{id} -> ChatDTO
{
  "id": "cht_…", "title": "…", "status": "active|archived|promoted|deleted",
  "project_id": "prj_…|null", "project_name": "Platform|null",
  "board_task_id": "btk_…",
  "preferred_model": "grok-4.5|null",              // from meta.preferred_model
  "orch_mode": "auto|solo|fanout",                 // from meta.orch_mode, default "solo"
  "plan_mode": false,                              // from meta.plan_mode
  "routing": {"allow": [], "deny": [], "speed": null, "effort": null, "hint": null},
  "project_suggestion": {"project_id": "prj_…", "name": "Platform",
                          "confidence": 0.82, "rationale": "…"} | null,
  "message_count": 8, "last_message_at": "ISO|null",
  "promoted_at": null, "created_at": "ISO", "updated_at": "ISO",
  "meta": { /* retained; flat fields authoritative */ }
}
```

`message_count`/`last_message_at`: one grouped `LEFT JOIN (SELECT scope_id, COUNT(*), MAX(created_at) FROM conversations WHERE scope_type='chat' GROUP BY scope_id)`. `project_name`: one prefetched projects map per list call.

## 3.2 `PATCH /api/chats/{id}` — extended (fixes P0-5, P0-7.4)

```jsonc
// request — every field optional; omitted = no change
{ "title": "…",
  "status": "active|archived",
  "project_id": "prj_…|null",      // null unassigns; unknown id -> 404
  "model": "grok-4.5|null",        // -> meta.preferred_model; null = auto
  "orch_mode": "auto|solo|fanout", // -> meta.orch_mode
  "plan_mode": true,               // -> meta.plan_mode
  "routing": { "allow": [], "deny": [], "speed": "fast|auto|ultra|null",
               "effort": "low|medium|high|null", "hint": "string|null" },
  "folder": "…",                   // back-compat one release; UI never sends
  "meta": { … } }
// 200 -> ChatDTO · 404 not_found · 400 validation
```

Setting `project_id` mirrors it onto the companion board task's `project_id` column (single transaction) so the board scope agrees.

## 3.3 `POST /api/chats` — extended (grow-a-chat)

```jsonc
{ "title": "New chat", "project_id": "prj_…|null", "model": "…|null",
  "board_task_id": "btk_…|null",   // NEW: link to an EXISTING card instead of creating
                                    // a companion. 404 unknown; 409 conflict if the card
                                    // already has a chat (chats.board_task_id UNIQUE).
  "meta": { … } }
// 201 -> ChatDTO
```

With `board_task_id`, `create_chat` skips companion creation and does **not** flip the card's `origin` (it stays a visible board card). The chat inherits the card's `project_id` when present.

## 3.4 `POST /api/chats/{id}/messages` — extended (fixes P0-4 client-side; orchestration + steer-when-live)

```jsonc
// request
{ "content": "…", "model": "…|null",
  "orch_mode": "auto|solo|fanout|null",  // per-send override; null -> chat's orch_mode
  "count": 3,                             // fan-out only, 1..10
  "meta": {"attachments": [{"kind": "skill|file|url", "ref": "…", "label": "…"}]} }

// 201, orch resolved solo/auto:
{ "message": {"id":"cnv_…","seq":3,"role":"user","content":"…","model":"…|null",
              "created_at":"ISO","meta":{…}},
  "dispatch": {"session_id":"ses_…|null","task_id":"…","run_id":null,"board_task":{…},
               "steered": false} }

// 201, orch resolved fanout:
{ "message": {…}, "task_ids": ["btk_…", …] }   // via the existing spawn path
```

Server-side routing (one place, the handler):
1. Model: `body.model` > `meta.preferred_model` > None (existing, `routes/chats.py:277`).
2. Orchestration: `body.orch_mode` > chat `orch_mode` > `"solo"`.
3. **Fan-out** → existing spawn path; children inherit the chat's `project_id`.
4. **Solo/Auto** → if the linked task's `result_ref` session is active (`queued|planning|starting|running|awaiting_approval|resuming`, same set as `routes/sessions.py:621-628`): enqueue via `dal.enqueue_message` + `mark_steering_pending` (**steered: true**) and register the bridge on that session. Else `dispatch_spec(execute="session")` for solo, `execute=None` for auto, with `pins`/`speed` derived from `routing` (`hint` + `allow`/`deny` render into `pins`).
5. Register the turn with `ChatTurnBridge`.
6. First-message hooks: fire-and-forget classify when no `project_id`/`project_suggestion`/`classified_at`; set title from first ~60 chars while still `"New chat"`.

## 3.5 `POST /api/chats/{id}/classify` — project suggestion (new)

```jsonc
// request: {}   (optional {"force": true} re-classifies)
// 200
{ "project_id": "prj_…|null", "name": "Platform|null",
  "confidence": 0.0, "rationale": "mentions cascade routing and the API layer" }
```

`omniagentos/chats/classify.py` using `ShortCallClient` (`omniagentos/llm/client.py:41`, budgeted), `purpose="chat_project_classify"`, `response_format={"type":"json_object"}`; input = `(id, name, description)` per project from the tree + the first user message. Result → `meta.project_suggestion` + `meta.classified_at`; **never** writes `project_id` (module has no access to the project-update path; a test asserts `chats.project_id` unchanged after classify). Confidence < 0.5 discarded server-side. Failure: debug log, store nothing, bar never appears. Fires once per chat (guarded by `meta.classified_at`). The existing orgdims companion-task classification (`POST /api/orgdims/classify/board_task`, `routes/orgdims.py:113`) is unchanged.

## 3.6 `POST /api/chats/{id}/plan` — plan-mode seed (new, thin)

```jsonc
{ "goal": "string|null",   // null -> seeded from the last user message + thread title
  "speed": "fast|auto|ultra|null" }
// 202 -> { "job_id": "…", "status": "running" }   (recorded to meta.plan_job_id)
```

Delegates to the existing plan-job machinery (`routes/intake.py:1138`). `meta.plan_job_id` lets a reload re-attach to a running job; cleared on confirm/discard. Poll and confirm use the existing endpoints unchanged (`project_override: "auto"|"prj_…"|"new:<name>"`).

## 3.7 Chat SSE events (fixes P0-2)

Pinned shape unchanged: `{"type": "chat.turn.started"|"chat.turn.delta"|"chat.turn.completed", "chat_id", "task_id", "turn", "text"?, "model"?, "ts"}`. `started` stays where it is (`routes/chats.py:309`); `delta` (coalesced, ≤4/s/chat) and `completed` come from `ChatTurnBridge` only. Timed-out closes carry partial text; the completed row carries `meta.timed_out: true`.

## 3.8 Migration `086_board_project_scope.sql` (new — 086 verified free; 085 = `lab_jobs`)

```sql
ALTER TABLE board_tasks ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_board_tasks_project ON board_tasks(project_id);
-- Backfill 1: runner-lane cards via the only link that exists today.
UPDATE board_tasks SET project_id = (
  SELECT t.project_id FROM runs r JOIN tasks t ON t.id = r.task_id
  WHERE r.id = board_tasks.run_id
) WHERE run_id IS NOT NULL AND project_id IS NULL;
-- Backfill 2: chat companions inherit their chat's project.
UPDATE board_tasks SET project_id = (
  SELECT c.project_id FROM chats c WHERE c.board_task_id = board_tasks.id
) WHERE origin = 'chat' AND project_id IS NULL;
```

Write paths that set `project_id` at creation from now on: `dispatch_spec` (already receives it), `ChatStore.create_chat` (companion inherits), `ChatStore.create_spawn_task` (children inherit), promote path.

## 3.9 `GET /api/board` — changed (fixes P0-7, P0-9; the REAL route, `routes/intake.py:1364`)

```
GET /api/board?archived=0&project_id=prj_…&parent_task_id=btk_…
```

- `project_id`: server-side filter on the new column.
- `parent_task_id`: children of that card (match `org_json.parent_task_id`), including companions/spawns — the drawer's sub-task disclosure.
- Default exclusion changes from `origin != 'chat'` (:1376) to **companion-only** (prefetched `chats.board_task_id` set). Spawned sub-tasks become visible.
- Every card gains four fields (declared in `features/collab/types.ts:115-125`, never emitted until now):

```jsonc
{ …existing (work{}, run_state, run_agent, run_progress, run_error, category,
   lane, park_state, pending_approval, attempt_count, …)…,
  "project_id": "prj_…|null",
  "chat_origin": {"chat_id": "cht_…", "title": "…"} | null,   // join chats.board_task_id
  "planner_brief": "markdown|null",
  "checklist": {"done": 3, "total": 7} | null }                // = work.steps_done/total when total>0
```

`planner_brief` provenance (nothing by this name exists in the backend — do not invent a store): when a card comes from a confirmed plan job, the confirm path records the plan payload into `org_json.planner_brief` (one new write, `routes/intake.py:1185-1361`); emission rule is `org_json.planner_brief` else `null` — the UI falls back to the card description, which already holds the refined spec + formatted acceptance criteria (`intake/service.py:298-310`).

## 3.10 `GET /api/board/{task_id}/eta` — new

```jsonc
// 200
{ "estimate_seconds": 420 | null,
  "basis": "run_steps" | "session_progress" | "discipline_history" | null,
  "sample_size": 5,
  "confidence": "low" | "medium" | "high" | null,
  "computed_at": "ISO" }
```

First qualifying basis wins; else `estimate_seconds: null`:
1. **run_steps** — linked run, ≥2 completed steps: `remaining × median(duration of last 5 completed steps)`, floor 30s. Confidence high ≥5 completed, else medium.
2. **session_progress** — active session, `steps_total > 0`, `steps_done ≥ 2`: `(elapsed / steps_done) × remaining`, floor 30s. Confidence low.
3. **discipline_history** — ≥3 completed attempts of the same discipline in 90d: `median(wall time) − elapsed`, floor 0. Confidence low; `sample_size` = n.

UI renders `—` + "Estimating…" on null; otherwise `est. ~7m` with `(n=5)` in the tooltip. Kimi's n<3 rule lives in basis 3's threshold.

## 3.11 Orchestration options — decision: **no discovery endpoint**

`Auto | Solo | Fan-out` is a fixed product enum with no per-install variance; Qwen's `GET /api/orchestration/options` would be dead configuration. The contract is the enum pinned in §3.2/§3.4 (`orch_mode: "auto"|"solo"|"fanout"`). If a pattern ever becomes conditionally available, add the endpoint then.

## 3.12 `GET /api/chats/folders` — retirement behavior

Endpoint and pinned shape survive one release. As chats map to projects the list naturally empties; the UI calls it only for the one-time migration banner.

---

# PART 4 — TEAM SPLIT

Two packages, **disjoint file ownership**, integration only through the pinned contracts below. Both teams: no `tokens.ts` edits, no new deps, no `api/main.py` edits, full ladder per `TESTING.md` before merge, `./scripts/certify-omniagentos.sh` stays green.

## TEAM-Q — Chat surface + chat backend (the conversation pipeline)

**Owns (backend):** `omniagentos/chats/bridge.py` (new) · `omniagentos/chats/classify.py` (new) · `omniagentos/chats/store.py` · `omniagentos/api/routes/chats.py` · `omniagentos/memory/runner_hook.py` · `omniagentos/sessions/supervisor.py` (close-turn hook only) · `omniagentos/sessions/chain_read.py` (new; `routes/sessions.py` refactored to use it) · `tests/chats/*`

**Owns (frontend):** `dashboard/src/features/chats/*` (ChatSurface, sidebar rewrite, composer deck, ModelPicker, PlanCard, SpawnCard, ProjectSuggestionBar, chatApi, useChats, chats.module.css, WorkspaceTabs) · `dashboard/src/features/models/useModels.ts` (new) + the surgical `useModelOptions` deletion at `hierarchyHooks.ts:291` · `dashboard/src/app/chats/page.tsx` · `dashboard/src/design/theme.css` (additive) + any primitive prop extensions in `src/design/*` (noted in report) · `dashboard/e2e/product.spec.ts` (update v0 chat assertions at :47-93 in the same PR)

**Acceptance criteria (Q):**
1. curl: create → send → **agent reply appears in `GET /api/chats/{id}/messages` without a reload**; SSE shows `started → delta(s) → completed`, deltas ≤4/s; concurrent `close_turn` double-call test yields exactly one row.
2. Bridge disabled (env flag) → reply still arrives via the 6s poll fallback; UI shows `working — no live output`.
3. ChatDTOs everywhere; sidebar groups by Projects + Recents; drag-to-project persists (verified via GET); folder UI gone; migration banner appears when legacy folders exist.
4. `PATCH {model}` changes the next send's dispatch model; `PATCH {project_id}` mirrors onto the companion card.
5. Attachment pills survive a reload (meta object round-trip both directions).
6. Classify: suggestion stored, bar renders ≥0.5, `chats.project_id` unchanged (regression test), fires exactly once per chat.
7. Plan mode: PlanCard from poll → Approve & run → confirm 201; reload mid-job re-attaches via `meta.plan_job_id`.
8. Fan-out count=3 → 3 task_ids, SpawnCard chips live.
9. `npm run lint && npx tsc --noEmit && npm run test && npm run build` green; updated chat e2e green; zero `prompt()`/`confirm()` in features.

## TEAM-K — Board, task detail, board backend (the project room)

**Owns (backend):** `omniagentos/db/migrations/086_board_project_scope.sql` (new) · `omniagentos/api/routes/intake.py` (board params/emission/exclusion change, planner_brief write on confirm, ETA endpoint) · `omniagentos/intake/service.py` (`dispatch_spec` persists `project_id`; `_enrich_board_row` emits the 4 fields; ETA helpers) · `tests/collab/test_board_project_scope.py`, `tests/collab/test_board_eta.py`

**Owns (frontend):** `dashboard/src/features/board/*` (TaskDetailDrawer, TaskOverview, PlanView, AttemptTimeline, ETA display, BoardKanban card-face: checklist bar, 💬/chat-origin badge, click→drawer; `filters.ts` untouched — it becomes correct) · `dashboard/src/features/collab/*` (types promotion, `useLiveBoard` passthrough, fixtures, collab.module.css) · `dashboard/src/app/board/page.tsx` (`?task=` + drawer mount) · `dashboard/src/app/activity/[taskId]/page.tsx` (recompose from board components) · new board e2e as a **new file** under `dashboard/e2e/` (never `product.spec.ts`)

**Acceptance criteria (K):**
1. Migration 086 applies clean on a live-DB copy; backfills populate runner-lane + companion cards; `/board?project=<id>` shows exactly that project's cards, server-filtered (today: empty); pre-086 unscoped cards produce the explicit disclosure empty state.
2. Spawned sub-tasks visible in their project; companions still hidden; `?parent_task_id=` returns children; `/board?task=` deep links open the drawer.
3. Every drawer tab renders from live data (Overview/Plan/Files/Runs/Approvals; approval decision round-trips); zero `<pre>` JSON dumps remain.
4. ETA: unit tests for all three bases + null case; UI never shows a number without a `basis`.
5. Card face: checklist bar + badges render; click → drawer; `Esc` closes; URL updates.
6. `/activity/[taskId]` renders the same components (timeline extracted, not duplicated).
7. `pytest tests/collab -q` green; dashboard lint/tsc/test/build green; board e2e green.

## Integration contract (the ONLY coupling between teams)

| # | Interface | Producer → Consumer | Pinned shape |
|---|---|---|---|
| I1 | `ChatSurface` | Q → K (drawer Chat tab) | `{chatId, variant: "page"\|"panel", boardTaskId?}` — K imports only `@/features/chats/ChatSurface` |
| I2 | ChatDTO + `/api/chats*` | Q → K | §3.1–3.6 verbatim; dock calls `POST /api/chats {board_task_id}` (409 on already-linked); `chat_origin` on the card carries the id, no chat lookup endpoint needed |
| I3 | Board card fields + params | K → Q | §3.9 verbatim: `project_id`, `chat_origin`, `planner_brief`, `checklist`, `?project_id=`, `?parent_task_id=` |
| I4 | `dispatch_spec(project_id=…)` persists | K implements → Q relies | signature unchanged; behavior added |
| I5 | Chat SSE events | Q emits → K's drawer consumes | §3.7 verbatim |
| I6 | ETA | K → Q (optional later use) | §3.10 verbatim |
| I7 | Migration ordering | K's 086 before Q's PATCH-mirror on main | Q's companion-mirror is a no-op `try/except` if the column is missing, so either merge order boots |

**Sequencing:** Q-step-0 (bridge + DTO + envelope = P0-1…P0-6) and K-step-0 (migration 086 + board emission = P0-7/P0-9) are independent — start both in parallel. The single cross-join is K mounting `ChatSurface` (I1) — schedule after Q's UI lands; until then the drawer's Chat tab shows its `EmptyState` behind the `NEXT_PUBLIC_*_FIXTURES` convention.

**Verification ladder (both teams, per `TESTING.md` + FINAL-PLAN §D):** `uv run pytest -q tests/chats tests/collab` → `cd dashboard && npm run lint && npx tsc --noEmit && npm run test && npm run build` → boot API :8485 + dashboard, curl every contract in this spec → Playwright: create → send → **streamed reply renders** → promote → card on `/board` → `?project=` scopes → drawer tabs render → `./scripts/certify-omniagentos.sh` green. Run `/archi update` after merge — this build changes architecture-level truth (new bridge module, new migration, changed board contract).
