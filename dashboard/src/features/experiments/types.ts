export type SurfaceKind =
  | "prompt"
  | "orchestration_genome"
  | "model_assignment"
  | "routing_policy"
  | "review_rubric";

export type SurfaceStatus = "champion" | "challenger" | "archived" | "draft";

export type ExperimentStatus =
  | "proposed"
  | "running_baseline"
  | "running_challenger"
  | "evaluating"
  | "replicating"
  | "decided"
  | "canary"
  | "promoted"
  | "rolled_back"
  | "failed"
  | "cancelled";

export type Disposition = "promote" | "reject" | "invalid" | "human_review";
export type ExplorePolicy = "exploit" | "explore" | "simplify" | "red_team";
export type Arm = "champion" | "challenger";
export type EvalSplit = "dev" | "held_out";

export interface Budgets {
  wall_minutes: number | null;
  tokens: number | null;
  cost_usd: number | null;
  replicates: number;
}

export interface PromotionThreshold {
  primary_delta_min: number;
  no_safety_regressions: boolean;
  cost_increase_max: number;
  min_utility: number;
  max_complexity_delta: number;
  require_human: boolean;
}

export interface Scorecard {
  champion: Record<string, number>;
  challenger: Record<string, number>;
  primary_delta: number | null;
  utility: number | null;
  complexity_delta: number | null;
  confidence_interval: number[] | null;
  safety_regression: boolean;
  audit_flags: string[];
}

export interface Experiment {
  id: string;
  hypothesis: string;
  discipline: string;
  explore_policy: ExplorePolicy;
  mutable_surface_kind: SurfaceKind;
  champion_surface_id: string;
  challenger_surface_id: string;
  eval_suite_id: string;
  dataset_hash: string;
  primary_metric: string;
  budgets: Budgets;
  promotion_threshold: PromotionThreshold;
  status: ExperimentStatus;
  disposition: Disposition | null;
  scorecard: Scorecard | null;
  snapshot_hash: string;
  created_at: string;
  updated_at: string;
}

export interface EvalResult {
  id: string;
  experiment_id: string;
  arm: Arm;
  suite_id: string;
  suite_version: number;
  split: EvalSplit;
  metrics: Record<string, number>;
  per_case: Record<string, Record<string, number>>;
  replicate: number;
  deterministic_passed: boolean;
  created_at: string;
}

export interface JudgeRecord {
  id: string;
  case_id: string;
  arm: Arm;
  judge_lineage: string;
  blind_token: string;
  score: number;
  notes: string;
  rubric_dimension: string;
}

export interface ExperimentDetail extends Experiment {
  eval_results: EvalResult[];
  judge_notes: JudgeRecord[];
}

export interface Surface {
  id: string;
  kind: SurfaceKind;
  discipline: string;
  version: number;
  path: string;
  content_hash: string;
  parent_version: number | null;
  status: SurfaceStatus;
  label: string;
  safety_relevant: boolean;
  created_at: string;
}

export interface SurfaceDetail extends Surface {
  content: string | Record<string, unknown>;
}

export interface ChampionHistoryEntry {
  id?: number;
  discipline: string;
  surface_kind: SurfaceKind;
  surface_id: string;
  event: string;
  promoted_from_experiment: string | null;
  promoted_at: string;
  surface_version?: number;
  decided_by?: string | null;
}

export interface Champion {
  discipline: string;
  surface_kind: SurfaceKind;
  surface_id: string;
  surface_version: number;
  promoted_from_experiment: string | null;
  rollback_to_surface_id: string | null;
  cas_version: number;
  promoted_at: string;
  history: ChampionHistoryEntry[];
}

export interface ExperimentFilters {
  discipline?: string;
  status?: ExperimentStatus;
}

export interface SurfaceFilters {
  discipline?: string;
  kind?: SurfaceKind;
}

export interface RunExperimentRequest {
  dry_run?: boolean;
}

export interface RunExperimentResponse {
  status: ExperimentStatus;
}

export interface DispositionRequest {
  decision: Disposition;
  note: string;
  decided_by: string;
}

/** GET /api/lab/disciplines — per-discipline roll-up with champion surfaces. */
export interface DisciplineSummary {
  discipline: string;
  experiment_count: number;
  top_elo: number | null;
  newest_at: string | null;
  champion_surfaces: Champion[];
}
