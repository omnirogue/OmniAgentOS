# Shared design brief — Chat Screen v2 + Kanban Chat (all designers read this)

You are designing the NEXT version of the chat experience for OmniAgentOS — a local-first control plane for governed AI agent workflows (FastAPI backend `omniagentos/` port 8485, Next.js 15 dashboard `dashboard/` port 3002). Read `docs/ui-redesign/FINAL-PLAN.md` sections A (Design Charter) and B (pinned contracts) first — they are binding. Explore the current implementation: `dashboard/src/app/chats/page.tsx`, `dashboard/src/features/chats/*` (ChatSidebar, ChatHeader, ChatComposer, ChatThread, chatApi, useChats), `dashboard/src/features/board/*`, `omniagentos/api/routes/chats.py`, `omniagentos/chats/store.py`.

## What exists today (just built)

- `/chats`: 2-pane layout — sidebar (folders, Inbox, drag-to-folder, ⌘N) + thread; model picker fed by `/api/models`; `@skill` mentions; doc upload; `/spawn`; Fan out; Promote-to-Board; SSE `chat.turn.*` streaming; workspace drawer.
- `/board`: 9-column kanban, project scoping, plan toggle, checklist progress, remaining line.
- Backend: `/api/chats` CRUD + messages + folders + spawn + promote; `/api/models` catalog; `/api/projects` + `/api/projects/tree`; `dispatch_spec` accepts `model`, `speed`, `pins`, `swarm_planner`; orgdims/metacog classifiers wired on spawn.

## Owner's new requirements (verbatim intent)

1. **Chat front and center like any normal AI chat interface** (ChatGPT/Claude/OpenCode-grade): the thread IS the product — centered, calm, fast; chrome recedes.
2. **Model + orchestration control**: choose the model per chat/message; "tell the model what models we want to use" (steerable routing preferences); choose the orchestration pattern (solo agent / plan-first / swarm fan-out).
3. **Projects are first-class in the chat UI**: create projects from the sidebar; DRAG chats into a project; the real projects from `/api/projects/tree` populate the sidebar; **auto-classify** new chats into a suggested project (use the existing classifier; user confirms or overrides).
4. **The same chat screen lives on the Kanban** — a chat panel with the SAME model picker and a **Plan mode vs Regular mode** toggle. Bigger orchestrations flow to the board.
5. **Kanban task detail must show EVERYTHING**: progress visualization (how far along), agents involved, the plan, acceptance criteria, ETA, chat logs/transcript, files, run history, approvals — the backend already tracks most of this; surface it.

## Design rules

- Stripe/Apple bar: design primitives only, zero inline styles, CSS Modules + `var(--ds-*)` tokens, intentional empty/error states, keyboard-first (⌘N new chat, ⌘K palette, ⌘J model picker?, Enter send), dark-mode-safe, subtle 120–200ms motion, skeletons not spinners.
- Wire, don't rebuild: prefer existing endpoints; where a small backend addition is needed (e.g. `PATCH /api/chats/{id}` accepting `project_id`, plan-mode flag, routing-preferences meta), specify it precisely.
- Speed: the thread must open instantly from cache and update via SSE; no full-page fetches per message.

## Your deliverable

A design document: information architecture, component breakdown (exact files, new vs modified), state management, backend additions (precise request/response), keyboard map, empty/loading/error states, and the 5 highest-risk details. Be opinionated and concrete — this will be built from your doc.
