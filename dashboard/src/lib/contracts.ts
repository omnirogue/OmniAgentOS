/**
 * Shared TypeScript contract surface, originating in Wave 0.
 * Keep behavioral changes additive and covered by contract tests.
 */

// Browser requests always use the dashboard's same-origin Next proxy. The
// proxy is the only layer allowed to contact the loopback FastAPI service.
export const API_BASE = "";

/** SSE goes DIRECT to FastAPI, never through the Next proxy: a proxied
 * EventSource holds a long-lived upstream stream inside the Node process, and
 * client disconnects do not reliably cancel it — leaked relays accumulate
 * until the dashboard server wedges and every proxied read (GET /api/board)
 * times out. That exact outage happened twice; the browser's own EventSource
 * lifecycle against the product-scoped EVENTS_BASE (Grok default :8485;
 * Omni sibling uses :8484) is the designed path — see apiRoute.ts and
 * features/collab/hooks.ts. Override with NEXT_PUBLIC_EVENTS_BASE if needed. */
export const EVENTS_BASE =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_EVENTS_BASE) ||
  "http://127.0.0.1:8485";

export type TaskState =
  | "draft" | "ready" | "queued" | "running" | "awaiting_approval" | "validating"
  | "reviewing" | "revision_requested" | "completed" | "paused" | "failed"
  | "cancelled" | "retrying";

export type RunState =
  | "queued" | "running" | "awaiting_approval" | "validating" | "completed"
  | "failed" | "cancelled" | "paused";

export type StepStatus = "pending" | "started" | "completed" | "failed" | "compensated" | "skipped";
export type ApprovalState = "pending" | "approved" | "rejected" | "expired";
// DRIFT FIX (TN.0): "irreversible" was missing. It is the highest-risk member of
// contracts.ActionClass and the sole member of policy.HARD_STOP_CLASSES, so the
// mirror was omitting precisely the value that always requires a human.
export type ActionClass =
  | "read_only" | "sandboxed_creation" | "internal_reversible"
  | "external_reversible" | "consequential" | "irreversible";
export type Arm = "b0" | "b1" | "champion";
// DRIFT FIX (TN.0): "swarm" was missing (ledger-only, never adapter-resolved).
// DRIFT FIX: "cli-qwen" and "agentdeck" were missing — both are real adapters
// contracts.py declares, so a RunSummary carrying either was a value the backend
// emits and this type did not admit. Found by, and now pinned by,
// tests/api/test_dashboard_enum_mirror.py, which compares EVERY union in this
// file against its StrEnum instead of relying on the next drift being noticed by
// hand — which is how all three of these were found.
export type HarnessType =
  | "cli-claude" | "cli-codex" | "cli-grok" | "cli-kimi" | "cli-gemini"
  | "cli-qwen"
  | "mini-swe" | "openhands" | "agentdeck"
  | "fusion" | "improve" | "swarm" | "mock";

// --- TN.0 execution envelope. Mirrors omniagentos/contracts.py. Ordering is
// significant for ModelTier and ReasoningEffort (cheapest/lowest first).
export type ModelTier = "cheap" | "standard" | "strong" | "max";
export type ReasoningEffort = "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export type SandboxLevel = "read_only" | "workspace_write";
export type ScopeEnforcement = "off" | "observe" | "enforce";
export type AssessmentVerdict =
  | "pass" | "fail" | "blocked" | "needs_review" | "needs_replan";
export type TaskMode =
  | "code" | "report" | "content" | "image" | "video" | "intake_processing";
export type WorkItemMode = "build" | "report" | "image" | "campaign";
export type InteractionKind = "nudge" | "question" | "answer";
export type BlockingPolicy = "none" | "checkpoint" | "wait";
export type PlanApprovalState =
  | "not_required" | "pending" | "approved" | "rejected";

export const EVENT_TYPES = [
  "run.updated", "step.updated", "task.updated", "approval.requested",
  "approval.decided", "session.updated", "pause.changed", "audit.event", "worker.heartbeat",
  // V2-additive string event kinds (board + swarm activity) ride the same
  // /api/events stream.
  "board.updated", "task.message", "swarm.event",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface AgentUsage {
  wall_ms: number;
  turns: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  estimated: boolean;
  source: string;
}

export interface RunSummary {
  id: string;
  task_id: string;
  state: RunState;
  harness: HarnessType;
  arm: Arm | null;
  model: string | null;
  agent: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  cost_usd: number | null;
  usage_estimated: boolean;
}

export interface Receipt {
  key: string;
  run_id: string;
  step_name: string;
  result_json: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface RunDetail extends RunSummary {
  discipline_id: string | null;
  attempt: number;
  worker_id: string | null;
  harness_version: string;
  env_hash: string;
  plan_json: string;
  output_text: string | null;
  output_json: string | null;
  error: string | null;
  wall_ms: number | null;
  turns: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  usage_source: string;
  session_ref: string | null;
  cancel_requested: number;
  trace_id: string;
  manifest_path: string | null;
  vault_note_path: string | null;
  steps: StepRow[];
  events: EventRow[];
  artifacts: ArtifactRow[];
  approvals: Approval[];
  receipts: Receipt[];
}

export interface StepRow {
  id: number;
  run_id: string;
  seq: number;
  name: string;
  action_class: ActionClass;
  status: StepStatus;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface EventRow {
  id: number;
  ts: string;
  type: EventType | string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  trace_id: string;
}

export interface ArtifactRow {
  id: string;
  run_id: string;
  type: string;
  uri: string;
  sha256: string;
  bytes: number;
  created_at: string;
}

export interface TaskRow {
  id: string;
  discipline_id: string | null;
  title: string;
  state: TaskState;
  risk: string;
  created_at: string;
  updated_at: string;
}

export interface Approval {
  id: string;
  run_id: string | null;
  /** Nullable because session approvals are not associated with a run. */
  session_id?: string | null;
  task_id: string | null;
  step_seq: number | null;
  action_class: ActionClass;
  proposed_action: string;
  /** Serialized tool input when the approval originated from a session hook. */
  params_json?: string;
  risk: string;
  evidence: string;
  state: ApprovalState;
  decided_by: string | null;
  decision_note: string | null;
  decided_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export type SessionTodoStatus = "pending" | "in_progress" | "completed";

export interface SessionTodo {
  content: string;
  status: SessionTodoStatus;
}

export interface SessionProgress {
  done: number;
  total: number;
  /** 0..100 integer; 0 when total is 0. */
  pct: number;
}

/** Dashboard mirror of the Session Bridge session record (IF-8). */
export interface Session {
  id: string;
  source: "bridge" | "external";
  project_dir: string;
  provider: string;
  session_ref?: string | null;
  state: "starting" | "running" | "awaiting_approval" | "resuming" | "completed" | "failed" | "cancelled" | "killed";
  model: string | null;
  title: string | null;
  cost_usd: number | null;
  last_activity_at: string | null;
  created_at: string;
  approvals_requested?: number;
  approvals_granted?: number;
  approvals_denied?: number;
  /** Additive (task-detail live view): GET /api/sessions/{id} only — may be absent on list responses. */
  todos?: SessionTodo[];
  progress?: SessionProgress;
  stage?: string | null;
  files?: string[];
  /** Additive (scp-ui-0815): collector-assigned company slug/name and the
   * operator's manual correction. Both nullable; `company_override` wins
   * when present. Landing in parallel from a backend package — keep every
   * consumer tolerant of both being absent (older API responses). */
  company?: string | null;
  company_override?: string | null;
  /** Additive (scp-ui-0815): which local agent/lane is attached to this
   * session, if any. `agent_status` is typically "busy" | "idle" but is kept
   * open-string since other values may exist server-side. */
  agent_name?: string | null;
  agent_status?: "busy" | "idle" | (string & {}) | null;
  agent_profile?: string | null;
  /** Additive (scp-ui-0815, hooks package): surfaces sessions that need an
   * operator decision ("needs_input") or have just finished ("finished").
   * Absent/null means "nothing to flag" — never rendered as an error. */
  attention_state?: "needs_input" | "finished" | null;
  attention_reason?: string | null;
  attention_since?: string | null;
}

export interface PauseState {
  paused: boolean;
  reason: string;
  updated_at: string;
}

export interface Health {
  status: string;
  version: string;
  db: boolean;
  worker: { alive: boolean; last_beat_at: string | null };
}

export interface ApiError {
  error: { code: string; message: string; detail?: unknown };
}
