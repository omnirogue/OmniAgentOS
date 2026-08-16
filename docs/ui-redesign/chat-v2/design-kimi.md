# Chat Screen v2 — Kimi's design

**Thesis:** today `/chats` is a mail client (list → thread). A real AI chat interface is *thread-dominant*: the conversation is the visual center, the composer is the control deck, and the sidebar recedes until needed. Every control the owner asked for — model, routing, orchestration, plan mode, projects — lives within one hand's reach of the composer.

## 1. Layout

```
┌────────────┬──────────────────────────────────┬────────────┐
│  SIDEBAR   │            THREAD                │  DRAWER    │
│ (collaps.) │   centered column, max-w 760px   │ (toggle,   │
│  Projects  │   ── scrollback, day dividers ── │  hidden    │
│  Folders   │   ┌──────────────────────────┐   │  by def.)  │
│  Inbox     │   │ COMPOSER (floating card) │   │  activity/ │
│            │   │ [model][mode][orch][📎]  │   │  terminal/ │
│            │   └──────────────────────────┘   │  files     │
└────────────┴──────────────────────────────────┴────────────┘
```

- **Sidebar** (`ChatSidebar.tsx` rework): sections = **Projects** (real tree from `/api/projects/tree`, chats nested under each), **Folders** (existing), **Inbox** (unfiled). Collapses to icon rail (⌘[). `+ Project` button → create dialog (`POST /api/projects`). Chats draggable onto projects AND folders; right-click menu mirrors every drag action for keyboard/overflow users.
- **Thread** (`ChatThread.tsx` restyle): centered column, day dividers, avatars dropped (calmer), agent turns subtly tinted, tool-call cards collapsed to one-liners that expand. Instant open from cache; SSE deltas only.
- **Composer** (`ChatComposer.tsx` rebuild as the control deck), one floating card, two rows:
  - Row 1: textarea (auto-grow, Enter send / ⇧Enter newline), 📎 attach (skills via `@`, files), send button with state (queued→running→done).
  - Row 2 (control strip): **Model picker** (⌘J, from `/api/models`, remembers per chat), **Mode** segmented control `Regular | Plan` (⌘.), **Orchestration** `Auto | Solo | Fan out` (default Auto = router decides; Fan out = swarm_planner), overflow menu (rename, move, delete, routing preferences).
- **Routing preferences dialog** ("tell the model what models to use"): free-text hint + preferred-model multi-select, stored in chat `meta.routing_hint` / `meta.preferred_model`, injected into dispatch via `pins` + `model`.

## 2. Projects & auto-classify

- Sidebar projects = real `/api/projects/tree` data (hierarchyHooks already fetches it). Drag a chat onto a project → `PATCH /api/chats/{id}` **`project_id`** *(backend addition #1: accept `project_id` in the PATCH body — column exists, one-line validator + test)*.
- **Auto-classify** *(backend addition #2)*: `POST /api/chats/{id}/classify` → `{suggested_project_id, confidence, reason}` using the existing orgdims/metacog classifier on the first user message. UI: a quiet suggestion chip appears on the sidebar chat row — `Move to "AgentProAcademy"? ✓ ✗` — never auto-moves on first sight; after 3 accepted suggestions in a row, a settings toggle offers full-auto.

## 3. Kanban chat dock

- `/board` gains a right **chat dock** (collapsible, ⌘]): selecting any card loads its conversation (`/api/tasks/{id}/conversation`) into the SAME `ChatThread` + composer control strip (model picker + **Plan | Regular** toggle). Sending in Regular = steer/queue to the task's session (`POST /api/sessions/{id}/message` when live, hierarchy message otherwise); Plan mode = dispatch plan-first (reuse the cockpit's plan-first path — `CommandComposer` already exposes it).
- "Promoted from chat" cards deep-link back: dock opens scrolled to the promotion point.
- Dock is an overlay drawer under 1280px, persistent panel above.

## 4. Task detail — the "everything" panel (Qwen-3 builds; contract defined here)

Upgrade `/activity/[taskId]` (or full-screen dialog from the card) with sections, all from existing endpoints:
- **Header**: title, state chip, discipline, ETA *(backend addition #3: `GET /api/tasks/{id}/eta` → median wall-time of completed runs in same discipline over 90d, `{estimate_seconds, sample_size, confidence}`; render "est. ~25m (n=12)" or "—" honestly)*.
- **ProgressRail**: phase stepper + checklist n/m bar (exists) + run state.
- **AgentsRow**: chips of every agent/session that touched the task (sessions table + swarm attempts), each linking to its transcript.
- **Plan**: planner brief / TaskContract rendered as components.
- **Acceptance criteria**: from `tasks.acceptance_json` — criterion list with verify state (G5 evidence where present).
- **Tabs**: Chat log (task conversation), Timeline (events), Files, Approvals, Runs.

## 5. Backend additions (the only three)

1. `PATCH /api/chats/{id}` accepts `project_id` (+ tests).
2. `POST /api/chats/{id}/classify` → suggestion (read-only; never mutates).
3. `GET /api/tasks/{id}/eta` → `{estimate_seconds, sample_size, confidence}`.
Plus documented chat-meta keys: `preferred_model`, `routing_hint`, `orch_mode` (`auto|solo|swarm`), `plan_mode` (bool) — all already storable via `meta_json`; the message-send path threads them into `dispatch_spec(model=, pins=, swarm_planner=)`.

## 6. State & speed

- Hooks stay (`useChats` pattern): list cache → thread opens instantly; optimistic send with rollback on error; one shared SSE channel (`useEventChannel`) filtered by `chat_id`; composer state per chat id in a module map so switching chats never loses a draft.

## 7. Keyboard map

⌘N new chat · ⌘⇧N new project · ⌘J model picker · ⌘. plan/regular · ⌘[ sidebar · ⌘] drawer/dock · Esc close dialog/drawer · Enter send · ⇧Enter newline.

## 8. Top risks

1. **SSE fan-out** with many open chats — one shared channel, client-side filter, throttle deltas (already pinned ≤4/s).
2. **DnD accessibility** — every drag has a menu equivalent; HTML5 DnD only, no new deps.
3. **Classifier noise** — suggestion-first, never destructive; full-auto only after 3 accepts.
4. **Mode confusion** (plan vs swarm) — one segmented Mode control; orchestration tucked under it.
5. **ETA honesty** — show sample size; render "—" below n=3 rather than a fake number.
