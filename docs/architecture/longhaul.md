# Longhaul — long-horizon agentic coding lane

Deployed 2026-07-23, live-verified end-to-end same day. Full design + review history:
`docs/architecture/LONGHAUL-DESIGN.md` (v1.2). This page is the compact operating map.

## Model

The **board task is the durable unit**; sessions are an ordered chain of executor
attempts (`task_sessions`, one live attempt max — partial unique index). Continuity
across attempts is the **workbook** (`var/longhaul/<task_id>/WORKBOOK.md`: goal,
acceptance, plan, progress, decisions, `## Status: WORKING|BLOCKED|DONE`) plus a
supervisor checkpoint of the session's final todos/files on every terminal — never
transcript replay. Same-account interrupts may use native `claude -p --resume`.

## Components

| Piece | Where |
|---|---|
| Engine (state machine: dispatch / on_session_terminal / tick) | `omniagentos/longhaul/engine.py` |
| Store DAL (categories, slots, attempts, cooldowns, task turns) | `omniagentos/longhaul/store.py` |
| Limit classifier (structured-first) + reset-time parser | `omniagentos/longhaul/limits.py` |
| Workbook, prompts, mechanical worker routing, config | `omniagentos/longhaul/{workbook,prompts,routing,config}.py` |
| Steering fan-in | `omniagentos/longhaul/steering.py` + hook-eval `eval_kind="steering"` branch in `api/routes/sessions.py` |
| Intake entry (`lane`/`category` on POST /api/intake/quick) | `api/routes/intake.py`, `intake/service.py` (`_resolve_dispatch_lane`, `_set_board_routing`) |
| Task endpoints (message/conversation/longhaul, categories) | `api/routes/intake.py`, `api/routes/categories.py` |
| Dashboard (category grouping, task thread, attempt timeline, workbook) | `dashboard/src/app/{board,activity}` |
| Migration | `db/migrations/043_longhaul.sql` |
| Config | `configs/longhaul.yaml` |

## Invariants (violating these reintroduces shipped bugs)

- **The engine is the SOLE respawn owner.** The supervisor's built-in auth retry and
  the "all logins broken" notifier are both gated off for sessions whose title
  carries the `[longhaul:<attempt_id>]` marker. One death → exactly one successor.
  The A2 reaper MAY *kill* a longhaul session (idle/budget, with `killed_by`
  attribution, under the per-lane `sessions.idle_minutes` override the engine
  persists at spawn — `configs/longhaul.yaml idle_minutes`, default 45) — but it
  never respawns: `on_session_terminal` discriminates on the terminal row's
  `killed_by` (operator set `{operator, cancel_requested}` → superseded/cancel;
  any other killer on a `killed` row → the killed branch → exactly one successor).
- **`reconcile_board` and `board_sweep` skip `lane='longhaul'` cards.** The engine
  owns their status/park_state; the sweep must not block/archive parked tasks.
- **Reviews claim `phase='reviewing'` BEFORE the reviewer runs** (15-min stale
  reset). Two engine processes exist (API + sessions daemon); without this gate a
  peer tick reads the closed-attempt review window as a crash and churns attempts.
  `open_attempt` also refuses terminal/archived tasks inside its own transaction.
- **Usage limits cool accounts, never disable them** (`claude_accounts.cooldown_until`,
  status `rate_limited`; `next_account_for_spawn` filters cooling accounts). Hard
  auth failures keep the disable path. Limit detection is STRUCTURED-FIRST: a clean
  completion (successful result, no terminal errors, rc 0) beats any intermediate
  api_retry 401/429 hint — recovered blips must not cool healthy accounts.
- **Steering can never be lost:** turns persist in `conversations`
  (`scope_type='task'`, scope_id=`btk_*`, `meta.delivery`), fan into the live
  session's `session_messages` for PostToolUse `additional_context` delivery, are
  re-injected into every (re)spawn prompt (last 10, regardless of receipts), and a
  task **cannot go `done` while `pending_steering` is non-empty** — the engine
  respawns a continuation instead.
- **Categories serialize — lane-scoped (FB4+):** `claim_category_slot` enforces
  `wip_limit` (default 1) in one `BEGIN IMMEDIATE`, counting ONLY
  `lane='longhaul'` cards; a parked `waiting_capacity` task HOLDS its slot; an
  already-`in_progress` task re-claims as success (never demote).
  `category_id` itself is cross-lane board-taxonomy METADATA — swarm and fast
  cards may carry one (the old longhaul-XOR-swarm guard is gone) and are inert
  to WIP counting and to `next_waiting_in_category` (also lane-filtered). Any
  future slot-consumption query MUST keep the `lane='longhaul'` scope.
- **The completion review fails CLOSED:** only an explicit `confirm` finishes;
  unavailable/unparseable reviewer parks `waiting_review` with bounded backoff.
- **Fable is excluded from worker routing** (`excluded_models`) — lead capacity is
  not for volume work; registry ranking picks opus/sonnet, codex sol/terra fallback.

## Operational gotchas (earned the hard way)

- The claude CLI has **no `--title` flag**; the attempt marker lives only on the
  sessions row. Passing unknown flags = instant exit-1 crash loop.
- `var/modelintel/registry.json` `models` is a **LIST** of entries with `key`
  (`claude-opus-5`, `claude-sonnet-5`, …); capability scores live at
  `capabilities.<domain>.score`; pricing is per-million.
- The claude CLI family alias `opus` resolves to **Opus 5**, so `routing.py`'s
  `spawnable` map keys it off `claude-opus-5`; `claude-opus-4.8` is deliberately
  absent (mapping both to `opus` would enqueue two identical workers).
- `launchctl kickstart -k` takes **one** service target; a second argument is
  silently ignored (stale-daemon confusion).
- The api plist runs `db.migrate` before uvicorn — restarting the API applies
  pending migrations.
- Sandboxed codex requires the metadata-only allows for intermediate dotfile path
  components (`runner/sandbox.py`) and the plain `model_reasoning_effort="low"`
  TOML quoting in `adapters/codex.py`.

## Notes (human)

Follow-ups tracked in lead memory: fast-crash-loop backoff guard; `steering_respawn`
attempt label (currently mislabeled `review_denied` on the steering-refusal path);
server-side test asserting steering hook-evals create zero approval rows.
