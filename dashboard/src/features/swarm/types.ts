/**
 * TypeScript surface for the Swarm Command Center (C1 scaffold).
 *
 * C1 renders against TODAY'S APIs only — it derives the fleet from board cards
 * carrying `swarm_run_id` (migration 045), the terminal grid from
 * `GET /api/sessions`, and provider health from `GET /api/accounts`. The
 * `GET /api/swarm` fleet endpoint (WP6a) is queried opportunistically and
 * degrades to the board-derived grouping on a 404 until it merges.
 *
 * Lives in this feature (not lib/contracts.ts, which is FROZEN and owned by the
 * lead) — same pattern as features/collab/types.ts and features/accounts/types.ts.
 */
import type { Account, AccountStatus } from "@/features/accounts/types";
import type { LiveBoardTask } from "@/features/collab/types";

/** Parsed shape of board_tasks.swarm_json (migration 045). All fields optional —
 * a card with no swarm metadata still renders (it degrades into Running). */
export interface SwarmJson {
  complexity?: string;
  risk_class?: string;
  est_manual_minutes?: number;
  est_agent_minutes?: number;
  owned_paths?: string[];
  tier_hint?: string;
  acceptance?: string;
  verify_command?: string;
  /** true on the auto-appended integration task. */
  integration?: boolean;
  /** Phase overlay on `in_progress` — review | testing | integration | running. */
  swarm_phase?: string;
}

/**
 * A live board card enriched with its swarm membership. `swarm_run_id`/`swarm_json`
 * are the additive board_tasks columns from migration 045; the board serializer
 * may not emit them until WP6/WP10, so both are optional and absent today (swarm
 * groups render empty until a run is dispatched).
 */
export interface SwarmBoardTask extends LiveBoardTask {
  swarm_run_id?: string | null;
  /**
   * The FULL migration-045 envelope. Board LIST endpoints (`GET /api/collab/board`,
   * `GET /api/board`) no longer send it: it is unbounded per card (one live board
   * shipped 47.8 MB of it across ~1k cards — 96% of a 51 MB response) and the two
   * fields below are all the kanban ever read. Still present on single-card and
   * run-detail payloads, so it stays typed and every reader falls back to it.
   */
  swarm_json?: SwarmJson | string | null;
  /**
   * `swarm_json.swarm_phase`, projected server-side (`json_extract`). Drives the
   * Review / Testing / Integration column split; null on non-swarm cards.
   */
  swarm_phase?: string | null;
  /**
   * `swarm_json.integration`, projected server-side. SQLite's `json_extract`
   * yields 1/0 for a JSON boolean, so the number arm is real for any consumer
   * that skips the store's normalization.
   */
  swarm_integration?: boolean | number | null;
}

export type SwarmRunStatus =
  | "queued"
  | "planning"
  | "running"
  | "merging"
  | "completed"
  | "failed"
  | "cancelled";

/** One row of the WP6a fleet list (`GET /api/swarm`). Shape is forward-looking;
 * every field is optional so a partial/early API response still renders. */
export interface SwarmRunSummary {
  id: string;
  status?: SwarmRunStatus | null;
  goal?: string | null;
  working_dir?: string | null;
  target_concurrency?: number | null;
  max_concurrency?: number | null;
  cost_usd?: number | null;
  budget_usd_max?: number | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** Per-status task counts, when the API provides them. */
  task_counts?: Record<string, number> | null;
  progress?: { done: number; total: number } | null;
}

/** Fleet-wide utilization (the plan's fleet capacity model). */
export interface SwarmFleetUtilization {
  active_sessions?: number | null;
  max_sessions_global?: number | null;
  active_swarms?: number | null;
  max_concurrent_swarms?: number | null;
  queued_runs?: number | null;
}

/** `GET /api/swarm` response (WP6a). */
export interface SwarmFleet {
  runs: SwarmRunSummary[];
  utilization?: SwarmFleetUtilization | null;
}

/** Aggregate done/total progress across active runs (`GET /api/swarm/overview`). */
export interface SwarmOverviewProgress {
  done: number;
  total: number;
  pct: number;
}

/** Fleet throughput block — the ThroughputPanel's tiles. Every value is a
 * computed rollup across active runs; `health` is a coarse string
 * (idle | healthy | degraded). */
export interface SwarmThroughput {
  est_manual_minutes: number;
  est_swarm_minutes: number;
  actual_minutes: number;
  speedup: number;
  time_saved_minutes: number;
  active_terminals: number;
  max_terminals: number;
  utilization_pct: number;
  idle_pct: number;
  tasks_per_hour: number;
  rate_limit_delays_avoided: number;
  health: string;
}

/** One in-flight swarm task from the overview — the phase-overlay join source
 * for the kanban's Review/Testing/Integration columns. Only claimed/in_progress
 * member cards appear here. */
export interface SwarmOverviewTask {
  task_id: string;
  /** review | testing | integration | running (columns.ts vocabulary). */
  phase: string;
  session_id?: string | null;
  assignment_reason?: string | null;
}

/** Consumed vs capped budget summed across active + running runs. */
export interface SwarmBudget {
  consumed_usd: number;
  cap_usd?: number | null;
}

/** `GET /api/swarm/overview` response (C2) — fleet-level metric rollup. */
export interface SwarmOverview {
  active: number;
  progress: SwarmOverviewProgress;
  est_completion_at?: string | null;
  throughput: SwarmThroughput;
  tasks: SwarmOverviewTask[];
  budget: SwarmBudget;
}

/** Formation bound to a swarm run (parsed from plan assumptions). */
export interface SwarmFormationInfo {
  id: string;
  implementers?: string[];
  reviewer?: string | null;
  planner?: string | null;
  mechanical_gate?: boolean;
  confidence?: number | null;
  low_confidence?: boolean;
  topology?: string | null;
  reason?: string | null;
}

/** One leader / swarm run in the team board. */
export interface SwarmTeamLeader {
  swarm_run_id: string;
  goal?: string | null;
  status?: string | null;
  formation?: SwarmFormationInfo | null;
  target_concurrency?: number | null;
  max_concurrency?: number | null;
  started_at?: string | null;
}

/** One worker/agent row on the team board. */
export interface SwarmTeamWorker {
  swarm_run_id: string;
  goal?: string | null;
  task_id: string;
  task_title?: string | null;
  task_status?: string | null;
  role: string;
  phase?: string | null;
  session_id?: string | null;
  provider?: string | null;
  model?: string | null;
  tier?: string | null;
  attempt_id?: string | null;
  started_at?: string | null;
  assignment_reason?: string | null;
  live: boolean;
}

/** One swarm.event feed item for spawns / leader updates. */
export interface SwarmTeamEvent {
  ts?: string | null;
  swarm_run_id: string;
  action: string;
  payload: Record<string, unknown>;
  event_id?: number | string | null;
}

/** `GET /api/swarm/team` — live agents + leaders without token streaming. */
export interface SwarmTeam {
  generated_at: string;
  running_agents: number;
  claimed_agents: number;
  active_swarms: number;
  leaders: SwarmTeamLeader[];
  workers: SwarmTeamWorker[];
  live_workers: SwarmTeamWorker[];
  recent_spawns: SwarmTeamEvent[];
  leader_updates: SwarmTeamEvent[];
}

/** A Claude/multi-provider account row, with the additive migration-045
 * `provider` column and the rotation `cooldown_until` timestamp the accounts
 * serializer already returns (SELECT *). Both optional so the reused accounts
 * client keeps typechecking. */
export interface ProviderAccount extends Account {
  provider?: string | null;
  cooldown_until?: string | null;
}

/**
 * One row of `GET /api/swarm/providers` (C4) — the ProviderHealthPanel's
 * PRIMARY source, backed by WP2's durable `limit_state.py`. `account_id` is
 * null for an implicit row (a known provider with no pooled account). Both the
 * primary route and the `GET /api/accounts` fallback are normalized to this
 * shape in `useProviderHealth`, so the panel renders one uniform row type.
 *
 * `reset_in_seconds`/`active_sessions`/`max_inflight` are only computable from
 * the durable primary source — the accounts fallback leaves them null (the
 * panel still ticks a live cooldown off `cooldown_until`).
 */
export interface ProviderHealthRow {
  provider: string;
  account_id: string | null;
  display_name: string;
  status: AccountStatus;
  cooldown_until: string | null;
  reset_in_seconds: number | null;
  active_sessions: number | null;
  max_inflight: number | null;
  status_detail?: string | null;
}

/** One executor attempt row from `GET /api/swarm/{id}` (`attempts`). */
export interface SwarmRunAttempt {
  id: string;
  board_task_id: string;
  session_id?: string | null;
  provider?: string | null;
  model?: string | null;
  tier?: string | null;
  effort?: string | null;
  end_reason?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
}

/** One member-task spec row from `GET /api/swarm/{id}` (`tasks`). */
export interface SwarmRunTaskRow {
  id: string;
  title?: string | null;
  status?: string | null;
  swarm_json?: SwarmJson | string | null;
}

/**
 * `attempts` as `GET /api/swarm/{id}` actually serializes it: a MAP keyed by
 * board-task id (`{task_id: attempt[]}`, see `api/routes/swarm.py`
 * `get_swarm_run`), NOT a flat array. This type used to say `SwarmRunAttempt[]`,
 * and every consumer called `.filter`/`for…of` on the object — a hard
 * `attempts.filter is not a function` TypeError thrown during
 * `SwarmRunCard`'s render, which unmounted the whole tree and turned /board
 * into Next's "Application error: a client-side exception has occurred".
 * The array arm is kept so a future flattened payload still typechecks; run
 * everything through `flattenAttempts` (runAggregate.ts) rather than indexing
 * either shape directly.
 */
export type SwarmRunAttempts = Record<string, SwarmRunAttempt[]> | SwarmRunAttempt[];

/** `GET /api/swarm/{id}` — the run detail the ONE-card kanban view and the
 * run modal are fed from (run row + member tasks + attempts). */
export interface SwarmRunDetail {
  run: SwarmRunSummary & {
    working_dir?: string | null;
    board_task_id?: string | null;
    target_concurrency?: number | null;
    error?: string | null;
  };
  tasks: SwarmRunTaskRow[];
  attempts: SwarmRunAttempts;
  progress?: { done: number; total: number } | null;
}

/** One session `POST /api/swarm/{id}/cancel` could not (or refused to) stop. */
export interface SwarmCancelFailure {
  session_id: string;
  reason: string;
}

/** Per-session outcome buckets from the cancel kill fanout. `cancelled` means
 * CONFIRMED process death (terminal session state read back); `kill_pending`
 * means the request is durably recorded but death is unconfirmed. */
export interface SwarmCancelSessions {
  cancelled: string[];
  already_terminal: string[];
  kill_pending: string[];
  failed: SwarmCancelFailure[];
  not_owned: SwarmCancelFailure[];
  unbound_attempts: string[];
  scan_error?: string | null;
}

/** `POST /api/swarm/{id}/cancel` response (contracts/swarm-api.md). A 200 with
 * `kill_complete: false` is a PARTIAL cancel — something may still be running
 * — and must never be rendered as a clean stop. */
export interface SwarmCancelResult {
  id: string;
  status: string;
  kill_complete: boolean;
  sessions: SwarmCancelSessions;
  warning?: string | null;
}
