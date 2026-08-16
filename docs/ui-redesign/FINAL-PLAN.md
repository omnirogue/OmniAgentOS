# VERDICT

**Kimi's plan (A) wins.** Its decisive edge is groundedness on the *blocking* gap: I verified in code that `safe_persist_agent_turn` (`omniagentos/memory/runner_hook.py:90`) writes agent replies **only** to `scope_type="task"` — so no chat UI can show a back-and-forth today. Qwen never mentions this, and its claim that "the OpenCode backend already exists, it just has no frontend" is therefore materially wrong: without reply write-back, Phase 1 of Plan B ships a chat where the agent never answers. Kimi is also right that `useModelOptions()` is a hardcoded 5-entry list (`hierarchyHooks.ts:286`, confirmed), that no `/api/models` or `DELETE /api/chats/{id}` exists (confirmed against `routes/chats.py`), and that `dispatch_spec` already accepts `model=` and `swarm_planner=` (confirmed at `intake/service.py:1089`). Kimi's IA consolidation (53 routes → 3 primary sections) and Observatory concept serve owner goal #5 far better than Qwen's cockpit widgets alone.

**But Qwen is better at execution granularity** — per-file change tables with effort estimates, the project-scoped board filter, the cross-routine runs aggregate, the "Since you were gone" delta, and the "ship promote-to-kanban first" instinct. The merged plan below keeps Kimi's diagnosis, architecture, and sequencing, and steals Qwen's file-level precision, board/loops upgrades, and cockpit pulse.

---

# OmniAgentOS Dashboard — Merged Master Plan

**Thesis:** the backend is ~85% built; the dashboard mirrors the module list instead of the owner's workflow. The fix is one conversation primitive at the front door, the kanban as the project room, and an Observatory that proves the system is growing. When done, this is the console of a self-improving autonomous agent OS: you talk to it, it works, you watch it get better — all from one screen.

## 0. Verified ground truth (what exists vs. what's missing)

**Exists and works:**
- `ChatStore` (`omniagentos/chats/store.py`): chats with `project_id`, `board_task_id` (auto-provisioned companion card, line 77), `status`, `promoted_at`, `meta_json`, and `promote_chat()` (line 188).
- `omniagentos/api/routes/chats.py`: `POST /api/chats`, `GET /api/chats`, `GET/PATCH /api/chats/{id}`, `POST/GET /api/chats/{id}/messages`; message-send already calls `dispatch_spec` (line 153).
- `dispatch_spec` (`omniagentos/intake/service.py:1089`) accepts `model`, `speed`, `pins`, `swarm_planner`, `board_task_id` — the spawn/model plumbing exists at the intake layer.
- `/api/routines` full CRUD + per-routine `/runs`; `/api/improvements`, `/api/reflection`, `/api/system/{map,agents,skills,improvers,agent-activity}`, `/api/skills/tree`, reliability, knowledge, metacog.
- `BoardKanban.tsx` (9-column board, swarm overlay, needs-response queue), `CommandComposer`/`TaskPulse` cockpit, design system (`dashboard/src/design/*`, ~30 primitives, frozen tokens), global SSE (`/api/events` + `useEventChannel`).

**Missing (all verified absent):**
1. **Agent reply write-back to chat scope** — `runner_hook.py` persists agent turns to `scope_type="task"` only. *This blocks everything chat.*
2. **`/api/models`** — no route file; the picker is hardcoded (`hierarchyHooks.ts:286-298`).
3. **Chat folders, per-chat model, delete** — no folder entity, no `DELETE /api/chats/{id}`, `model` not on `CreateChatRequest`.
4. **Chat streaming events** — no `chat.turn.*` events on the SSE bus.
5. **Cross-routine runs aggregate** — `/api/routines/{id}/runs` exists per-routine only.
6. **Growth time-series** — counts are computable but no daily snapshot, so no real trends.
7. **`GET /api/system/delta?since=`** — no "what changed while I was gone" aggregate.

## 1. North star & principles

> **Chat is the front door. The Board is the project room. The Observatory is the proof it's growing.**

- **One conversation primitive.** Quick question, project thread, task steer, promoted chat — same object, same `ChatThread` component, everywhere.
- **Promote, don't re-type.** Chat → kanban is a one-click promotion that carries the plan, files, and provenance.
- **Everything measurable, one screen.** Skills, improvements, loops, ELO, memory, reliability — tiles with real trends, each deep-linking to its full page. What gets measured, gets managed.
- **Wire, don't rebuild.** The plan connects existing machinery (`dispatch_spec`, skills tree, routine runs, SSE) rather than replacing it.
- **Stripe/Apple polish bar.** Design primitives only, zero inline styles in feature code, intentional empty states, motion only where it communicates state.

## 2. Information architecture — 53 routes → 5 sidebar entries

Rewire `NAV_SECTIONS` in `dashboard/src/design/AppShell.tsx`:

| Primary | Absorbs |
|---|---|
| **Chat** `/chats` | standalone + project/task threads; `/chat` collab log becomes a "System channels" filter chip inside (page stays at its URL, leaves the nav) |
| **Board** `/board` | kanban + matrix + portfolio; `/activity`, `/executions`, `/sessions`, `/runs` reachable from cards |
| **Loops** `/routines` | top-level, renamed "Loops" in nav (URL unchanged) |
| **Observatory** `/pulse` (new) | Skills, Improvements, Lab, Leaderboard, Judges, Tournaments, Knowledge, Vault, Memory, Metacog, Reliability, System, Artifacts |
| **Approvals** `/approvals` | + global alerts bell |

Settings (projects, agents, accounts, files, goals, comms, revenue) collapses into a gear menu. All 53 routes stay routable via deep links; ⌘K palette regenerated from the full route table; remove the `/organization` nav dupe; add `/memory` and `/artifacts` to Observatory.

## 3. Chat — the front door (owner goals 1 & 2)

### 3.1 Backend

1. **Reply write-back** *(the critical unlock)* — extend `omniagentos/memory/runner_hook.py`: when the session's board task originated from a chat (join `chats.board_task_id`), `safe_persist_agent_turn` also appends the turn to `scope_type="chat", scope_id=<chat_id>` in the same transaction, deduped by `(session_id, turn_index)`. Tests in `tests/chats/test_reply_writeback.py`.
2. **`GET /api/models`** — new `omniagentos/api/routes/models.py` aggregating the cascade ladder (`configs/cascade.yaml`), swarm provider health (`/api/swarm/providers`), and accounts. Returns `[{id, label, provider, tier, available, lineage}]` plus a first-class `{"id": "auto", "label": "Auto — router decides"}` default. Kills the hardcoded picker for every composer in the app.
3. **Chats CRUD completion** (`omniagentos/chats/store.py` + `routes/chats.py`):
   - Folders via `meta_json.folder` to start (no migration): `GET /api/chats/folders` (distinct values), `folder` query param on `GET /api/chats`, rename = bulk PATCH.
   - `model` on `CreateChatRequest` and per-message; store as `meta.preferred_model`; thread through the existing `dispatch_spec(..., model=...)` call at `chats.py:153`.
   - `DELETE /api/chats/{id}` (soft delete, `status="deleted"`).
   - `POST /api/chats/{id}/spawn` — fan out sub-agents on the existing thread context via `dispatch_spec(..., swarm_planner=...)`; spawned tasks are **children of the chat's companion task**, not new top-level cards (keeps the board clean — Qwen's nesting insight).
   - Attachment manifest schema documented on message `meta`: `{attachments: [{kind: 'skill'|'file'|'url', ref, label}]}` — `ConversationStore.append` already accepts `meta`, zero storage change.
4. **Streaming** — emit `chat.turn.started` / `.delta` / `.completed` keyed by chat id on the existing `/api/events` SSE bus from the session hook; throttle deltas to ~4/s per chat.

### 3.2 Frontend (`dashboard/src/app/chats/` + `features/chats/`)

Rewrite `/chats` as a 2-pane layout (Qwen is right — 3-pane is why v0 feels cramped): **sidebar (folders + recent + all) → thread**, with the plan/todolist/files `WorkspaceTabs` as a collapsible right drawer, not a permanent pane.

- **`ChatSidebar.tsx`** (new): folder tree, Inbox for folderless chats, `Cmd+N` new chat, drag-to-folder (PATCH `meta.folder`), rename/delete.
- **`ChatHeader.tsx`** (new): editable title, prominent **model picker** fed by `/api/models`, **Fan out** (spawn) button, **Promote to Board** button, archive/delete.
- **`ChatComposer.tsx`**: `@skill` mention autocomplete from `GET /api/skills/tree`, `📎` document picker reusing the token-gated companion-task upload path (`POST /api/board/{task_id}/files/upload`), `/spawn` slash command, per-message model override (Shift+click picker).
- **`ChatThread.tsx`**: scope-aware (`chat | project | task`); live tokens via `useEventChannel` on `chat.turn.*`; tool-call cards; per-turn model badge; `queued → running → done` state chips.
- **Promote to Board** — upgrade `promote_chat` from a status stamp to a real converter: dialog picks/creates a project, LLM-extracts action items from the thread into board tasks under the companion card, links the chat as the project conversation, sets `status="promoted"` for provenance. The board card carries a "from chat" badge linking back.
- Project/task conversation tabs everywhere else in the app reuse this same `ChatThread` — one primitive, no more surface drift.

**Ship-first slice (Qwen's single PR, kept):** reply write-back + standalone chat creation + Promote button. That alone turns `/chats` from a dead letterbox into a working front door within days.

## 4. Board — the project room (owner goal 3)

The 9-column kanban stays; every card gains the owner's three asks:

- **The plan:** card expand renders the planner brief / TaskContract (already on `/api/intake/board` payloads) as real components, never `<pre>` JSON.
- **Todolist:** the live checklist from `/activity/[taskId]` hoisted onto the card face — progress bar (n/m done) + `SubtaskList` disclosure per swarm card (reuse the component from `TaskPulse.tsx`).
- **Done / not done:** per-card "remaining" line (open checklist items + blockers) visible without opening the card.
- **Project scope:** `?project=<id>` filter (the route already supports `?run=`) — toolbar Select from `useProjectTree`, threaded through `useLiveBoard` → `collabApi.liveBoard` → `project_id` param on `GET /api/board` in `omniagentos/api/routes/collab.py`. Opening one project shows its promoted chats, companion tasks, and spawned sub-tasks — the whole plan in one view.
- **Provenance:** promoted-chat badge linking back to the source conversation.

## 5. Loops — top-level and honest (owner goal 4)

- "Loops" becomes a top-level nav entry (URL stays `/routines`).
- **Aggregate endpoint:** `RoutinesStore.list_recent_runs(limit)` in `omniagentos/scheduler/store.py` (join `routines` × `routine_runs`) exposed as `GET /api/routines/runs?limit=50`.
- Routines page gains: **RecentRunsPanel** (routine, run id, gate passed, accepted, cost, finished_at — a timeline of what the system did on its own), next-fire countdown, live run state, **per-loop acceptance-rate sparkline** (data exists: `routine_runs` + `gate_evidence`, auto-pause <50%), one-click pause/resume.
- Loops recommended by `/orgdims` get an "accept" action that creates the routine; the orgdims loops tab becomes a link.

## 6. Observatory `/pulse` — watch it grow (owner goal 5)

One new page, six tiles + trend charts (design chart kit exists: LineChart/Sparkline), each deep-linking to its full page:

1. **Skills** — total, versions this week, proposals pending (`/api/skills/tree`, `/api/updates`)
2. **Self-improvement** — applied / monitoring / rolled-back + latest entries, applied-per-day sparkline (`/api/improvements`)
3. **Loops** — active, fires this week, mean acceptance (`/api/routines` + new runs aggregate)
4. **Capability** — ELO delta, tournaments won (leaderboard data)
5. **Memory/Knowledge** — facts promoted, vault notes, metacog records (`/api/knowledge/stats`, `/api/metacog/memories`)
6. **Reliability** — scorecard, open events (`/api/reliability/summary`)

**Trends are real, not recomputed:** new `omniagentos/pulse/` module with a daily snapshot aggregator (rides the existing scheduler as a routine) writing one `pulse_series` table; `GET /api/pulse/series?metric=…` serves the charts. This is the "watch it grow" backbone — a self-improving system that graphs its own improvement.

## 7. Cockpit pulse — "since you were gone"

The home `/` cockpit keeps `CommandComposer` + `TaskPulse` and gains:

- **`LoopsPulse.tsx`** — 3–5 active routines × last-run status, next fire.
- **`GrowthPulse.tsx`** — skills Δ, improvements applied/reverted, routine runs, spend (telemetry only).
- **`SinceYouWereGone.tsx`** — `last_seen` in localStorage → `GET /api/system/delta?since=ISO` (new, ~50 lines in `routes/system.py`, capped at 30 days server-side): skills updated, improvements decided, loops run, tasks completed since — each a count + deep link. This is the hook that pulls the owner back from the terminal.

## 8. Design-system hardening

- Lint rule banning inline `style={{}}` in feature code; migrate `/improvements`, `/graph`, `/orgdims`, `/chats` to `design/*` primitives; replace every `<pre>` JSON dump with real components.
- Tokens stay FROZEN — add only a `pulse` accent + chart palette via theme.css vars.
- `EmptyState` everywhere: a fresh install must look intentional.
- ⌘K regenerated from the route table (all 53, not 12); breadcrumbs updated for the new IA.

## 9. Phasing (each phase shippable alone)

| Phase | Contents | Effort | Depends on |
|---|---|---|---|
| **0 — Unlock** | reply write-back, `/api/models`, chats CRUD completion (folders/model/delete/spawn), SSE chat events | 2 d backend | — |
| **1 — Chat** | `/chats` rewrite: sidebar, header, composer, streaming thread, promote converter | 3–4 d | P0 |
| **2 — Board** | plan/todo/remaining on cards, `?project=` scope, subtask disclosure, provenance badges | 1.5–2 d | P1 (promote only) |
| **3 — Loops + Observatory** | runs aggregate, RecentRunsPanel, `/pulse` page, snapshot series, cockpit pulses + delta | 3 d | — (parallel with P1–P2) |
| **4 — IA + Polish** | nav rewire, ⌘K, `/chat` demotion, design hardening, orphan routes | 1.5–2 d | P1–P3 |

Total ≈ 11–13 focused days serial — but see Implementation Slices: most of it parallelizes to ~4–5 calendar days.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Dual-write on reply write-back diverges | Same transaction in the hook; dedupe key `(session_id, turn_index)`; test both scopes |
| SSE delta fan-out cost | Ride the existing shared EventSource; throttle ~4 deltas/s/chat |
| Spawn spam floods the board | Spawned agents nest under the chat's companion task, never top-level |
| `/chat` demotion breaks agent↔agent handoffs | Page and API untouched; only nav prominence changes |
| Arbitrary file uploads | Reuse the token-gated board-files path; no new upload surface |
| Model pickers drift | `/api/models` is the single source; `useModelOptions` becomes a thin fetch hook, all composers consume it |
| `system/delta` gets expensive | 30-day server cap; longer ranges deep-link to full pages |
| Scope creep | Every workstream *wires* existing backend; nothing is rebuilt that already works |

---

# Implementation slices (parallel-safe workstreams)

Seven slices, disjoint file ownership. The only shared-file exception is router registration (one `include_router` line each in the API app module) — resolved by an append-only convention, one line per slice, no logic.

### S1 — Chat backend core
- **Owns:** `omniagentos/chats/`, `omniagentos/api/routes/chats.py`, `omniagentos/memory/runner_hook.py`, `tests/chats/`
- **Depends on:** nothing.
- **Delivers:** reply write-back to chat scope; folders (`meta_json.folder` + `GET /api/chats/folders` + `folder` filter); `model` on create/message threaded to `dispatch_spec`; `DELETE /api/chats/{id}`; `POST /api/chats/{id}/spawn` (children of companion task); `chat.turn.*` SSE emission; promote converter endpoint (project pick/create + action-item task extraction).
- **Accept:** `pytest tests/chats/` green; curl script: create chat → send message → agent turn appears in `GET /api/chats/{id}/messages` with `role="agent"`; spawn creates nested tasks; promote returns created task ids.

### S2 — Models API
- **Owns:** new `omniagentos/api/routes/models.py`, new `omniagentos/modelintel/catalog.py` (or equivalent aggregator), `tests/models_api/`
- **Depends on:** nothing (reads `configs/cascade.yaml` + swarm provider store read-only).
- **Accept:** `GET /api/models` returns ≥ the cascade ladder entries with `{id, label, provider, tier, available}` and an `auto` default; unavailable providers flagged `available:false`; tests cover empty/partial config.

### S3 — Chat frontend
- **Owns:** `dashboard/src/app/chats/`, `dashboard/src/features/chats/` (rewrite + new `ChatSidebar.tsx`, `ChatHeader.tsx`, `skillMention.ts`)
- **Depends on:** S1 + S2 contracts (can build against stubbed fixtures; integrates last).
- **Accept:** create standalone chat via `Cmd+N`; drag to folder; pick non-default model; `@skill` attach; `📎` upload; send → streamed agent reply renders; Fan out spawns; Promote opens dialog and lands cards on `/board`; zero inline styles; empty state present.

### S4 — Board upgrades
- **Owns:** `dashboard/src/app/board/`, `dashboard/src/features/board/`, `dashboard/src/features/collab/` (hooks + client), `omniagentos/api/routes/collab.py`, `tests/collab/`
- **Depends on:** none for filter/disclosure; consumes S1's provenance field read-only (renders badge only if present).
- **Accept:** `/board?project=<id>` shows only that project's cards; cards show checklist progress bar + remaining line; swarm cards disclose subtasks; promoted cards show a source-chat badge when the field exists.

### S5 — Loops
- **Owns:** `dashboard/src/app/routines/`, `dashboard/src/features/routines/` (+ new `RecentRunsPanel.tsx`), `omniagentos/scheduler/store.py`, `omniagentos/api/routes/routines.py`, `tests/routines/`
- **Depends on:** nothing.
- **Accept:** `GET /api/routines/runs?limit=50` returns joined recent runs; routines page shows the panel, next-fire countdown, acceptance sparklines, pause/resume; tests cover the aggregate query.

### S6 — Observatory + cockpit pulse
- **Owns:** new `dashboard/src/app/pulse/`, new `dashboard/src/features/pulse/`, new `dashboard/src/features/cockpit/{GrowthPulse,LoopsPulse,SinceYouWereGone}.tsx`, `dashboard/src/app/page.tsx` (cockpit additions only), new `omniagentos/pulse/`, new `omniagentos/api/routes/pulse.py`, `omniagentos/api/routes/system.py` (delta endpoint), `tests/pulse/`
- **Depends on:** S5's runs aggregate for the Loops tile (degrade gracefully to per-routine data until it lands).
- **Accept:** `/pulse` renders six tiles from live endpoints; daily snapshot routine writes `pulse_series` and `GET /api/pulse/series` returns it; cockpit shows the three pulses; `GET /api/system/delta?since=` returns correct counts against seeded fixtures.

### S7 — Shell, IA, and polish
- **Owns:** `dashboard/src/design/AppShell.tsx`, command-palette route table, breadcrumb map, `dashboard/src/app/chat/page.tsx` (demotion styling only), lint config (inline-style ban), `/improvements` `/graph` `/orgdims` primitive migration
- **Depends on:** merges last (needs S3/S6 routes to exist for nav targets; pure integration, no API deps).
- **Accept:** sidebar shows exactly Chat / Board / Loops / Observatory / Approvals + settings gear; no duplicate or dead nav links; ⌘K reaches all routes; `npm run lint` fails on inline styles in feature code; no `<pre>` JSON dumps remain on migrated pages.

**Merge order:** S1, S2, S5 land first (pure backend, no conflicts) → S3, S4, S6 integrate against them → S7 closes. Validation per TESTING.md's ladder at every merge.

---

# Kimi final pass — execution charter (this section governs all implementers)

## A. Design Charter — "Stripe/Apple bar" (binding for every slice)

The visual target: **Stripe's information density with Apple's calm**. Sleek, fast, obvious.

1. **Primitives only.** Every screen is composed from `dashboard/src/design/*` (Button, Card, Badge, Table, Tabs, Dialog, Toast, Select, Input, Stat, EmptyState, ErrorState, charts). Never hand-roll a tab bar, modal, menu, or tooltip. If a primitive is missing a prop you need, extend the primitive in `src/design/` (note it in your summary), do not fork it locally.
2. **Zero inline styles in feature code.** Layout via per-feature CSS Modules (`features/**/*.module.css`) referencing token vars (`var(--ds-*)` from `theme.css`). `tokens.ts` is FROZEN — do not edit it; new values go through `theme.css` custom properties only.
3. **One accent.** Brand accent for primary actions + live states; everything else neutral. No rainbow badges — semantic colors only (success/warn/danger/info as defined in tokens).
4. **Typography does the work.** Hierarchy through the token type scale — page title, section title, body, caption. No new font sizes, no `fontSize` overrides.
5. **Density & whitespace.** 4px spacing grid. Cards breathe (generous padding, 1px hairline borders, small radii — the Stripe look). No gradients, no glassmorphism, no drop-shadow stacks.
6. **Motion.** Subtle 120–200ms ease-out transitions on hover/disclosure only. Loading = skeleton or `EmptyState`-style placeholder, never spinners alone on a blank page.
7. **Every screen has an intentional empty state** (`EmptyState`) and error state (`ErrorState`). A fresh install must look designed, not broken.
8. **Speed.** List pages virtualize or paginate >100 rows; SSE-driven updates (existing `useEventChannel`) over polling where an event exists; no layout shift when data lands (reserve space).
9. **Keyboard-first.** `Cmd+N` new chat, `Cmd+K` palette, `Esc` closes drawers/dialogs, Enter sends (Shift+Enter newline). All actions reachable by keyboard.

## B. Pinned contracts (do not deviate — slices integrate against these)

**`GET /api/models`** → `{ "models": [{ "id": string, "label": string, "provider": string, "tier": number|null, "available": boolean, "lineage": string|null }] }` — first entry always `{ "id": "auto", "label": "Auto — router decides", "provider": "router", "tier": null, "available": true, "lineage": null }`.

**Chat SSE events** on `/api/events`: `{"type": "chat.turn.started"|"chat.turn.delta"|"chat.turn.completed", "chat_id": string, "task_id": string, "turn": int, "text"?: string, "model"?: string, "ts": ISO}` — deltas throttled to ≤4/s/chat.

**`POST /api/chats/{id}/spawn`** → body `{"goal": string, "count"?: int}` → `{"task_ids": [string]}` (children of the chat's companion task).

**`POST /api/chats/{id}/promote`** → body `{"project_id": string|null, "new_project_title"?: string}` → `{"project_id": string, "task_ids": [string]}`.

**Message attachment manifest** (message `meta_json`): `{"attachments": [{"kind": "skill"|"file"|"url", "ref": string, "label": string}]}`.

**`GET /api/routines/runs?limit=50`** → `{"runs": [{"routine_id", "routine_name", "run_id", "gate_passed": bool|null, "accepted": bool|null, "cost_usd": number|null, "finished_at": ISO}]}`.

**`GET /api/pulse/series?metric=<name>&days=30`** → `{"metric": string, "points": [{"date": "YYYY-MM-DD", "value": number}]}`; metric names: `skills.total`, `skills.versions`, `improvements.applied`, `loops.fires`, `loops.acceptance`, `memory.facts`, `reliability.score`.

**`GET /api/system/delta?since=ISO`** → `{"since": ISO, "skills_updated": int, "improvements_decided": int, "loops_run": int, "tasks_completed": int, "chats_active": int}`.

**`GET /api/chats/folders`** → `{"folders": [string]}`; `GET /api/chats?folder=<name>` filters; folder stored at `chats.meta_json.folder`.

## C. Swarm coordination rules (binding)

1. **Edit only files your slice owns.** Read anything. Shared files are owned as follows: `omniagentos/api/main.py` — **Kimi only** (I register routers after all slices land; S2/S6 must NOT edit it). `dashboard/src/lib/*`, `dashboard/src/design/index.ts` — read-only for all slices except S7. `dashboard/src/design/theme.css` — S7 only.
2. **No git mutations** (no commit/branch/push/reset). **No dependency installs.** Use only packages already in `package.json` / `pyproject.toml`.
3. **DB schema:** no new migrations except S6 (`pulse_series`, next free migration number 083 — verify against `omniagentos/db/migrations/` before writing). Everyone else: use `meta_json`.
4. **Match the file's existing style** — comment density, naming, import order. Python: type hints, no new deps. TypeScript: strict, no `any` unless the file already uses it.
5. **Complete code, no stubs, no TODOs.** If an API your frontend needs isn't live yet, build against the pinned contract in section B with a labeled fixture fallback (existing convention: `NEXT_PUBLIC_*_FIXTURES` flags).
6. **Tests:** backend slices add pytest tests in their owned `tests/` dir; frontend slices add vitest unit tests for new logic. Follow `TESTING.md`.
7. Since you run with `--approval-mode auto-edit`, you **cannot run shell commands** — write code and tests, but do not attempt to execute them. Kimi runs the full verification ladder afterward.
8. Update `docs/ui-redesign/SLICE-REPORT-<id>.md` with: what you built, files changed, contracts consumed/emitted, anything left for integration.

## D. Verification ladder (Kimi runs after all slices land)

1. `pytest tests/chats tests/routines tests/pulse tests/collab -q` (backend slices)
2. `cd dashboard && npm run lint && npx tsc --noEmit && npm run test` then `npm run build`
3. Register routers in `api/main.py` (S2 models, S6 pulse), boot API on :8485, curl every pinned contract in section B
4. Boot dashboard on :3001, Playwright smoke: create chat → send → reply renders; promote → card on `/board`; `/pulse` tiles render; nav shows 5 entries
5. `./scripts/certify-omniagentos.sh` must stay green

## E. Corrections & sharpenings on the merged plan

1. **e2e debt:** `dashboard/e2e/product.spec.ts` asserts current `/chats` v0 behavior ("queued" chip, 3-pane). S3 must update these specs to the new UX or they go red — explicitly in S3's scope.
2. **Home cockpit ownership:** `app/page.tsx` is S6's, but S7 rewires nav that links to it — S7 must not edit `page.tsx`; link targets only.
3. **Migration number:** merged plan didn't pin one — pinned here at 083 for `pulse_series` (060–069 Grok-exclusive ledger is claimed; verify next free in `omniagentos/db/migrations/`).
4. **`useModelOptions()` deletion:** after S2 + S3 land, the hardcoded hook at `features/projects/hierarchyHooks.ts:286` must become a thin `/api/models` fetch — assigned to S3 (it owns the composer) with S7 verifying no stragglers via grep.
5. **Promote extraction:** S1's promote converter should use the existing short-call LLM client (`omniagentos/llm/client.py`, budgeted) for action-item extraction, not a new provider path.
6. **Speed budget:** every new page must stay under the dashboard's existing bundle discipline — no new runtime deps whatsoever (framer-motion, radix, etc. are NOT installed; do not add them).

**`GET /api/connections`** (S8) → `{"categories": [{"id": string, "label": string, "integrations": [{"id": string, "name": string, "logo": string, "status": "connected"|"configured"|"not_configured"|"error", "instances": [{"label": string, "status": string}], "detail": string, "docs_url": string|null}]}]}` — status derives ONLY from presence of vault keys / connector store rows / poller state. **Secret values are never read into responses — presence booleans and masked counts only.**
