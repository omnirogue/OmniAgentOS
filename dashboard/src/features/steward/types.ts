/**
 * Types for the Steward horizon dashboard: morning briefing, goals, comms
 * transcripts, suggestions, and alerts. Mirrors the REAL response shapes
 * returned by omniagentos/api/routes/{briefings,goals,comms,suggestions,alerts,voice}.py
 * — verify against those files (and omniagentos/steward/store.py) before changing
 * a field here.
 */

/** risk_class / proposed action class — same domain as contracts.ActionClass. */
export const RISK_CLASSES = [
  "read_only",
  "sandboxed_creation",
  "internal_reversible",
  "external_reversible",
  "consequential",
  "irreversible",
] as const;
export type RiskClass = (typeof RISK_CLASSES)[number];

export const SUGGESTION_STATES = ["open", "approved", "dismissed"] as const;
export type SuggestionState = (typeof SUGGESTION_STATES)[number];

export const ALERT_STATES = ["open", "acked"] as const;
export type AlertState = (typeof ALERT_STATES)[number];

/** omniagentos/comms/extract_batch.py is the source of truth for these values. */
export const KB_STATUSES = ["none", "selected", "skipped", "failed", "extracted"] as const;
export type KbStatus = (typeof KB_STATUSES)[number];

export interface BriefingSection {
  title: string;
  body: string;
}

export interface BriefingSummary {
  subject: string;
  headline: string;
  sections: BriefingSection[];
  composed_by: string;
}

export interface BriefingDelivery {
  channel: string;
  ok: boolean;
  detail: string;
  artifact_id: string | null;
}

export interface Briefing {
  id: string;
  briefing_date: string;
  vault_path: string | null;
  summary: BriefingSummary;
  deliveries: BriefingDelivery[];
  voice_artifact_id: string | null;
  acked_at: string | null;
}

export interface NorthStar {
  source: string;
  metric: string;
}

export interface MetricSnapshot {
  id: number;
  goal_id: string | null;
  source: string;
  metric: string;
  value: number;
  unit: string;
  window: string;
  captured_at: string;
  meta: Record<string, unknown>;
}

export interface Goal {
  id: string;
  name: string;
  description: string;
  discipline_id: string | null;
  north_star: NorthStar;
  target: Record<string, unknown>;
  keywords: string[];
  priority: number;
  status: string;
  created_at: string;
  updated_at: string;
  /** Keyed by metric name (see omniagentos/goals/service.py::_metrics). */
  latest: Record<string, MetricSnapshot | null>;
  trend: Record<string, MetricSnapshot[]>;
}

export interface GoalFact {
  fact_id: number;
  statement: string;
}

/** A pared-down view of a `runs` row (see omniagentos/db/migrations/001_init.sql). */
export interface GoalRun {
  id: string;
  task_id: string;
  state: string;
  harness: string;
  model: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  cost_usd: number | null;
}

export interface GoalDetail extends Goal {
  facts: GoalFact[];
  suggestions: Suggestion[];
  recent_runs: GoalRun[];
}

export interface CommsMessage {
  id: number;
  source: string;
  external_id: string;
  thread_id: string;
  sender: string;
  recipients: string[];
  sent_at: string | null;
  subject: string;
  body_text: string | null;
  attachments: unknown[];
  kb_status: KbStatus;
  episode_id: number | null;
  created_at: string;
}

export interface CommsSource {
  name: string;
  kind: string;
  status: string;
  config: Record<string, unknown>;
  last_poll_at: string | null;
  last_error: string | null;
}

export interface CommsMessageFilters {
  source?: string;
  q?: string;
  thread_id?: string;
  kb_status?: string;
  limit?: number;
  before_id?: number;
}

/** The suggestion's `proposed_plan` — the exact instruction that would be
 * dispatched to a live agent if approved (see omniagentos/steward/suggest.py's
 * build_plan, which reads `prompt`/`harness`/`action_class` verbatim off this
 * object). `action_class` is the same ActionClass vocabulary as `risk_class`
 * but is the value actually used to build the run's plan step, so it is
 * surfaced separately in case the two ever diverge. */
export interface ProposedPlan {
  prompt: string;
  harness: string;
  action_class: string;
}

export interface Suggestion {
  id: string;
  title: string;
  rationale: string;
  evidence: unknown[];
  proposed_plan: ProposedPlan | null;
  risk_class: RiskClass;
  source: string;
  goal_id: string | null;
  alert_id: number | null;
  outcome: Record<string, unknown>;
  state: SuggestionState;
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
  task_id: string | null;
  run_id: string | null;
}

export interface ApproveSuggestionResult {
  suggestion: Suggestion;
  task_id: string;
  run_id: string;
}

export interface Alert {
  id: number;
  rule: string;
  severity: string;
  title: string;
  body: string;
  evidence: Record<string, unknown>;
  cooldown_key: string | null;
  state: AlertState;
  created_at: string;
  acked_at: string | null;
}

export interface AlertCount {
  open: number;
}

export interface VoiceProviderStatus {
  provider: string;
  status: string;
  detail: string;
}
