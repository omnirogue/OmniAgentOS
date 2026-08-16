# Execution — orchestrator, runner, routing & adapters

The execution layer is the durable state machine that turns a `task` into one or more
`runs`, each of which executes a persisted `plan_json` step list through a CLI adapter
(Claude/Codex/Grok/Kimi/Gemini/Qwen) and lands a terminal state with a JSONL ledger manifest
and a vault note. Everything here is SQLite-WAL backed (`var/omniagentos.db`), migrated
from the frozen `contracts/schema.sql` (Wave 0) via numbered migrations under
`omniagentos/db/migrations/`.

## State machines (`omniagentos/contracts.py`, frozen)

- **TaskState**: created → ready → running → done/blocked/failed (see
  `contracts/statemachine.md`). A task's projected state always reflects its LATEST run
  (by `queued_at`) — never derived by scanning all runs.
- **RunState**: queued → running → (approval/awaiting_approval) → done/failed/cancelled,
  with `worker_id` fencing (D-006): every step-boundary write includes
  `expect_worker=self`; if the check fails (another worker reclaimed the run), the
  runner aborts immediately without continuing steps or adapter calls.
- **ApprovalState**: pending → approved/rejected/expired (see `governance.md`).
- **SessionState**: running → awaiting_approval → resuming (Session Bridge, long-lived
  Claude Code bridge sessions supervised by `omniagentos/sessions/supervisor.py`).

## Orchestrator (`omniagentos/orchestrator/`)

`Orchestrator.run()` conducts tier-escalating executor sessions: CHEAP → FUSION →
FUSION_ULTRA, with cascade learning (win/loss traces) gated by
`OMNIAGENTOS_CASCADE=1` and optional Reflexion corrective retries
(`OMNIAGENTOS_REFLEXION=1`, off by default). `CrossLineageReviewer.review()`
(`orchestrator/review.py`) is the current single-reviewer quality gate (codex-critic by
default); the V2 judge panel (`reliability/judges.py`, see `reliability.md`) is a
separate, purpose-built 3-family panel for *improvement proposals*, not a replacement
for this reviewer.

## Runner (`omniagentos/runner/core.py`)

A polling worker (`com.omniagentos.runner`, keep-alive launchd job,
`scripts/launch-omniagentos.sh`) executes queued runs' step plans with:

- **Heartbeats**: `upsert_heartbeat` fires every loop iteration and from a daemon
  thread during step execution (interval = `stale_s / 3`, default stale threshold 30s);
  a missing heartbeat marks a run stale for reclaim by another worker.
- **Stale-worker reclaim**: dead workers' runs are reclaimed via the fencing check
  above — no double-execution.
- **Finalization quarantine**: a run that fails finalization 5+ times is marked
  `vault_note_path=_FINALIZE_QUARANTINE_SENTINEL` and stops retrying (terminal but
  unfinalized; operator must inspect).
- **Auth-failure retry**: one retry per session on auth failure; the multi-account
  auto-disable rule (`32edd06`) disables a dead Claude account and respawns once on the
  next enabled account — V2's `account_disabled` failure class (see `reliability.md`)
  watches for this without re-triggering on the same underlying failure.
- **Budgets**: `wall_ms`, `tokens`, `cost_usd`, `turns` caps enforced per run
  (`omniagentos/budget/`); `max_turns` is NOT yet tracked as a DB column (spin-loop
  detection is in-process only).

## Model routing & adapters (`omniagentos/routing/`, `omniagentos/adapters/`)

- **Adapters** (`adapters/registry.py` `REGISTRY` dict): `cli-claude`, `cli-codex`,
  `cli-grok`, `cli-kimi`, `cli-gemini`, `cli-qwen`, etc. — each invokes a LOCAL CLI binary via
  subprocess (never a direct HTTP client); new providers need a local CLI.
  `HarnessType` (frozen, `contracts.py`) enumerates the supported keys.
  Adapter calls run under the existing OS-sandbox wrapper (§ governance.md).
- **modelintel registry** (`omniagentos/modelintel/`): daily `com.omniagentos.modelintel`
  job (07:15) merges curated priors + live benchmark fetches (Aider leaderboard,
  SWE-bench-Live, OpenRouter, Artificial Analysis) into `var/modelintel/registry.json`
  + vault notes; the LLM router (Grok-backed, `modelintel/router.py`) reads the
  DERIVED `~/.claude/fusion/model-rankings.json` / `model-intel.json`, not the registry
  directly — the daily cycle must succeed before the router reflects new scores.
- **Account pool** (`routing/account_pool.py`, opt-in `OMNIAGENTOS_ACCOUNT_POOL=1`):
  in-process, thread-locked credential rotation across `claude_accounts` config dirs
  for rate-limit resilience; state is per-process (does not cross launchd daemons).
  A separate `sessions`-table account rotation (`last_used_seq`) exists for the
  Session Bridge — the two rate-limit mechanisms do not share state.
- **Fallback chain** (`intake/fallback.py`): hardcoded `[Fable → Opus → Sol]`,
  sequential; `routing/cascade.py` is the generic tier-escalation primitive used for
  verification (`run_cascade(work, verify, ...)`).

## Ledger & vault (execution side)

Every run writes an immutable JSONL manifest (`ledger/`, append-only — audit trails
must append, never rewrite existing lines) and, on completion, a vault run note. The
V2 reliability system reads `runs.error` / `steps` / `sessions.error` PURELY from the
DB (`reliability/detector.py`) — it never modifies runner/orchestrator code, matching
principle 1 ("extend, never replace") in `docs/architecture/V2-DESIGN.md`.

## Where V2 hooks in

The reliability watch cycle (`reliability.md`) is a read-only consumer of this layer's
tables (`runs`, `steps`, `sessions`, `approvals`, `events`, `claude_accounts`,
`routine_runs`) — zero changes to runner/orchestrator/executor code. Safe recovery
(`reliability/recovery.py`, pending — see `reliability.md`) will requeue-once for
transient classes only, never touching money/delete classes (the existing
`is_hard_stop()` floor, see `governance.md`).

## Deterministic landing pipeline (2026-08-09)

The autonomous improvement conveyor is owned by this repository under `pipeline/`, with runtime
state in the ignored `var/loopqueue/`. Planner and Reviewer loops feed proposals and findings to
the Implementer; the Reviewer is not a serial candidate-verdict stage. The Implementer builds an
immutable candidate commit and supplies execution evidence. Routine real diffs proceed directly
to the mechanical gate, while auth, payments, money, migrations, permissions, policy,
secret-bearing allowlists, and pipeline-critical paths require a named different-lineage approval
bound to the exact final `head_sha`.

The gate daemon is the sole lander. Each cycle reconciles candidate SHAs already on `main`, derives
changed files from Git, selects up to ten pairwise-disjoint candidates, and cherry-picks them onto
an ephemeral deterministic train based on the current `main`. Gate and schema changes travel as
one-member trains; secret-bearing surfaces such as `configs/accounts.yaml` remain human-only. A
candidate branch is only a convenience: a missing branch is recoverable by exact SHA and a branch
that moved away from its approved SHA is refused.

Each train tip receives its candidate-bound signed receipt before the complete merge gate runs.
Slot one gates locally and slot two gates on the twin Mac; the scheduler never routes both slots to
the same host. Evidence must return to the landing machine. Immediately before landing, the daemon
requires `main` to equal the train base; a moved base causes rebuild and re-gate. A valid train
advances `main` only by fast-forward, records one terminal event per candidate, and retires its
temporary branch. The serving daemon and judge execute pinned `main` copies so pipeline-critical
changes cannot grade or deploy themselves.

## Notes (human)
