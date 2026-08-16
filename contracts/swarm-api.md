# Swarm Mode API (WP6a — plan/read/cancel slice)

New, ADDITIVE contract for `omniagentos/api/routes/swarm.py`, following the same
conventions as `contracts/reliability-api.md`: JSON bodies, error envelope
`{"error": {"code", "message", "detail"}}`, a session-token gate
(`X-Session-Token`) on EVERY route below (hoisted to the router constructor —
`board_files.py`'s F-016 idiom, so a route added here later can never forget
it), `utc_now_iso()` timestamps, `new_id(prefix)` ids (`swr_` runs, `swa_`
attempts, `btk_` the originating board card). Registered into
`omniagentos/api/main.py` via one import + one `include_router` line.

Backing types: `omniagentos/swarm/contracts.py` (`SwarmRunStatus`,
`SwarmTaskSpec`, `SwarmPlan`, the `ACTION_*` constants, `SwarmEmitter`),
`omniagentos/swarm/dal.py` (`SwarmDal`, WP1), `omniagentos/swarm/activity.py`
(`StoreSwarmEmitter`, this slice).

**Two-slice rollout (plan doc, "WP6 — API + activity"):** this slice covers
plan/read/cancel. **WP4's planner (`omniagentos/swarm/planner.py`) merged
during this slice's own development** — this API was rebased onto it, so
`POST /api/swarm` calls the REAL `plan_swarm`/`provision_run` (not a
placeholder): a validated DAG is built and every board card is provisioned
in one transaction, run status lands at `planning`. The "if WP4 has merged"
framing the original brief anticipated is kept as a narrow resilience seam
(`swarm.py::_wp4_planner_functions`) against a future partial revert/rename —
if it ever trips, `POST /api/swarm` 503s with a clear message rather than
failing to import at process startup. **Run-start activation (a coordinator
actually executing the provisioned plan) and the cancel session-kill fanout
are WP5a's**, not this slice's — both are called out explicitly below where
they matter.

**Dashboard cross-reference:** `dashboard/src/features/swarm/` (C1 scaffold,
merged alongside WP4) already queries `GET /api/swarm` opportunistically and
was built against the `SwarmFleet`/`SwarmRunSummary`/`SwarmFleetUtilization`
shapes in `features/swarm/types.ts` — this contract's `GET /api/swarm` shape
below matches that file field-for-field (a few WP2/WP7-only fields are
present in the TS types but omitted here rather than fabricated).

## `swarm.py` — `/api/swarm`

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/swarm | → `{runs:[RunSummary], utilization:{active_sessions, active_swarms, queued_runs, max_concurrent_swarms}}` | `runs` = every NON-terminal run (`planning`/`running`/`merging`/`queued`) flattened together — terminal runs (`completed`/`failed`/`cancelled`) are excluded so this list never grows unbounded. `utilization.active_sessions` is a swarm-only stopgap (`SwarmDal.live_attempt_count`) until WP2's cross-lane `limit_state.py` ledger exists — it is NOT a fleet-wide (fast+longhaul+swarm) count yet. `max_concurrent_swarms` is the same placeholder-constant caveat as the budget ceiling below. |
| POST /api/swarm | `{brief\|spec, working_dir, budget_usd_max?}` → `202 {swarm_run_id, board_task_id}` | `brief`/`spec` are aliases for the same goal-text field (first non-blank wins); 400 if both blank. `working_dir` is validated against the board-files F-015 approved-root floor (`board_files._enforce_workspace_floor`) — 400 if missing/not-a-directory, 403 if it resolves outside the approved roots. `budget_usd_max` defaults to `DEFAULT_BUDGET_USD_MAX` (25.0) and 400s above `MAX_BUDGET_USD_MAX` (200.0) — both module constants in `swarm.py` pending a real `configs/swarm.yaml`. Calls `plan_swarm` (never raises — an unavailable/invalid model degrades to a flat single-task solo plan, recorded as an assumption) then `provision_run` (run row + root card + child cards + DAG edges in one transaction); `board_task_id` is the run's root/container card, NOT a work-unit task. Emits `plan_created`. 503 `unavailable` if the planner module cannot be resolved (see the resilience-seam note above). **Latency**: this call is INLINE, not backgrounded — Fable planning can legitimately take a while at high effort; see the route's own docstring for the tradeoff. |
| GET /api/swarm/{id} | → `{run, tasks:[board_task], deps:[swarm_dep], attempts:{task_id:[swarm_attempt]}, progress, metrics}` | 404 if the run doesn't exist. `tasks` = every board card with this `swarm_run_id` EXCLUDING the run's own root/container card (`run.board_task_id` — never a work unit, always `in_progress`, would otherwise inflate every count by one phantom task). `progress` = `{<BoardTaskStatus value>: count, ..., total}`. `metrics` = parsed `swarm_runs.metrics_json` (`{}` until the run reaches a terminal status and `swarm/summary.py`'s `compute_metrics`/`write_summary` populate it — see "WP7 run summary" below). |
| GET /api/swarm/{id}/activity?after=&limit= | → `[SwarmEvent]` | 404 if the run doesn't exist. Chronological (ascending `id`) `swarm.event` rows for this run; `after` is an event-id cursor (default 0 = from the start), `limit` 1–500 (default 200). Same replay-by-id idiom as the frozen SSE contract's `after_id` (`contracts/events.md`), under this route's own param name. |
| POST /api/swarm/{id}/cancel | `{}` → `{id, status, kill_complete, sessions:{cancelled, already_terminal, kill_pending, failed:[{session_id, reason}], not_owned:[{session_id, reason}], unbound_attempts, scan_error?}, warning?}` (declared as `SwarmCancelResponse` in the OpenAPI artifact — `kill_complete`, `id`, `status` and `sessions` are REQUIRED, so generated clients can rely on them) | 404 if the run doesn't exist. Idempotent: cancelling an already-terminal run (`completed`/`failed`/`cancelled`) returns its current status, never a 409. A non-terminal run has `swarm_runs.status` flipped to `cancelled` **atomically** (`SwarmDal.cancel_run_if_active` — one UPDATE whose WHERE excludes terminal statuses, so of N concurrent cancels exactly ONE performs the transition) and only that winner emits `run_failed` with `{"reason": "cancelled"}` (plus the fanout facts when the run had anything running; a run with nothing running keeps that payload byte-identical). **It also stops the run's live sessions**: every open `swarm_attempts` row for THIS run id is first cross-checked against the durable ownership binding — `orchestrator_sessions.run_id`, written at spawn — and any session whose binding is missing, NULL, or names a different run is REFUSED and reported under `sessions.not_owned` (`swarm_attempts.session_id` has no FK; a mis-bound row must never kill a bystander). Owned, still-live sessions are signalled via the sessions DAL's `request_cancel` — the operator attribution, so the supervisor finishes them `cancelled` and the longhaul engine treats them as superseded rather than respawning — and each one is then re-read off the session row. **`sessions.cancelled` means CONFIRMED PROCESS DEATH**: a terminal session state was read back, which only the supervisor's kill/terminalize path writes. A session whose cancel is durably recorded but whose state is still non-terminal is `kill_pending` — signalled, not dead — and always makes `kill_complete` false (fail closed: unknown is not dead; press Stop again to re-verify). The fanout runs on the already-terminal path too, which is how a run whose coordinator died still gets its leaked sessions stopped. `sessions.scan_error` (string, present only on this failure) means the attempt scan itself failed — the live set is UNKNOWN, never "empty", so `kill_complete` is false. `kill_complete` is false whenever anything might still be running that the call did not verifiably stop — a `kill_pending` or `failed` session, a refused `not_owned` one, an attempt still mid-spawn with no session id to name (`unbound_attempts`), or a failed scan — and `warning` then names every stray. The scheduler remains the sole owner of respawn, attempt finalization and worktree cleanup; this route only signals and verifies. |

### `RunSummary` shape (GET /api/swarm — matches `features/swarm/types.ts::SwarmRunSummary`)

```json
{
  "id": "swr_...", "status": "planning", "goal": "...", "working_dir": "/...",
  "board_task_id": "btk_...", "target_concurrency": 3, "max_concurrency": 10,
  "cost_usd": 0.0, "budget_usd_max": 25.0,
  "created_at": "...", "started_at": null, "finished_at": null,
  "task_counts": {"pending": 0, "open": 2, "claimed": 0, "in_progress": 1,
                  "blocked": 0, "done": 3, "cancelled": 0},
  "progress": {"done": 3, "total": 6}
}
```

## `swarm.event` — the activity vocabulary

Written by `omniagentos.swarm.activity.StoreSwarmEmitter` (implements
`swarm.contracts.SwarmEmitter`) via the EXISTING `Store.insert_event`
mechanism, exactly like the reliability V2 events (`reliability-api.md`'s "SSE
additive event types" section) reuse it — a plain string `type` on the SAME
`events` table, `type="swarm.event"`, `target_type="swarm_run"`,
`target_id=<swarm_run_id>`, `actor="swarm"`. Emission is **best-effort and
never raises** into the caller (the `intake.service._emit_board_event` idiom)
— a full events table or a transient sqlite lock must never take down a slot
worker mid-spawn.

Read via `GET /api/swarm/{id}/activity` (this contract) and, for anything
wired onto the live SSE stream later, the same `GET /api/events` frame shape
`contracts/events.md` already documents (`payload` parsed from
`payload_json`).

### Action names (`omniagentos.swarm.contracts.SWARM_EVENT_ACTIONS`)

| Action | Emitted when | Payload keys (whichever apply — never a credential path or secret value) |
|---|---|---|
| `plan_created` | WP4 validates a DAG (topo-sorted, `owned_paths` containment-checked, integration task appended) | `task_count`, `parallelism_ratio` |
| `run_started` | WP5a's coordinator begins executing a plan | `target_concurrency` |
| `slot_opened` | a slot worker is admitted (grow) or exits (shrink) | `target_n`, `reason` |
| `task_assigned` | a task is claimed and an attempt opens | `task_id`, `provider`, `model`, `account` (LABEL, never a path/secret) |
| `task_completed` | a task's attempt closes with `end_reason="completed"` | `task_id`, `seconds` |
| `review_confirmed` | the reviewer CONFIRMs a task's output | `task_id` |
| `review_denied` | the reviewer DENYs (mechanical or LLM) | `task_id`, `reason` |
| `provider_switched` | a rate-limited/timed-out attempt reroutes to a different provider | `task_id`, `provider`, `reason` |
| `rate_limit` | a provider/account attempt hits a rate limit | `provider`, `account`, `reason` |
| `rate_limit_stall` | the run pauses because every eligible provider is cooling | `reason` |
| `task_split` | a twice-timed-out task is partitioned into ≤4 subtasks | `task_id`, `subtask_count` |
| `resize` | the slot budget (`target_n`) changes | `target_n`, `reason` |
| `task_blocked` | a task (or a transitive dependent of one) is blocked | `task_id`, `reason` |
| `approval_parked` | a task's command parks for human approval and releases its slot | `task_id` |
| `merge_started` | the integration task begins | — |
| `run_completed` | the run reaches `completed` | `seconds`; **WP7** emits a SECOND `run_completed` once `swarm/summary.py`'s terminal-status hook finishes (mechanical metrics + vault note): `score` (Throughput Score, 0-100), `summary_note_path`, `status`, `partial` |
| `run_failed` | the run reaches `failed` OR `cancelled` (`reason: "cancelled"` disambiguates a cancel from any other failure — there is no separate `run_cancelled` action) | `reason`; **WP7**'s summary hook likewise emits a SECOND `run_failed` (same score/note/partial payload as above) for failed/cancelled runs |

`StoreSwarmEmitter.emit` does not hard-validate `action` against this table —
an unrecognized action is still written (logged once at DEBUG) so a future
action name landing before this doc/module updates is still observable rather
than silently dropped.

### `SwarmEvent` shape (GET /api/swarm/{id}/activity)

```json
{
  "id": 1234, "ts": "2026-07-23T00:00:00Z", "type": "swarm.event",
  "actor": "swarm", "action": "task_assigned",
  "target_type": "swarm_run", "target_id": "swr_...",
  "payload": {"task_id": "btk_...", "provider": "codex", "model": "gpt-5.6-sol",
              "account": "codex-2"},
  "trace_id": ""
}
```

### WP7 run summary (`omniagentos/swarm/summary.py`)

Runs `compute_metrics`/`write_summary` at every terminal status (wired from
`SwarmScheduler._coordinate`'s single exit-point `finally` block, best-effort —
never blocks or raises into the coordinator). Every estimate used here is the
FROZEN `est_manual_minutes`/`est_agent_minutes` from `swarm_runs.plan_json` at
plan time — NEVER `board_tasks.swarm_json` (which can drift after a split) and
never recomputed, so a live re-estimate can never move the score.

`swarm_runs.metrics_json` (surfaced as `metrics` on `GET /api/swarm/{id}`)
gains, once a run goes terminal:

```json
{
  "score": 87.4, "speedup": 4.1, "parallelism_ratio": 2.0, "utilization": 0.62,
  "first_attempt_rate": 0.9, "rate_limit_stall_fraction": 0.05,
  "estimate_error": 0.12, "estimate_error_penalty": 3.6,
  "cost_usd": 1.2345, "cost_overshoot_usd": 0.0,
  "bottlenecks": {"critical_path_task_keys": ["t1", "t3", "integration"],
                  "most_retried_task_key": "t2", "most_retried_count": 2,
                  "cooldown_wait_seconds": 45.0, "provider_switches": 1},
  "tasks_done": 5, "tasks_blocked": 0, "tasks_total": 6, "partial": false,
  "summary_note_path": "/abs/path/vault/swarm/swr_....md"
}
```

> ⚠ **SUPERSEDED (2026-07-29):** The following formula is stale and has been superseded under the pre-null-discipline rules (referred to as the last documented "unknown-as-favourable" representation). It is retained here solely for historical accuracy.

Throughput Score (clamped 0-100): `40*min(1, speedup/parallelism_ratio) +
25*utilization + 25*first_attempt_rate + 10*(1 - rate_limit_stall_fraction) -
min(15, 30*estimate_error)`. `swarm_runs.summary_note_path` mirrors the same
path. `est_completion(run_id)` (remaining critical-path minutes over frozen
`est_agent_minutes` for not-yet-done tasks) is exposed for a future
`GET /api/swarm/overview` (C2, not yet built — `ThroughputPanel`/tiles in
`dashboard/src/features/swarm/` are placeholders pending that route).

The run note lands at `vault/swarm/<run_id>.md`: mechanical sections always
(result, bottlenecks, planner assumptions), plus ONE optional Fable
medium-effort narrative section ("Improvement opportunities") that silently
degrades to mechanical-only on any LLM failure. Knowledge ingest (discipline
`"swarm"`) is best-effort and never blocks the hook.

## Dashboard proxy

`dashboard/src/app/api/[...path]/route.ts`'s `AUTHORIZED_READ_PREFIXES` gained
`"swarm"` — every `GET`/`HEAD /api/swarm*` now proxies through
`proxyRead` (token attached server-side) instead of `proxyPublicRead`, matching
this contract's "session-token gate on every route." `POST /api/swarm` and
`POST /api/swarm/{id}/cancel` already reach the FastAPI app through the
catch-all's generic `proxyAuthorized` POST handler (unaffected by the read
allowlist — POST is always authorized there) — **planned, not built in this
slice**: a DEDICATED mutation route for cancel
(`app/api/swarm/[id]/cancel/route.ts`, the `app/api/sessions/[id]/kill/
route.ts` pattern) for parity with how other single-purpose mutations are
exposed to the dashboard's client SDK. Functionally the catch-all already
authorizes it today; the dedicated route is an ergonomics/SDK-typing follow-up,
not a security gap.

## Planned — Workstream C REST shapes (dashboard builds against these; NOT implemented in WP6a)

C2/C3/C4 of the plan doc's "Workstream C — Dashboard" need three more GETs,
fixed here so the dashboard can build against stable shapes before they exist.
`GET /api/swarm/overview` (C2) and `GET /api/swarm/providers` (C4) are now
IMPLEMENTED; `GET /api/swarm/terminals` remains planned (C1/C3 read
`GET /api/sessions` directly for the terminal grid, so the dedicated swarm-scoped
view is a later ergonomics follow-up, not a blocker).

**C3 SSE is wired** (`dashboard/src/features/swarm/useSwarmEvents.ts`): it
consumes the existing `GET /api/events?types=swarm.event` frame stream (the same
frozen SSE contract `contracts/events.md` documents, filtered to the single
`swarm.event` type — one `event: swarm.event` frame per row, the `action`
naming the change), cloning `lib/useReliabilityEvents.ts` verbatim in structure
(1.5s reconnect, `after_id` cursor in sessionStorage key
`omniagentos:swarm:lastEventId`, `resync` handling). Frames are **debounced
(1s) refresh HINTS only** — a hint's `action` name selects which REST reads to
refetch (overview/fleet/terminals/providers); REST stays the source of truth and
each panel keeps its own poll fallback. No new SSE event *type* is introduced —
`swarm.event` already exists (this section's activity vocabulary).

### `GET /api/swarm/overview` (IMPLEMENTED, C2)

Fleet-level metric rollup for the Command Center's header + throughput tiles,
aggregated across every ACTIVE run (`planning`/`running`/`merging`; per-run
detail stays on `GET /api/swarm/{id}`). Registered BEFORE `GET /{run_id}` so the
literal `/overview` path is never captured as a run id. An empty fleet returns
the same envelope with zeroed numbers, an empty `tasks` list,
`est_completion_at`/`budget.cap_usd` null, and `throughput.health = "idle"`.

The C2 route's response is richer than the WP6a-planned sketch (which is
superseded by this block): it carries the full throughput breakdown the
ThroughputPanel renders and the in-flight `tasks` that feed the kanban's
Review/Testing/Integration phase overlay.

```json
{
  "active": 2,
  "progress": {"done": 9, "total": 16, "pct": 56},
  "est_completion_at": "2026-07-23T04:00:00Z",
  "throughput": {
    "est_manual_minutes": 480, "est_swarm_minutes": 120, "actual_minutes": 62.5,
    "speedup": 7.7, "time_saved_minutes": 417.5,
    "active_terminals": 6, "max_terminals": 100,
    "utilization_pct": 6.0, "idle_pct": 94.0,
    "tasks_per_hour": 8.6, "rate_limit_delays_avoided": 3, "health": "healthy"
  },
  "tasks": [
    {"task_id": "btk_...", "phase": "running", "session_id": "ses_...",
     "assignment_reason": "codex · gpt-5.6-sol"}
  ],
  "budget": {"consumed_usd": 12.40, "cap_usd": 50.0}
}
```

`throughput.*` estimates + `est_completion_at` are WP7's (`swarm/summary.py`)
to freeze; until it lands the route computes them directly from each active
run's `metrics_json`/`plan_json` and its board cards' `swarm_json`, preferring
any WP7-populated `metrics_json` value so the swap is a drop-in (see the
`TODO(WP7)` in `swarm.py::_build_overview`). `est_swarm_minutes` is the DAG
floor (Σ `est_agent_minutes` ÷ `parallelism_ratio`); `actual_minutes` is
wall-clock since each run's `started_at`; `active_terminals` is the fleet live
attempt count; `rate_limit_delays_avoided` falls back to counting
`provider_switched` events; `budget.*` sums across active runs. `tasks` lists
only `claimed`/`in_progress` member cards (the phase-overlay join source);
`assignment_reason` surfaces the live attempt's provider/model.

### `GET /api/swarm/terminals` (planned, C1/C3)

One row per live session doing swarm work — the `TerminalGrid`'s data source,
a swarm-scoped view over `GET /api/sessions` joined against `swarm_attempts`.

```json
[
  {
    "session_id": "ses_...", "swarm_run_id": "swr_...", "task_id": "btk_...",
    "provider": "codex", "model": "gpt-5.6-sol", "account": "codex-2",
    "state": "running", "started_at": "...", "idle_seconds": 12
  }
]
```

### `GET /api/swarm/providers` (IMPLEMENTED, C4)

Durable rate-limit / cooldown / inflight state per provider account — the
`ProviderHealthPanel`'s PRIMARY source (the dashboard falls back to
`GET /api/accounts` only on a 404, per the plan doc's C4 bullet). Backed by
WP2's `omniagentos/routing/limit_state.py` (the single cross-lane authority):
`active_sessions` is the durable inflight count (live, activity-fresh,
non-kill-requested sessions attributed to the account + open longhaul attempts
+ unexpired reservations — NOT a naive session scan), and `reset_in_seconds` is
the remaining durable cooldown (null once it lapses, even if a stale
`cooldown_until` timestamp lingers).

A flat list, one row per registered `claude_accounts` account (grouped by the
migration-045 `provider` column) PLUS one **implicit** row per account-less
known provider — `claude` + the wrapped CLI providers `codex`/`grok`/
`gemini`/`kimi`/`qwen` (`swarm.py::_KNOWN_PROVIDERS`) — so a configured lane with no
pooled account still shows (account-less CLI providers authenticate via their
own config dir, not the multi-account pool). Rows are ordered known-providers
first (in that literal order), each provider's accounts default-first then
enabled-first; any additional provider seen in the table follows alphabetically.
`status` ∈ `ok`|`rate_limited`|`error`|`unknown` (anything else collapses to
`unknown`). `max_inflight` is the provider's configured per-account concurrency
ceiling (`configs/swarm.yaml`, default 3). Registered BEFORE `GET /{run_id}` so
the literal `/providers` path is never captured as a run id. Token-gated like
every route here.

```json
[
  {
    "provider": "claude", "account_id": "acct_...", "display_name": "Primary",
    "status": "rate_limited", "cooldown_until": "2026-07-23T08:00:00Z",
    "reset_in_seconds": 287, "active_sessions": 2, "max_inflight": 3,
    "status_detail": "quota exhausted"
  },
  {
    "provider": "gemini", "account_id": null, "display_name": "gemini",
    "status": "unknown", "cooldown_until": null, "reset_in_seconds": null,
    "active_sessions": 0, "max_inflight": 3, "status_detail": null
  }
]
```

## WP10 — intake wiring (`POST /api/intake/quick` `execute:"swarm"` passthrough)

`POST /api/intake/quick` (contract otherwise unchanged — `omniagentos/api/routes/
intake.py`) gains one OPTIONAL body field:

| Field | Type | Notes |
|---|---|---|
| `execute` | `"swarm"` \| `"single"` \| absent | `"swarm"` routes the goal into Swarm Mode dispatch (`intake.service.dispatch_spec(execute="swarm")`); `"single"` hard-suppresses the auto solo-vs-swarm upgrade (one orchestration however parallelizable the goal). ABSENT (the cockpit composer default) means the ROUTER decides: fastlane heuristic first, then the auto solo-vs-swarm upgrade (`dispatch_spec` mode `"auto"`), which is eligible for PROJECTLESS dispatches too — the swarm provision path git-initializes the managed workspace it creates. Any other value is a 422 (pydantic `Literal`); every other execution posture keeps its dedicated field/endpoint. |

**Precedence (both are absolute):**

1. `plan: true` — the plan-first preview branch wins unchanged; `execute` is
   ignored there.
2. An EXPLICIT `lane` (`"fast"` / `"longhaul"`) — explicit lanes are NEVER
   intercepted by swarm planning; the request takes its lane's existing path
   exactly as if `execute` were absent. The swarm branch fires only with
   `lane: "auto"` (the default).

**Goal-prefix force hatch (D12, cockpit):** a brief starting `solo:` /
`single:` / `swarm:` (case-insensitive) forces the matching `execute` value;
the prefix is stripped before the fastlane classifier or any planner sees the
goal. The prefix OUTRANKS a body `execute` (most-explicit wins — it sits in
the user's own text, not an automation default). A bare prefix with no task
text after it is treated as a normal goal. On the `plan: true` branch the
forced value is persisted in the pending card's intake directives exactly like
a body `execute`.

**Response (201, same envelope family as the other quick lanes):**

```json
{"board_task_id": "btk_...", "run_id": "orch_...", "status": "queued",
 "lane": "swarm", "message": "On it — swarm mode: planning the DAG, cards land on the board ↓"}
```

`board_task_id` is an INSTANT placeholder card (the fast lane's idiom) and
`run_id` a correlation id with no orchestrations row. The real work happens in
a BackgroundTask: `dispatch_spec(execute="swarm")` plans via
`swarm.planner.plan_swarm_bundles`, then

* **swarm-worthy plan** → provisioned as a swarm run (root card + child DAG
  cards, `lane` NULL, membership via `swarm_run_id`), the placeholder archived;
  activation ONLY behind `OMNIAGENTOS_SWARM_EXECUTE` (unset/false =
  provision-only, identical to `POST /api/swarm`);
* **N unrelated asks (bundles)** → each bundle dispatched independently as its
  own card(s): solo bundles down the existing orchestrate path, swarm-worthy
  bundles as their own runs — all drawing from the fleet budget;
* **single SOLO plan** → falls through to the EXISTING planned/orchestrate path
  with zero behavior change, REUSING the placeholder card + `run_id` above
  (async orchestration, exactly what the planned lane returns);
* **fleet admission** — at/over `configs/swarm.yaml max_concurrent_swarms` the
  run provisions with `swarm_runs.status='queued'` (parked, no coordinator,
  started oldest-first as capacity frees). Intake NEVER blocks on capacity;
  small/fast/longhaul tasks keep `reserved_small_task_slots` headroom via the
  scheduler's limits port.
* **failure** → the placeholder card flips `blocked` with the error in its
  description (the quick lanes' shared idiom).

Related WP10 pieces (library-level, documented here for the one-stop contract):
`dispatch_spec` accepts `execute="swarm"` and `execute="auto"` (the
solo-vs-swarm auto-decision: plan first, swarm only when parallelism pays), and
the sessions daemon runs a ONE-SHOT startup resume sweep
(`SessionSupervisor.resume_swarms_once` →
`swarm.scheduler.resume_stale_swarms`): provider-exec orphan reconciliation
(pgid + command identity) plus heartbeat-lease takeover of `running`/`merging`
runs whose coordinator heartbeat is > 2 min stale — the `adopt_run` CAS refuses
fresh heartbeats, so a live coordinator is never displaced; with
`OMNIAGENTOS_SWARM_EXECUTE` off only the orphan reconcile runs.

## Notes (human)
