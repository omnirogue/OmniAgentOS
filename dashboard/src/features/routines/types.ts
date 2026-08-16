/** TypeScript mirror of the routine row shapes returned by
 * omniagentos/api/routes/routines.py (backed by omniagentos/scheduler/store.py's
 * RoutinesStore). Every routine round-trips trigger/gate/hard-cap/notification as
 * JSON objects — the DAL rejects a create/update whose shape doesn't match its
 * declared *_type (see omniagentos/scheduler/routines.py:validate_routine). */

export type TriggerType = "cron" | "event";
export type GateType = "exit_code" | "test_command" | "metric_threshold" | "merge_candidate";
export type HardCapType = "max_iterations" | "budget_usd" | "human_checkpoint";
export type NotificationChannel = "slack" | "email" | "webhook" | "desktop";
export type RoutineStatus = "active" | "disabled" | "auto_paused";

export const TRIGGER_TYPES: TriggerType[] = ["cron", "event"];
export const GATE_TYPES: GateType[] = [
  "exit_code",
  "test_command",
  "metric_threshold",
  /** Grades a candidate SHA for merge to main — gate_config carries the
   * verifier command plus candidate_sha/merge_base_sha (40-char lowercase git
   * SHAs; migration 091, omniagentos/scheduler/routines.py GATE_TYPES). */
  "merge_candidate",
];
export const HARD_CAP_TYPES: HardCapType[] = ["max_iterations", "budget_usd", "human_checkpoint"];
export const NOTIFICATION_CHANNELS: NotificationChannel[] = ["slack", "email", "webhook", "desktop"];

export type Routine = {
  id: string;
  name: string;
  description: string;
  trigger_type: TriggerType;
  trigger_config: Record<string, unknown>;
  task_template: Record<string, unknown>;
  gate_type: GateType;
  gate_config: Record<string, unknown>;
  hard_cap_type: HardCapType;
  hard_cap_value: number;
  notification_target: Record<string, unknown>;
  status: RoutineStatus;
  auto_pause_reason: string;
  total_runs: number;
  accepted_runs: number;
  /**
   * Runs that produced no judgeable result — parked awaiting a human, or
   * nothing to do. Subtracted from `total_runs` to get the acceptance
   * denominator. Optional because a row read from a database older than
   * migration 103 does not carry it.
   */
  neutral_runs?: number;
  /** `null` = UNKNOWN (empty denominator). Never 0 for "no judgeable runs". */
  acceptance_rate: number | null;
  /**
   * `null` = UNKNOWN — at least one contributing run's cost was never
   * reported (omniagentos/scheduler/store.py, nullable since migration 119).
   * Never 0 for "unreported"; a genuinely exact zero total still renders 0.
   */
  total_cost_usd: number | null;
  cost_per_accepted_change: number | null;
  last_fired: string | null;
  created_at: string;
  updated_at: string;
};

export type RoutineRun = {
  id: number;
  routine_id: string;
  run_id: string | null;
  iteration: number;
  gate_passed: boolean | null;
  accepted: boolean | null;
  /**
   * `null` = UNKNOWN — this run's true cost was never reported
   * (omniagentos/scheduler/store.py, nullable since migration 120). The
   * `api.routines.runs(id)` fetch casts the JSON response to `RoutineRun[]`
   * without runtime validation, so this type is the only thing standing
   * between a legitimate null response and an unguarded `.toFixed()` crash.
   */
  cost_usd: number | null;
  stop_reason: string;
  notes: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

/** Cross-routine recent run item — the pinned section B contract shape
 * returned by ``GET /api/routines/runs``. */
export type RecentRunItem = {
  routine_id: string;
  routine_name: string;
  run_id: string | null;
  gate_passed: boolean | null;
  accepted: boolean | null;
  cost_usd: number | null;
  finished_at: string | null;
};

/** Orgdims loop template (mirror of ``omniagentos.orgdims.service.LOOP_TEMPLATES``). */
export type LoopTemplate = {
  id: string;
  title: string;
  purpose: string;
  topology: string;
  workstreams: string[];
  risk_classes: string[];
};

/** An accepted loop recommendation (what the "Accept" action on the Loops
 * page turns into a real routine create). */
export type LoopRecommendation = LoopTemplate & {
  reason?: string;
};

export type CreateRoutineInput = {
  name: string;
  description?: string;
  trigger_type: TriggerType;
  trigger_config: Record<string, unknown>;
  task_template: Record<string, unknown>;
  gate_type: GateType;
  gate_config: Record<string, unknown>;
  hard_cap_type: HardCapType;
  hard_cap_value: number;
  notification_target: Record<string, unknown>;
};

/** Model formation and routing configuration — returned by GET /api/models/formation. */
export type ModelFormation = {
  swarm_planner?: {
    model: string;
    effort: string;
  };
  model_ladder?: string[];
  lane_floors?: Record<string, string[]>;
  integration_roles?: Record<string, {
    harness: string;
    model: string;
    effort: string | null;
    can_merge_to_main?: boolean;
  }>;
  loop_models?: Record<string, {
    harness: string;
    model: string;
    effort: string | null;
    can_edit_plans?: boolean;
  }>;
  default_model?: string;
};
