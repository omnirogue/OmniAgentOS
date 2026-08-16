# OmniAgentOS UI/UX Improvement Plan — Kimi's draft

**Author:** Kimi (analysis grounded in full codebase exploration, 2026-07-27)
**Goal:** make the dashboard the owner's daily home again — chat-first, measurable, alive.

---

## 1. Diagnosis — why the UI feels bad today

The backend is broad and mostly *done*; the dashboard grew organically to **53 routes across 7 nav sections** and now works against the owner instead of for them:

1. **No real conversation exists anywhere.** Four overlapping "chat" surfaces (`/chats` "Chats v0", `/chat` "Conversation log", project/task conversation tabs, home `CommandComposer`) and *none* is an interactive LLM chat: messages are queued — *"agents read this on their next run"* — and **agent replies are never written back to the chat scope** (grep-confirmed: nothing appends `role="agent"` to `scope_type="chat"`; replies land on the companion board task's scope). So the UI literally cannot show a back-and-forth.
2. **The owner's two daily tools are buried.** Chat is nav item 3 under "Home"; the kanban (`/board`, the strongest surface) is one of five links under "Executions".
3. **Model choice is an afterthought.** The only picker is a **hardcoded list** of 5 entries (`useModelOptions()`, `features/projects/hierarchyHooks.ts:286`); no `/api/models` endpoint exists; the home cockpit deliberately hides model choice.
4. **Growth data exists but is scattered across ~12 pages.** Improvements, reflection, lab, tournaments, leaderboard, judges, reliability, metacog, knowledge, memory, skills, routines — each a separate route, most not cross-linked. The owner cannot answer "is my system growing?" from any one screen.
5. **Polish is uneven.** A real design system exists (`src/design`, tokens FROZEN, ~30 primitives) but key pages (`/graph`, `/orgdims`, `/improvements`) render raw `<pre>` JSON dumps and hand-rolled inline-styled tabs — they read as debug tooling. Orphan routes (`/memory`, `/artifacts`), a redirect stub in nav (`/organization`), and ⌘K covering only ~12 of 53 routes complete the picture.

**Root cause:** the IA mirrors the *backend module list*, not the owner's *workflow*.

## 2. North star

> **Chat is the front door. The Board is the project room. The Observatory is the proof it's growing.**

Design principles:
- **One conversation primitive.** Every chat — quick question, project thread, task steer — is the same object with the same UI. Kill surface duplication by consolidation, not deletion of capability.
- **Everything measurable on one screen.** The system's vital signs (skills, improvements, loops, ELO, memory, reliability) get a first-class Observatory with trends, not just tables.
- **Promote, don't switch.** Chat → kanban is a one-click *promotion* that carries context, not a re-type.
- **Stripe/Apple bar:** generous whitespace, one accent color, real typography, zero inline styles, motion only where it communicates state.

## 3. Information architecture — 53 routes → 3 primary sections

New sidebar (AppShell `NAV_SECTIONS`, `src/design/AppShell.tsx:42-175`):

| Primary | Contains (existing routes absorbed) |
|---|---|
| **Chat** `/chats` | all conversation surfaces; `/chat` (collab) becomes a *channel filter* inside |
| **Board** `/board` | kanban + matrix + portfolio; `/activity`, `/executions`, `/sessions`, `/runs` reachable from cards |
| **Observatory** `/pulse` (new) | Growth `/pulse`, Loops `/routines`, Skills `/skills`, Improvements `/improvements`, Lab `/lab`, Leaderboard `/leaderboard`, Knowledge `/knowledge`, Vault `/vault`, System `/system` |
| **Approvals** `/approvals` | + alerts bell stays global |
| **Settings** | projects, agents, accounts, files, goals, comms, revenue/cash |

Secondary routes stay routable (deep links from cards) but leave the sidebar. ⌘K palette regenerated from the full route table. Fix: remove `/organization` nav dupe, add `/memory` + `/artifacts` to Observatory.

## 4. Workstream W1 — Chat (the front door)

Target: OpenCode-grade conversation UX on the existing `chats` + `conversations` + `intake.dispatch_spec` spine.

**Backend (the four real gaps):**
1. **Reply write-back** *(critical)* — extend `omniagentos/memory/runner_hook.py:90` (`safe_persist_agent_turn`): when a session's `board_task.origin == 'chat'`, also persist the agent turn to the chat's conversation scope (join via `chats.board_task_id`). One focused change + test in `tests/chats/`.
2. **`GET /api/models`** — new read endpoint aggregating: cascade ladder (`configs/cascade.yaml`), swarm providers (`/api/swarm/providers` account health), accounts. Returns `{id, label, provider, tier, available}`. Kills the hardcoded picker.
3. **Chat folders + model + delete** — `folders` table (or `meta_json.folder` to start), `PATCH /api/chats/{id}` already accepts meta; add `model` to `CreateChatRequest` and thread through `dispatch_spec(..., model=...)` (param already exists, `intake.py:1100`). Add `DELETE /api/chats/{id}` (soft-delete).
4. **Streaming** — reuse the global SSE (`/api/events`): emit `chat.turn.started/.delta/.completed` keyed by chat id from the session hook; UI subscribes via existing `useEventChannel`.

**Frontend (one screen, `features/chats`):**
- Three-pane: **folders → conversations → thread** (v0 skeleton exists; rebuild on design primitives, drop inline styles).
- Composer: **model picker** fed by `/api/models` (default "Auto — router decides"), **attach menu** (skills from `/api/skills/tree`, documents via the companion-task upload path `POST /api/board/{task_id}/files/upload`), **sub-agent button** ("Fan out") wiring `dispatch_spec(..., swarm_planner=...)` — the intake-layer hook exists; the chats route just doesn't use it yet.
- Thread: live agent tokens via SSE, tool-call cards, per-turn model badge, "queued → running → done" state chips.
- **Promote to Board** — replace the status-stamp (`ChatStore.promote_chat`) with a real converter: dialog → creates project (or picks existing) → creates board tasks from the chat's action items (LLM-extracted) → links chat as the project's conversation. `status="promoted"` records provenance.
- Consolidate: `/chat` collab log becomes a "Channels" filter chip; project/task conversation tabs reuse the same `ChatThread` component.

## 5. Workstream W2 — Board (the project room)

Keep the 9-column kanban (best surface in the app) and add the owner's three asks on every card:
- **The plan:** card expand shows the planner brief / TaskContract (`/api/intake/board` cards already carry briefs — render, don't dump JSON).
- **Todolist:** the existing live checklist (`/activity/[taskId]` "Live checklist") hoisted onto the card face with a progress bar (n/m done).
- **Done / not done:** column semantics already exist; add per-card "remaining" line (open checklist items + blockers) so unfinished work is visible without opening the card.
- Accept **promoted chats** (W1) with a provenance badge linking back to the source conversation.

## 6. Workstream W3 — Loops (routines, one honest home)

- Keep `/routines` CRUD; add it as an Observatory section with: next-fire countdown, live run state, **acceptance-rate sparkline** per loop (data exists: `routine_runs`, `gate_evidence`, auto-pause at <50%), one-click pause/resume.
- Fold the loops tab out of `/orgdims` (link instead); loops recommended by orgdims get an "accept" action that creates the routine.

## 7. Workstream W4 — Observatory `/pulse` ("what gets measured, gets managed")

One page, six tiles + six trend charts (design chart kit already exists: LineChart/Sparkline):
1. **Skills** — total, versions this week, proposals pending (`/api/skills/tree`, `/api/updates`).
2. **Self-improvement** — applied / monitoring / rolled-back counts + latest entries (`/api/improvements`).
3. **Loops** — active loops, fires this week, mean acceptance (`/api/routines` + runs).
4. **Capability** — ELO leaderboard delta, tournaments won (`/leaderboard` data).
5. **Memory/Knowledge** — facts promoted, vault notes, metacog memory records (`/api/knowledge/stats`, `/api/metacog/memories`).
6. **Reliability** — scorecard, open events (`/api/reliability/summary`).

Each tile deep-links to its full page. Add a tiny **growth-log API** (`GET /api/pulse/series?metric=…`) that snapshots counts daily (5-line cron-side aggregator into one table) so trends are real, not computed from scratch per load.

## 8. Workstream W5 — Design-system hardening (Stripe/Apple bar)

- Ban inline `style={{}}` in feature code (lint rule); migrate `/improvements`, `/graph`, `/orgdims`, `/chats` to `design/*` primitives; replace `<pre>` JSON dumps with real components.
- Extend tokens *minimally* (tokens are FROZEN — add a `pulse` accent + chart palette via theme.css vars only), dark-mode-first, SF-style type scale already present.
- Empty states everywhere (`EmptyState` exists): a fresh install must look intentional.
- Regenerate ⌘K from routes; fix nav duplicates; add `/memory`, `/artifacts`.

## 9. Phasing

| Phase | Contents | Effort | Depends on |
|---|---|---|---|
| **0 — Foundations** | reply write-back, `/api/models`, chat folders+model+delete | 1–2 d backend | — |
| **1 — Chat** | rebuild `/chats` (folders, picker, attach, fan-out, SSE streaming, promote) | 3–4 d | P0 |
| **2 — Board** | plan/todo/done on cards, promotion intake | 1–2 d | P1 (promote) |
| **3 — Observatory** | `/pulse` page + growth-log API; loops section | 2 d | — |
| **4 — Polish** | IA rewire, design hardening, ⌘K, orphan routes | 1–2 d | P1–P3 |

Total ≈ 8–12 focused days. Every phase is shippable and independently useful.

## 10. Risks

- **Reply write-back dual-writes** — mitigate: write chat-scope turn from the same hook transactionally, keyed dedupe by session+turn.
- **SSE fan-out cost** — chat deltas ride the existing shared EventSource; throttle deltas to ~4/s per chat.
- **Scope creep** — the temptation is rebuilding everything; the plan deliberately *wires* existing backend (skills select, swarm fan-out grants, routines) rather than replacing it.
