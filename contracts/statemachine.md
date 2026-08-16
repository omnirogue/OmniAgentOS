# Execution semantics (FROZEN, Wave 0 — revision 2 after design-adversarial review)

Authoritative spec for p02-runner (both compete implementations MUST conform) and the
behavioral reference for p03-api, p07-policy, p13-smoke. Transition tables live in
`omniagentos/contracts.py`; the persistence seam is the frozen `contracts.Store`
Protocol (implemented by p01 as `omniagentos.db.store.SqliteStore`); pure-function
signatures for policy/budget/ledger/vault live in `contracts/interfaces.md`.
Design findings folded in: D-001..D-013 (see run decision log).

## Ownership of state (D-002 — memorize this table)

| State | Writer | When |
|---|---|---|
| `tasks.state` DRAFT→READY→QUEUED | **API only** | task creation, operator actions, run enqueue |
| `tasks.state` QUEUED→terminal | **Runner only** | projection of the task's LATEST run (below) |
| `runs.state` (all) | **Runner only**, except QUEUED at enqueue (API) and cancel_requested flag (API) | |
| `steps.*`, `idempotency.*` | Runner only | |
| `approvals` rows | Runner creates (parking); API decides; Runner voids on terminal (D-009) | |
| `pause` row | API only writes; Runner only reads + requeues (D-008) | |
| `events` rows | Both processes (each emits what it did); NEVER `worker.heartbeat` (D-010) | |

**Task projection rule:** a task with runs reflects its LATEST run (the run with the
newest `queued_at`; `Store.list_runs` returns runs newest-first by queued_at):
run QUEUED → task QUEUED; run RUNNING → task RUNNING; run AWAITING_APPROVAL → task
AWAITING_APPROVAL; run VALIDATING → task VALIDATING; run COMPLETED → task COMPLETED;
run FAILED → task FAILED; run CANCELLED → task CANCELLED; run PAUSED → task PAUSED.
The runner applies this projection via `Store.update_task_state(task_id, target)`
(the guarded form) at every run-state change it makes — the TASK_TRANSITIONS table
now permits every projection edge H1 uses, including the direct RUNNING/VALIDATING →
COMPLETED that H1's reviewer-less flow needs (D-002/DV). Guard failures on projection
are logged as audit events, never raised — the run's own state is the source of
truth. (If the latest run isn't the only run, earlier runs never move the task
backward: project only from the latest.)

**IDs:** every entity id comes from `contracts.new_id(prefix)` — tsk/run/apr/art.

**H1-live states (D-013):** TaskState REVIEWING, REVISION_REQUESTED, RETRYING are
RESERVED for Horizon 2 — no H1 code path may enter them, and the dashboard renders
only live states. RunState VALIDATING is live and precisely defined below.

## Runner process

`python -m omniagentos.runner [--worker-id w1] [--poll-ms 500] [--once]`

Loop: upsert heartbeat (heartbeats TABLE only — never an events row) → requeue
PAUSED runs if pause is off (D-008: the runner is the single owner of
PAUSED→QUEUED) → service parked runs (below) → reclaim stale runs → claim one
queued run (skip claiming entirely while pause.paused=1) → execute steps → repeat.
`--once` processes at most one claim-or-parked-service pass then exits (tests).

### Heartbeats & reclaim
- `Store.upsert_heartbeat` every loop iteration.
- A run in ANY non-terminal owned state (RUNNING **or** AWAITING_APPROVAL) whose
  worker's `last_beat_at` is older than `OMNIAGENTOS_STALE_S` (default 30) — or
  whose worker row is missing — is stale (DV-003: a worker that dies while a run is
  parked must not strand it forever).
- `Store.reclaim_stale_runs` atomically (BEGIN IMMEDIATE) reassigns `worker_id`
  where the stale condition still holds, then the new owner RESUMES the run
  (RUNNING) or services it as a parked run (AWAITING_APPROVAL). Emit an
  `audit.event` (action=reclaimed).

### Fencing (D-006 — MANDATORY, compete-judged)
Every step-boundary write the runner makes for a claimed run passes
`expect_worker=<self>`; the Store appends `AND worker_id = ?` and reports zero rows
as False. On False the runner ABORTS that run's loop immediately (another worker
reclaimed it) without executing further steps or adapter calls. A soft-stalled
worker that wakes after reclaim therefore cannot double-execute a step. Test shape:
reassign `runs.worker_id` mid-run; original loop must stop before the next adapter
call.

### Claiming
`Store.claim_next_run(worker_id)`: single BEGIN IMMEDIATE transaction that selects
the oldest QUEUED run, sets state=RUNNING, worker_id, started_at (COALESCE), and
returns the row (None when nothing to claim). Never called while paused.

### Step execution
`runs.plan_json` is an ordered array of step specs:
`{"name": str, "kind": "agent"|"effect"|"validate", "action_class": ActionClass,
  "params": {...}}`
(`ledger`/`vault` are NOT step kinds — see Finalization, D-007.)

For each step, strictly in seq order:
1. **Pause check**: `pause.paused=1` → transition run RUNNING→PAUSED (projection
   follows), checkpoint, stop this run. Between steps only; in-flight steps finish.
2. **Cancel check**: `runs.cancel_requested=1` → cancellation path (below).
3. **Policy check**: `policy.evaluate_action(step.action_class, cfg)`; if
   `requires_approval` and `Store.get_approval_for(run_id, step_seq)` has no
   APPROVED row → create approval row (id=new_id('apr'), step_seq=seq,
   state=pending), run RUNNING→AWAITING_APPROVAL, emit approval.requested, park.
4. **Budget check**: `budget.check(spec, used_wall_ms, used_tokens, used_cost_usd)`
   (exact signature in contracts/interfaces.md §p06) on the run's accumulated usage
   → not allowed → run FAILED, error='budget_exceeded'. Usage recorded after each
   step via `Store.upsert_budget_usage` + run-row rollups.
5. **Checkpoint BEFORE** (fenced): step row status=STARTED + checkpoint_json +
   started_at, committed before executing. Emit step.updated.
6. **Execute** by kind:
   - `agent` → `registry.resolve_adapter(params.adapter)`; sandbox level =
     `policy.sandbox_for_tools(harness, task.tools_allowed)` (D-005) — the adapter
     MUST be invoked with exactly that SandboxSpec. Agent steps are re-executable
     on resume (their external effects must live in `effect` steps).
   - `effect` → side effect through the idempotency registry (below).
   - `validate` → run params.command in subprocess with timeout; nonzero exit →
     step FAILED. The run is in state VALIDATING while validate steps execute
     (definition below).
7. **Checkpoint AFTER** (fenced): status=COMPLETED + result_json + finished_at.
   Emit step.updated.
8. Step failure → retries per params.retries (default 0), else run FAILED with
   compensation: completed steps declaring params.compensate get compensations
   executed in reverse order; mark them COMPENSATED. Compensation failures are
   logged, never raised over the original error.

**VALIDATING definition (D-013):** plans order all non-validate steps before any
validate steps (the API rejects interleaved plans). When the first validate step
begins, the runner transitions RUNNING→VALIDATING; validate steps then run under
state VALIDATING; success → COMPLETED, failure → FAILED. Runs with no validate
steps go RUNNING→COMPLETED directly. Deterministic event sequence either way.

### Idempotency registry (D-012 semantics with canonical example)
- Key: `sha256(run_id|seq|name|digest(params))` unless params.key is given.
- BEFORE effect: `Store.idem_insert(key,...)`; False (exists):
  - receipt has result_json → effect already ran; adopt result, mark step SKIPPED.
  - receipt without result_json → unknown outcome:
    1. step declares `params.probe` (a command; exit 0 = effect landed) → run it;
       landed → adopt+complete receipt+SKIPPED; not landed → re-execute.
    2. else `params.unsafe_retry=true` → re-execute.
    3. else → run FAILED, error='idempotency_unresolved' (fail closed — never
       silently double-fire an external action).
- AFTER effect: `Store.idem_complete(key, result_json)`.

**Canonical effect step (used by the B2 kill -9 test — the probe is REQUIRED for
B2 to end COMPLETED):**
```json
{"name": "append-receipt", "kind": "effect", "action_class": "internal_reversible",
 "params": {"effect": "append_file", "path": "var/smoke/receipt.txt",
            "line": "effect-<run_id>",
            "probe": "grep -q 'effect-<run_id>' var/smoke/receipt.txt"}}
```
Built-in effect executors the runner ships: `append_file` (above) and `noop`.

### Resume (restart or reclaim)
Iterate the plan in order: COMPLETED/SKIPPED steps are skipped (result_json
re-hydrates context); a STARTED effect step resolves via idempotency rules; a
STARTED agent/validate step re-executes (pure by contract); continue to the end.

### Cancellation (D-009)
`POST /api/runs/{id}/cancel` → API sets `runs.cancel_requested=1` (+audit event).
Runner honors it (a) between steps, and (b) for PARKED runs during parked-run
service. Cancel path: adapter.cancel(session_ref) if a session exists, run →
CANCELLED, compensations for completed steps that declare them,
`Store.void_pending_approvals(run_id, note='voided: run cancelled')` (pending →
EXPIRED with decision_note), projection, finalization.

### Parked-run service (every loop)
For each run in AWAITING_APPROVAL owned by self (or unowned after reclaim):
1. cancel_requested → cancellation path.
2. approval APPROVED → run → RUNNING, continue at the parked step.
3. approval REJECTED → run FAILED (error='approval_rejected'), void other pending
   approvals, finalize.
4. approval past expires_at → mark EXPIRED, run FAILED (error='approval_expired'),
   finalize.

### Finalization (D-007 — NOT plan steps)
Exactly once, immediately AFTER a run reaches a terminal state and its usage
rollups are persisted, the runner:
1. builds RunManifest (state = terminal state; `receipts: list[IdempotencyReceipt]`
   from `Store.idem_for_run` — same shape as the API/dashboard receipts, DV-001),
2. `ledger.append_manifest` → sets runs.manifest_path,
3. `vault.render_run_note` + write → sets runs.vault_note_path,
4. emits run.updated (terminal).
Idempotent at TWO layers so a crash between the file append and the DB path-write
cannot double-write (D-007 residual): (a) the runner skips any of steps 2-3 whose
runs.manifest_path/vault_note_path is already set; (b) `ledger.append_manifest` is
itself idempotent by run_id — if a line for that run_id already exists in the target
month file it returns that path WITHOUT appending. Every terminal run MUST end with
exactly one manifest line and one vault note. On terminal states the runner also
voids any still-pending approvals (D-009).

## API process responsibilities (p03)
- CRUD per contracts/api.md; transition guards via contracts tables; every
  mutation emits an events row (actor='api').
- Default plan for POST /tasks/{id}/runs when plan omitted: `[agent]` ONLY
  (finalization is the runner's job — D-007). Reject plans where a validate step
  precedes a non-validate step.
- SSE per contracts/events.md: poll events table (250ms) + synthesize
  worker.heartbeat frames from the heartbeats table at most 1/15s (D-010).
- The API never executes steps and never writes runs.state beyond enqueue(QUEUED);
  cancel = cancel_requested flag only.

## Non-goals for H1 (do not build)
Priorities beyond FIFO, multi-worker sharding fairness, retry/backoff policy
config, DAG plans, TaskState REVIEWING/REVISION_REQUESTED/RETRYING paths, events
retention (documented follow-up: rotate/archive when events > 1M rows).
