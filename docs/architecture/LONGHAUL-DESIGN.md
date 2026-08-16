# Longhaul — Long-Horizon Agentic Coding Lane

**Status:** DESIGN v1.1 (post opus-critic review, GO-WITH-CHANGES folded) — implementation branch `feat/longhaul`, built in isolated worktree `~/OmniAgentOS-worktrees/longhaul`
**Date:** 2026-07-23 · **Author:** Fable (lead) · Recon: 5-scout sweep, findings verified with file:line refs.

> **BUILD ISOLATION (binding):** other orchestration runs are active in the main `~/OmniAgentOS` tree (V2 reliability build — untracked `omniagentos/reliability/**`, `omniagentos/company/**`, `omniagentos/archdocs/**`, migration `042_reliability_company.sql` (already APPLIED to the live DB, hence version 42), V2 dashboard pages, and uncommitted edits to shared dashboard files incl. the proxy `route.ts`; plus a fusion efficiency-pack run). All longhaul work happens ONLY in this worktree. Workers never run git commands, never touch any of the paths above, and never restart services. Live migration + deploy happen only at coordinated cutover by the lead. Longhaul migration stays `043` (migrate.py is gap-tolerant by numeric prefix).

## 0. Goal (the operator's requirements)

R1. Give a long coding task → it runs unattended to completion; check back later, it's done.
R2. Usage limit hit mid-way → seamless handoff to another account (or another harness) — "another person picks it up".
R3. Mid-task steering: type into a conversation box ON THE TASK; it reaches the current worker (or the next one). Survives handoffs.
R4. Tasks organized into CATEGORIES; work inside a category is serialized (no conflicts), categories run in parallel.
R5. Route each long task to the best agentic coder available, weighing capability AND speed (registry-driven).
R6. No fusion multi-stage overhead in this lane: ONE strong coder end-to-end, single review gate at completion.

## 1. Core model shift

Today a session ≈ one attempt: it dies → task stalls (auth respawn = fresh prompt, context lost; steering messages queue but are NEVER delivered — `claim_session_messages` has no caller). Longhaul makes the **board task the durable unit** and sessions an **ordered chain of executors**:

- durable per-task state: brief + acceptance criteria, **workbook** (continuity file), task conversation, session chain history
- the workbook is maintained CONTINUOUSLY by the worker (a limited session cannot write a handoff after the fact)
- successor context = brief + workbook + undelivered steering turns + `git log`/status summary — never transcript replay (cross-account `--resume` impossible: conversation lives in the account's CLAUDE_CONFIG_DIR; replay would also burn the successor's budget)
- same-account interrupted sessions MAY use native `--resume <session_ref>` (the `_resume()` path already does this for approvals)

## 2. Verified integration points (from recon)

- Spawn argv: `supervisor.py:387–415` — `claude -p "<prompt>" --settings <bridge> --output-format stream-json --verbose --include-hook-events --disallowedTools Task --model <m> --session-id <uuid>`, `stdin=DEVNULL`, `CLAUDE_CONFIG_DIR` from `_resolve_spawn_account` (`:70–92`) → `accounts/service.py:297–339 next_account_for_spawn()`.
- Steering gap: `POST /api/sessions/{id}/message` (`api/routes/sessions.py:252–275`) → `session_messages` (033) — **no consumer**. `claim_session_messages` (`sessions/dal.py:618–627`) / `mark_session_message_applied` (`:641–653`) defined, never called. Bridge hooks POST to `/api/sessions/hook-eval` on every tool call → that request/response loop is the delivery channel.
- Auth detection: `supervisor.py:590–665` + `_auth_failure_detail :119–155`; reaction `:721–803` (mark error + `set_enabled(False)` + notify + 1× respawn with FRESH prompt). Gated on cost==0 (`:725–731`). **No usage-limit detection anywhere in the session path.** `claude_accounts.status` documents `rate_limited` but nothing writes it.
- Accounts now: example-org (enabled, default), acmeuni (enabled), initech (disabled/OAuth broken).
- Task↔session: `board_tasks.result_ref = session_id` (`intake/service.py:986–995`); `reconcile_board` (`:1773–1887`) projects session state → board (completed→done — **no quality gate; exit-0-with-unfinished-todos → done**).
- Progress: TodoWrite stream events → `sessions.todos_json` (`supervisor.py:649–706`); `files_json` too.
- Fast lane: `intake/fastlane.py classify_task_speed` (heuristic) → `quick_dispatch` (`api/routes/intake.py:467–486`) → `dispatch_spec(execute="session", fast=True)` (`intake/service.py:746–814`) → spawn. **Default model = FABLE_MODEL for every session** — 41/54 sessions ever ran on fable. Longhaul must not do this.
- conversations (031): polymorphic `(scope_type 'project'|'task', scope_id, seq)` + `ConversationStore.append_turn/recent_turns` (`memory/store.py:58–155`). Task thread rides this with `scope_type='task', scope_id=<btk_*>` (ids are prefix-disjoint from tsk_*; readers query exact scope_id — no collision).
- Registry: `var/modelintel/registry.json` (daily 07:15) has per-model `coding-implementation`, `agentic-tool-use`, `debugging` scores + `measured_latency_ms` + pricing. Mechanical scorer template: `modelintel/router.py:_mechanical (:73–117)`.
- Review gate template: `orchestrator/review.py:64–106` CrossLineageReviewer (default `cli-codex`).
- Dashboard: board COLUMNS `board/page.tsx:21–25`, render loop `:204–287`; composer `CommandComposer.tsx` → `POST /api/intake/quick`; activity page `activity/[taskId]/page.tsx:128–320` with session-scoped steer box `:312–318`; reusable `ConversationPanel` `HierarchyViews.tsx:545–634`; board.* SSE events already flow outside the frozen EVENT_TYPES enum — new event types follow that pattern; proxy read-allowlist `api/[...path]/route.ts:26–42`.

## 3. Database — migration `043_longhaul.sql` (additive only; frozen tables untouched)

```sql
CREATE TABLE task_categories (
  id TEXT PRIMARY KEY,                       -- new_id('cat')
  name TEXT NOT NULL,                        -- display name
  slug TEXT NOT NULL UNIQUE,                 -- lower-kebab, app-computed
  color TEXT NOT NULL DEFAULT '',
  wip_limit INTEGER NOT NULL DEFAULT 1 CHECK (wip_limit >= 1),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
ALTER TABLE board_tasks ADD COLUMN category_id TEXT REFERENCES task_categories(id);
ALTER TABLE board_tasks ADD COLUMN lane TEXT;             -- NULL|'fast'|'longhaul'
ALTER TABLE board_tasks ADD COLUMN park_state TEXT;       -- NULL|'waiting_category'|'waiting_capacity'|'waiting_review'
ALTER TABLE board_tasks ADD COLUMN longhaul_json TEXT NOT NULL DEFAULT '{}';
  -- {acceptance: str, max_sessions: int, workbook_path: str, review: {verdict, notes, at}, parked_detail}

CREATE TABLE task_sessions (                 -- ordered executor-attempt chain per board task
  id TEXT PRIMARY KEY,                       -- new_id('tks')
  board_task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  session_id TEXT,                           -- ses_* for claude-bridge attempts; NULL for codex attempts
  harness TEXT NOT NULL,                     -- 'cli-claude'|'cli-codex'
  model TEXT NOT NULL,
  account_id TEXT,                           -- claude_accounts.id when applicable
  started_at TEXT NOT NULL, ended_at TEXT,
  end_reason TEXT,                           -- completed|usage_limited|auth_failed|crashed|killed|unfinished_exit|review_denied|superseded
  detail TEXT NOT NULL DEFAULT '',
  UNIQUE(board_task_id, seq)
);
CREATE INDEX idx_task_sessions_task ON task_sessions(board_task_id, seq);

ALTER TABLE claude_accounts ADD COLUMN cooldown_until TEXT;   -- UTC ISO; NULL = available
```

Task conversation: NO new table — `conversations` with `scope_type='task'`, `scope_id=<board_task_id>`; `meta_json.kind ∈ {steering, status, handoff, system}`; `meta_json.delivery = {session_id, delivered_at} | {pending: true}`. Caveat (opus finding 7, accepted): `memory/assemble.py` ancestor/rolling-summary inheritance expects `tsk_` ids and silently no-ops for `btk_` scopes — longhaul helpers use `recent_turns` (opaque scope) only; never route `btk_` scopes through `memory/assemble`.

Indexes: `CREATE INDEX idx_board_tasks_category ON board_tasks(category_id, status)` and `CREATE INDEX idx_board_tasks_park ON board_tasks(park_state) WHERE park_state IS NOT NULL` (partial). `claude_accounts` is tiny — no index needed for the cooldown filter.

## 4. Modules — `omniagentos/longhaul/`

### `store.py` (W1) — all new DAL, one place
`LonghaulStore(db_path)` with short `BEGIN IMMEDIATE` writes (repo idiom):
- categories: `create_category(name, color, wip_limit) -> Category` (slug-dedupe: returns existing on same slug), `list_categories()`, `get_category(id_or_slug)`, `update_category(id, ...)`
- **`claim_category_slot(category_id, board_task_id) -> bool`** — single `BEGIN IMMEDIATE` transaction: count `board_tasks WHERE category_id=? AND id!=? AND status IN ('claimed','in_progress') AND archived_at IS NULL`; `< wip_limit` → set this task `status='in_progress'` (from 'pending'/'open' only, WHERE-guarded like claim CAS) and return True; else set `park_state='waiting_category'`, return False. **Slot semantics:** a parked `waiting_capacity` task KEEPS `status='in_progress'` (park_state marks it) and therefore holds its category slot — its project dir contains mid-task work; letting a sibling start would violate R4. `waiting_category` tasks are `pending` and hold nothing.
- `release_category_slot(board_task_id)` — no-op marker (release is implicit via status change); provides `next_waiting_in_category(category_id) -> board_task_id|None` (FIFO by created_at, `park_state='waiting_category'`).
- task_sessions: `open_attempt(board_task_id, harness, model, account_id, session_id) -> TaskSession` (seq = MAX+1), `close_attempt(id, end_reason, detail)`, `list_attempts(board_task_id)`, `current_attempt(board_task_id)`.
- longhaul task fields: `set_lane/park_state/longhaul_json` accessors; `list_parked(park_state)`; `set_task_category(board_task_id, category_id)`.
- accounts cooldown: `set_account_cooldown(account_id, until_iso, detail)` (writes `status='rate_limited'`, `status_detail`, `cooldown_until`), `clear_expired_cooldowns(now) -> [account_id]` (status back to 'ok', cooldown NULL).
- conversation helpers (thin wrappers over `memory.store.ConversationStore`): `append_task_turn(board_task_id, role, content, meta)`, `task_turns(board_task_id, limit)`, `pending_steering(board_task_id)` (turns with `meta.delivery.pending`), `mark_turn_delivered(turn_id | (scope,seq), session_id)`.

**Also (surgical, outside longhaul/):** `accounts/service.py next_account_for_spawn()` gains filter `AND (cooldown_until IS NULL OR cooldown_until <= ?)` — additive to the existing enabled=1 filter, same ordering.

### `limits.py` (W2) — pure detection, no I/O
- `classify_terminal(events: list[dict], return_code: int, cost_usd: float) -> Classification` where `Classification = {kind: 'completed'|'usage_limited'|'auth_failed'|'crashed'|'unfinished_exit', reset_at: str|None, detail: str}`.
- **Structured-first detection (opus finding 4 — do not fork from the auth detector's discipline at `supervisor.py:627–632`):** PRIMARY signals are structured: system `api_retry` events with `error_status==429`, terminal result `subtype`/`terminal_reason`/`error` fields containing `rate_limit_error`, and limit phrases appearing in the terminal error's OWN `error`/`result` field. Free-text phrases (`"usage limit"`, `"you've reached your"+"limit"`, `"5-hour limit"`, `"weekly limit"`, `"limit resets"`, `"out of extra usage"`, `"upgrade to continue"`) found only in ASSISTANT-authored text are corroboration ONLY — never sufficient alone (a task whose OUTPUT discusses usage limits must not cool a healthy account). `"overloaded_error"` counts only as the terminal error (transient overload mid-stream with successful continuation is NOT terminal). NOT cost-gated (limits hit mid-session with cost>0) — but require the error gate (is_error / error subtype / terminal_reason), mirroring `supervisor.py:164–180`.
- `parse_reset_time(text, now) -> str|None` — handles "resets at 3am", "resets 10:30am (America/New_York)", "resets at 6pm ET", ISO stamps; returns UTC ISO. Unparsable → None (caller applies default cooldown from config).
- auth patterns: import/reuse the existing `_auth_failure_detail` logic (do not fork the pattern list — expose it from supervisor or duplicate ONLY via a shared constant module).

### `workbook.py` (W2)
- Workbook lives at `var/longhaul/<board_task_id>/WORKBOOK.md` (never inside the user's project). Sections: `## Goal`, `## Acceptance criteria`, `## Plan`, `## Progress log`, `## Decisions`, `## Next steps`, `## Status` (`WORKING|BLOCKED|DONE`).
- `init_workbook(task_id, title, brief, acceptance) -> path`, `read_workbook(task_id)`, `workbook_status(task_id) -> str|None` (parse `## Status`), `workbook_summary(task_id, max_chars)` (for prompts/UI).
- The workbook dir must be added to the session's `extra_write_roots`.
- **Supervisor-side checkpoint (opus workbook finding):** "maintain continuously" is prompt-level hope; the engine makes it durable — on EVERY attempt terminal, `on_session_terminal` appends a `### Checkpoint (attempt N)` block to WORKBOOK.md containing the session's final `todos_json` snapshot + `files_json` (touched files) + end reason. The continuation prompt includes the last todo snapshot verbatim, so the live TodoWrite checklist survives handoff even though each attempt is a fresh session.

### `prompts.py` (W2)
- `initial_prompt(task, workbook_path, acceptance, category, steering)` — longhaul protocol: maintain TodoWrite continuously; update WORKBOOK.md after every milestone (plan first, then progress/decisions/next-steps as you go); commit early and often when in a git repo; self-verify against acceptance criteria before finishing; finish ONLY by setting `## Status: DONE` with a completion summary; if blocked, set `## Status: BLOCKED` + why; steering may arrive mid-run as tool feedback — treat it as the operator's voice and adapt.
- `continuation_prompt(task, workbook_content, git_summary, undelivered_steering, prior_end_reason)` — "You are taking over from a colleague mid-task…" (also used verbatim for cli-codex attempts).
- `steering_wrap(turns)` — formatting for hook delivery and for prompt injection.
- All untrusted content (steering text, workbook content in successor prompts) wrapped with the existing `quote_untrusted` idiom.

### `routing.py` (W2, with W3 glue)
- `rank_workers(registry_path, cfg, claude_capacity: int) -> list[Worker]` where `Worker = {harness, model, score, why}` — **no per-account expansion** (opus finding 5: `spawn`/`_launch` always round-robin via `_resolve_spawn_account`; the design's additive `cooldown_until` filter in `next_account_for_spawn` is what implements account handoff — the ranked list stays harness+model only, and `claude_capacity` (count of enabled, non-cooling accounts, computed by the caller) simply gates whether cli-claude entries are eligible at dispatch time).
- Mechanical only (no LLM): `score = w_quality*(0.6*coding_impl + 0.4*agentic_tool_use) + w_speed*latency_norm − w_cost*cost_norm`; hard filters: model must be session-spawnable (claude CLI models: opus/sonnet; fable EXCLUDED by default via `excluded_models` — Fable capacity is for leads, and fable-by-default is why limits get hit). Cross-harness entries (cli-codex × gpt-5.6-sol/terra) rank after claude entries when `cross_harness_fallback: true`, and become the front of the list when `claude_capacity == 0`.
- Registry read is best-effort: missing/stale registry → static fallback order from `configs/longhaul.yaml`.

### `engine.py` (W3) — the state machine (all decisions logged as events)
- `dispatch(task_id)`: claim category slot (if category set) → rank workers → prep (acceptance extraction via one cheap `cli-claude` sonnet call, heuristic fallback; init workbook) → `open_attempt` → spawn (claude path: `SessionSupervisor.spawn` with model/account override + workbook root; codex path: run `CodexAdapter` attempt in a worker thread) → set `result_ref`, `lane='longhaul'`.
- `on_session_terminal(session, events, rc)`: `limits.classify_terminal` →
  - `completed` + workbook `DONE` → review gate (config-optional; CrossLineageReviewer with acceptance + `git diff` summary): pass → board done + notification `done`; deny → `close_attempt(review_denied)` + continuation respawn with reviewer findings.
  - `usage_limited` → `set_account_cooldown(account, reset_at or now+default)` + `close_attempt(usage_limited)` + immediate respawn on next ranked worker; none available → `park_state='waiting_capacity'` + notification `blocked`.
  - `auth_failed` → **the supervisor's built-in retry is SUPPRESSED for longhaul-owned sessions** (opus BLOCKER 2: otherwise two respawns run in the same project dir — gate the retry block at `supervisor.py:762–770` on "session's board task is not lane='longhaul'"). Keep disable+notify from the built-in path; the ENGINE alone performs the continuation respawn on the next worker. One death → exactly one successor, always engine-owned.
  - `crashed|killed` (not kill_requested) → same account healthy + `session_ref` present → native `--resume` continuation (once per attempt), else fresh continuation respawn.
  - `unfinished_exit` (rc==0, workbook not DONE) → continuation respawn ("you stopped early").
  - attempts ≥ `max_sessions` (default 8) → board `blocked` + notification `escalation` with chain summary.
- **Board-status ownership (opus BLOCKER 1):** `reconcile_board` (`intake/service.py:1826–1864`) must `continue` early for cards with `lane='longhaul'` — the engine exclusively owns their `status`/`park_state` (otherwise every board poll stamps a limit-parked task `blocked` via `_SESSION_TO_BOARD[FAILED]`, and `claim_category_slot`'s pending/open guard can never re-dispatch it). Same skip in `board_sweep` stale-card handling. Longhaul status vocabulary: `pending` (waiting_category) → `in_progress` (active attempt OR parked waiting_capacity) → `done` | `blocked` (terminal escalation only) | `cancelled`.
- **Idempotency journal:** `longhaul_json.phase ∈ {prep, running, reviewing, parked, done, blocked}` updated in the same transaction as the action it describes; invariant: at most ONE open attempt (`ended_at IS NULL`) per task — `open_attempt` refuses otherwise; `dispatch`/respawn are re-entrant: crash between `open_attempt` and spawn leaves an open attempt with no session → `tick()` closes it (`end_reason='crashed'`, detail='spawn_incomplete') and redispatches; cooldown written but respawn crashed → `tick()` sees in_progress task with no open attempt → redispatch. Every transition emits an `events` row (actor `longhaul`).
- `tick()` (wired into the supervisor loop cadence): resume `waiting_capacity` tasks when `clear_expired_cooldowns` frees an account; dispatch `waiting_category` FIFO when a slot frees; watchdog stale/orphaned attempts (above).
- Steering fan-in: task turn arrives → live claude attempt: also `enqueue_message(session_id, ...)` for hook delivery + mark delivered when applied; no live session / codex attempt: stays pending, injected into next prompt.
- Kill-switch: board archive/cancel of a longhaul task kills the live session and closes the chain (`superseded`).

### Steering delivery (W3, wiring in existing files) — REVISED per opus finding 3
The PreToolUse-only channel CANNOT deliver on the normal path: the hook client fast-path-allows read-only tools without calling hook-eval at all (`hook_client.py:145–147`), discards `reason` on allow (`:163–165`), and PreToolUse reasons reach the model only on deny/ask. Therefore:
1. **Guaranteed path (the contract):** every (re)spawn prompt — initial, continuation, and approval `_resume()` continuation — injects all pending steering turns at the top. Zero-tool, read-only-phase, parked, and codex attempts all receive steering this way at the next boundary.
2. **Live path (best-effort acceleration):** register a NEW PostToolUse hook in the bridge settings (`sessions/install.py` — currently it only removes PostToolUse) whose handler calls hook-eval (or a lighter dedicated eval kind); the response carries pending messages via `hookSpecificOutput.additionalContext`, which Claude Code injects into the model's context after the tool result. Accept the extra round-trip per tool call (config `steering.live_delivery: true` to disable if overhead bites).
3. **Atomic claim (opus finding 6):** new DAL method `claim_and_mark_session_messages(session_id)` — SELECT + `applied_at` stamp in one `BEGIN IMMEDIATE` so tick + hook paths never double-deliver; prompt-injection path dedupes by message id via `meta_json.delivery`.
Mark task-turn delivery receipts when applied. **This fixes the existing dead session-steer box for ALL lanes, not just longhaul.**

### Intake (W3, surgical edits)
- `POST /api/intake/quick` body gains optional `lane` (`auto|fast|longhaul`) + `category` (name or id; auto-creates). `classify_task_speed` extension: `lane=auto` → existing heuristic, but "planned"-classified CODING tasks route to longhaul instead of orchestrate when `longhaul.default_for_planned_coding: true` (config, default true); explicit `lane` always wins.
- `dispatch_spec` passes category/lane through to the board card; fast tasks with a category also respect `claim_category_slot`.

## 5. API (W4) — all session-token gated, error envelope preserved
- `GET /api/categories`, `POST /api/categories {name,color?,wip_limit?}`, `PATCH /api/categories/{id}`
- `POST /api/board/{task_id}/message {content}` → append task turn + fan-in; 404 archived; returns turn
- `GET /api/board/{task_id}/conversation?limit=` → turns with delivery status
- `GET /api/board/{task_id}/longhaul` → {attempts chain, workbook content, acceptance, park_state, current worker}
- `GET /api/board` rows gain `category`, `lane`, `park_state`, attempt count (extend reconcile projection)
- SSE (string event types, board.* pattern — frozen EVENT_TYPES untouched): `task.message`, `task.handoff`, `task.longhaul` (progress/park/review), emitted via existing `_emit`/`insert_event`
- Dashboard proxy: add the new GETs to `isAuthorizedReadPath`; mutations ride the existing gated catch-all

## 6. Dashboard (W5)
- Board: group-by-category toggle (default on when any category exists) — sections per category (+ "Uncategorized"), each with the 3 status columns; category chip + lane badge + attempt count on cards; parked badge ("waiting: capacity/category").
- CommandComposer: category picker (autocomplete + create-new) + lane selector (Auto / Fast / Long task).
- Activity page: persistent task ConversationPanel (adapted from `HierarchyViews.tsx:545–634`) wired to the new endpoints (send + live via `task.message` SSE) — replaces the dead session-steer box for longhaul tasks (keep session box for plain sessions; it works once W3 lands); session-chain timeline (attempt #, harness/model/account label, end reason, duration); workbook viewer (rendered markdown, collapsible).

## 7. Config — `configs/longhaul.yaml`
```yaml
longhaul:
  default_for_planned_coding: true
  max_sessions: 8
  default_cooldown_s: 3600          # when reset time unparsable
  excluded_models: [fable]
  cross_harness_fallback: true
  weights: {quality: 0.6, speed: 0.25, cost: 0.15}
  static_fallback_order:            # when registry unavailable
    - {harness: cli-claude, model: opus}
    - {harness: cli-claude, model: sonnet}
    - {harness: cli-codex,  model: gpt-5.6-sol}
  review: {enabled: true, harness: cli-codex, deny_respawns: 2}
  prep: {model: sonnet, wall_ms: 90000}
```

## 7b. v1.2 — codex-critic deltas (binding on W1–W5)

1. **Durable dispatch (codex B1):** `open_attempt` is persisted BEFORE spawn; the spawned session's `title` carries the marker `[longhaul:<attempt_id>]` so `tick()` can reconcile the crash window between spawn success and recording `session_id` on the attempt (match sessions by title marker → attach or close). `tick()` reconciles EVERY nonterminal longhaul task each pass: open attempt with no session and no live spawn → close(`crashed`/`spawn_incomplete`) + redispatch; session terminal but attempt open → run `on_session_terminal` (idempotent, CAS on attempt `ended_at`).
2. **Hook response model (codex B4):** `SessionHookEvalResponse` (`api/models.py:274–278`) gains an explicit optional `additional_context: str | None` field; hook client maps it to `hookSpecificOutput.additionalContext` for PostToolUse. Unmodeled fields are silently dropped by FastAPI — the field MUST be modeled.
3. **Steering loss-safety:** continuation prompts include the last N (default 10) steering turns REGARDLESS of delivery receipts — duplicates in a prompt are harmless, lost messages are not. `applied_at` is stamped atomically at claim; a message applied to a session that then died is therefore still re-surfaced by the next continuation prompt.
4. **Codex attempt durability (codex M5):** codex attempts run via a supervised engine thread; `task_sessions.detail` records `{pid, pgid, started}`; cancel = kill process group from the attempt row; engine restart with an attempt whose pid is dead → close + redispatch; duplicate-prevention via the single-live-attempt unique index. Tests cover restart/cancel/duplicate.
5. **Review gate fails CLOSED (codex M6):** `CrossLineageReviewer` maps adapter failure → confirm (`orchestrator/review.py:64–105`); longhaul must NOT use that path raw. Engine wraps it: unparseable/unavailable reviewer ⇒ `park_state='waiting_review'` + bounded backoff retries (separate counter from deny_respawns); ONLY an explicit valid `confirm` finishes the task; reviewer exhausted ⇒ notification `escalation`, task stays parked for the operator.
6. **Schema hardening (codex M7):** migration 043 adds partial unique indexes `CREATE UNIQUE INDEX idx_task_sessions_live ON task_sessions(board_task_id) WHERE ended_at IS NULL` and `... ON task_sessions(session_id) WHERE session_id IS NOT NULL`; CHECK constraints on `lane IN ('fast','longhaul')`, `park_state IN (...)`, `task_sessions.harness IN ('cli-claude','cli-codex')`, `end_reason IN (...)`.
7. **Auth + SSE privacy (codex M8):** every new GET route carries the explicit session-token auth dependency (match existing gated GETs; note the `request.scope['path']` idiom from 55d19c4) + a 401 test each. SSE payloads for `task.message`/`task.handoff`/`task.longhaul` carry ONLY ids/seq/status — never steering or workbook content (UI refetches via gated endpoints).
8. **Failure-injection tests (codex M9):** the W-package test briefs must include: crash at each dispatch/handoff phase boundary (journal reconcile), duplicate terminal callback (idempotent), cancel racing respawn, reconcile+sweep behavior for every park_state, parallel hook claims (no double-delivery), codex cancel/restart, reviewer-unavailable park, 401s on new routes.

## 8. Non-goals
No orchestrator-tier or fusion changes; no new apps; frozen contracts (`contracts.py`, `contracts/**`, schema.sql, EVENT_TYPES, notifications kinds) untouched; in-process `routing/account_pool.py` untouched; V2 reliability work untouched (it will observe longhaul failures via runs/sessions rows as designed).

## 9. Packages (disjoint ownership; workers run only their own tests; NO git commands — integrator commits)

| Pkg | Owns | Worker |
|---|---|---|
| W1 schema+store | `db/migrations/043_longhaul.sql`, `longhaul/{__init__,store}.py`, additive filter in `accounts/service.py::next_account_for_spawn` + `set_account_cooldown/clear_expired_cooldowns` home, `sessions/dal.py::claim_and_mark_session_messages` (atomic), `tests/longhaul/test_store.py` — **store API here is the frozen surface W2–W5 code against** | sol-coder |
| W2 pure libs | `longhaul/{limits,workbook,prompts,routing}.py`, `configs/longhaul.yaml`, `tests/longhaul/test_{limits,workbook,routing}.py` | terra-coder |
| W3 engine+wiring | `longhaul/{engine,steering}.py`, edits: `sessions/supervisor.py` (terminal hook, longhaul gate on built-in auth retry :762–770, spawn overrides), `sessions/install.py` + hook script (PostToolUse additionalContext), hook-eval steering consumer, `intake/{fastlane,service}.py` (lane/category plumb + reconcile_board longhaul skip + board_sweep skip), `tests/longhaul/test_engine.py` (mock adapter + fake stream events) | sol-coder |
| W4 api | `api/routes/{categories}.py` + edits `api/routes/{intake,collab or board}.py` for message/conversation/longhaul endpoints, SSE emissions, `main.py` router registration line, proxy allowlist, `tests/longhaul/test_api.py` | luna-coder |
| W5 dashboard | board grouping, composer fields, activity conversation panel + chain timeline + workbook viewer, SSE hooks | terra-coder |
| W6 review | codex-critic (all), opus-critic (W3 + limits + category CAS) | critics |
| W7 integrate | migrate, full pytest, `npm run build`, service restart, e2e drills, commit, ARCHI/vault notes | lead |

W2/W4/W5 start from this doc's interfaces in parallel after W1's store API lands; W3 starts after W1+W2.

## 10. Acceptance drills (tested or scripted)
1. Category serialization: two longhaul tasks, same category → second parks `waiting_category`, dispatches when first completes; two categories → parallel. CAS race test: concurrent claims never exceed wip_limit. Parked `waiting_capacity` task HOLDS its slot (sibling stays queued).
2. Steering live: message → appears in the running session's context within one tool-call boundary via PostToolUse `additionalContext`; `applied_at` set atomically; visible in task thread with delivery receipt. Steering while parked / to a codex attempt / during a zero-tool stretch → delivered via next (re)spawn prompt injection, no message ever lost or duplicated.
2b. Board-poll stability: while a longhaul task is parked `waiting_capacity` with a FAILED session as `result_ref`, repeated `GET /api/board` calls NEVER change its status (reconcile skip verified).
2c. Limit false-positive: terminal result healthy but assistant output text discusses "usage limit" → account NOT cooled, task completes normally.
3. Limit handoff drill: synthetic terminal stream event with usage-limit text (+ reset time) → account gets `cooldown_until` (NOT disabled), successor attempt opens on the other account with workbook context, chain recorded, board card stays `in_progress`. Reset-time parse cases covered.
4. Both accounts cooling → task parks `waiting_capacity` + `blocked` notification; cooldown expiry → `tick()` auto-resumes. With `cross_harness_fallback` → codex attempt instead of parking.
5. rc==0 with workbook not DONE → continuation respawn; DONE + review pass → board done; review deny → respawn with findings, bounded by `deny_respawns`.
6. Auth failure keeps today's disable+notify AND the task continues on the next worker.
7. Existing suites green (`pytest`), dashboard `npm run build` green, existing endpoints unchanged.
8. Fable never selected as a longhaul worker with default config.
```
