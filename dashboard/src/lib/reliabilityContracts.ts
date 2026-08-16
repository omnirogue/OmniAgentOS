/**
 * V2 reliability / company contracts — additive event types + API response shapes.
 * Separate from the shared lib/contracts.ts surface.
 *
 * Mirrors docs/architecture/V2-DESIGN.md §9–§10 and the store models in
 * omniagentos/reliability/contracts.py (Python-side only; this is the TS surface).
 */

/** Additive SSE event types for the reliability system (not in frozen EVENT_TYPES). */
export const RELIABILITY_EVENT_TYPES = [
  "reliability.event",
  "improvement.updated",
  "audit.completed",
  "autonomy.changed",
  "org.updated",
] as const;

export type ReliabilityEventType = (typeof RELIABILITY_EVENT_TYPES)[number];

/**
 * L12 event-hub degraded notice SSE name (`event: eventbus.status`).
 * Not part of the frozen EVENT_TYPES tuple; consumers must register it explicitly.
 */
export const EVENTBUS_STATUS_EVENT = "eventbus.status" as const;

/** L12 org approve spawn-failure error/action codes. */
export const SPAWN_FAILED_CODE = "spawn_failed" as const;
export const AGENT_REQUEST_SPAWN_FAILED_ACTION = "agent_request.spawn_failed" as const;

/** Generic SSE row shape (mirrors EventRow without depending on frozen EventType). */
export interface ReliabilitySseRow {
  id: number;
  type: string;
  ts: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  trace_id: string;
}

// ── Risk / health domain ────────────────────────────────────────────────────

export type RiskLevel = 1 | 2 | 3 | 4;
export type HealthState = "healthy" | "degraded" | "critical" | "recovering";
export type EventSeverity = "info" | "warning" | "critical";
export type EventStatus =
  | "open"
  | "recovering"
  | "recovered"
  | "proposed"
  | "resolved"
  | "ignored";

export type ImprovementStatus =
  | "proposed"
  | "testing"
  | "judging"
  | "panel_blocked"
  | "awaiting_human"
  | "approved"
  | "applying"
  | "applied"
  | "monitoring"
  | "confirmed"
  | "rejected"
  | "rolling_back"
  | "rolled_back"
  | "failed"
  | "superseded";

export type AutonomyMode = "approve" | "auto";
export type AutonomyScopeType = "global" | "department" | "agent" | "kind";
export type JudgeVerdict = "approve" | "reject" | "approve_with_conditions" | "needs_human";
export type ModelFamily = "anthropic" | "openai" | "xai" | string;

// ── API response shapes ─────────────────────────────────────────────────────

/** B02/L03 open-event aggregate availability (never invent zeros when unavailable). */
export type OpenEventsState = "current" | "unavailable";
export type LastAuditState = "current" | "never_run" | "unavailable";
export type WatchStateKind =
  | "never_run"
  | "current"
  | "stale"
  | "last_known_good"
  | "unavailable";

export interface ReliabilityOpenEvents {
  info: number | null;
  warning: number | null;
  critical: number | null;
}

export interface ReliabilityIncident {
  code: string;
  component: string;
  severity: string;
  message: string;
}

/** Nested watch object from reliability-summary.v1. */
export interface ReliabilityWatchSummary {
  state: WatchStateKind | string;
  heartbeat_at: string | null;
  cursor_at: string | null;
  age_seconds: number | null;
  stale_after_seconds: number | null;
  error: string | null;
}

/**
 * Frontend projection of B02/L03 `reliability-summary.v1`.
 * Incident counts are `number | null` so unavailable aggregates never become zeros.
 */
export interface HealthSummary {
  contract_version?: string | null;
  generated_at?: string | null;
  /** Null when open_events_state is unavailable — never invent 0. */
  open_critical: number | null;
  open_warning: number | null;
  open_info: number | null;
  open_events?: ReliabilityOpenEvents;
  open_events_state?: OpenEventsState | string;
  last_audit_status: string | null;
  last_audit_at: string | null;
  last_audit_kind?: string | null;
  last_audit_findings?: number | null;
  last_audit_state?: LastAuditState | string;
  last_audit?: Record<string, unknown> | null;
  watch_cursor_at: string | null;
  /** Cursor age in seconds (server-computed) or null if unknown/never run. */
  watch_cursor_age_s: number | null;
  watch_heartbeat?: string | null;
  watch?: ReliabilityWatchSummary | null;
  health: HealthState | string;
  degraded_reasons?: string[];
  incidents?: ReliabilityIncident[];
  mode?: AutonomyMode;
  max_auto_risk?: number;
}

/** L12 `GET /api/health` → `event_hub` operator-visible degraded surface. */
export interface EventHubStatus {
  contract_version?: number;
  state: "ok" | "degraded" | "unknown" | string;
  degraded: boolean;
  consecutive_failures?: number | null;
  last_error?: string | null;
  last_failure_at?: string | null;
  last_success_at?: string | null;
  tailer_alive?: boolean;
  tailer_restarts?: number | null;
  max_tailer_restarts?: number | null;
  subscriber_count?: number | null;
  degraded_after_failures?: number | null;
  restart_after_failures?: number | null;
}

/** L12 SSE frame `event: eventbus.status`. */
export interface EventBusStatusEvent {
  contract_version?: number;
  type: typeof EVENTBUS_STATUS_EVENT | string;
  state: "ok" | "degraded" | string;
  reason?: string | null;
  degraded: boolean;
  consecutive_failures?: number | null;
  last_error?: string | null;
  tailer_restarts?: number | null;
  max_tailer_restarts?: number | null;
  ts?: number | null;
}

export interface ApiReliabilityEvent {
  id: string;
  failure_class: string;
  severity: EventSeverity;
  signature: string;
  occurrence_key?: string;
  source: string;
  ref_type: string | null;
  ref_id: string | null;
  evidence_json: Record<string, unknown>;
  status: EventStatus;
  recovery_json: Record<string, unknown>;
  improvement_id: string | null;
  audit_id: string | null;
  detected_at: string;
  updated_at: string;
}

export interface ApiAudit {
  id: string;
  kind: string;
  status: string;
  window_start: string;
  window_end: string;
  stats_json: Record<string, unknown>;
  findings: number;
  report_note_path: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ApiVote {
  id: string;
  improvement_id: string;
  panel_attempt_id: string;
  judge_agent: string;
  model_family: ModelFamily;
  model: string;
  verdict: JudgeVerdict;
  scores_json: Record<string, number | unknown>;
  reasoning: string;
  conditions: string;
  created_at: string;
}

export interface ApiImprovement {
  id: string;
  origin: string;
  kind: string;
  title: string;
  summary: string;
  root_cause: string;
  proposal_json: Record<string, unknown>;
  risk_level: RiskLevel | number | null;
  status: ImprovementStatus | string;
  version?: number;
  ranking_score?: number;
  sandbox_json: Record<string, unknown>;
  votes_summary_json: Record<string, unknown>;
  votes?: ApiVote[];
  rollback_point_id: string | null;
  applied_sha: string | null;
  monitor_until: string | null;
  decided_by: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  applied_at: string | null;
  resolved_at: string | null;
  /** Optional before/after narrative from proposal_json or server enrichment. */
  before?: string | null;
  after?: string | null;
}

export interface ApiAutonomySetting {
  id: string;
  scope_type: AutonomyScopeType | string;
  scope_id: string;
  mode: AutonomyMode | string;
  max_auto_risk: number;
  updated_by: string;
  updated_at: string;
}

/** A single non-global scope entry within the GET /api/autonomy tree response. */
export interface ApiAutonomyScopeEntry {
  id: string;
  mode: AutonomyMode | string;
  max_auto_risk: number;
}

/**
 * Shape actually returned by GET /api/autonomy (see
 * omniagentos/api/routes/autonomy.py `get_autonomy`) — a tree, not a flat
 * setting row. `departments`/`agents`/`kinds` are scope-specific overrides
 * layered on top of `global`.
 */
export interface ApiAutonomyTree {
  global: { mode: AutonomyMode | string; max_auto_risk: number };
  departments: ApiAutonomyScopeEntry[];
  agents: ApiAutonomyScopeEntry[];
  kinds: ApiAutonomyScopeEntry[];
}

export interface ApiOrgUnit {
  id: string;
  name: string;
  kind: "company" | "department" | "team" | string;
  parent_id: string | null;
  charter: string;
  status: string;
  created_at: string;
  updated_at?: string;
  children?: ApiOrgUnit[];
  agents?: ApiOrgAgent[];
}

export interface ApiOrgAgent {
  id: string;
  name: string;
  model: string | null;
  org_unit_id: string | null;
  org_role: string;
  title: string;
  charter?: string;
  harness: string | null;
  enabled: number | boolean;
  vault_note_path?: string | null;
  created_at?: string;
  updated_at?: string;
  /** Optional sparkline series from scorecards (0–1 or raw KPI). */
  score_series?: number[];
  last_activity_at?: string | null;
}

export interface ApiOrgTree {
  roots: ApiOrgUnit[];
  agents?: ApiOrgAgent[];
}

export interface ApiAgentRequest {
  id: string;
  description: string;
  requested_by: string;
  status: string;
  design_json: Record<string, unknown>;
  improvement_id: string | null;
  agent_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiAgentActivity {
  runs: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  votes: Array<Record<string, unknown>>;
}

export interface ApiJudgePanelMember {
  family: ModelFamily;
  judge_agent: string;
  model: string;
  available: boolean;
  last_vote_at?: string | null;
}

export interface ApiJudgePanel {
  families: ApiJudgePanelMember[];
  quorum?: number;
  reseat_available?: boolean;
  /** Judge agents available as fallbacks if a primary seat is unavailable. */
  fallbacks?: string[];
}

/** A judge vote enriched with the improvement title for display in the judges list. */
export interface ApiJudgeVote extends ApiVote {
  improvement_title: string;
}

export interface ApiJudgeStats {
  judge_agent: string;
  model_family: ModelFamily;
  model?: string;
  votes: number;
  agreement_pct: number;
  override_pct: number;
  avg_reasoning_length: number;
}

export interface ApiJudgeStatsResponse {
  judges: ApiJudgeStats[];
}

export interface ApiVotesList {
  votes: ApiVote[];
}
