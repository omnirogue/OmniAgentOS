export type AgentStatus = "idle" | "busy" | "offline";
// `awaiting_approval` mirrors omniagentos/collab/contracts.py: live work that cannot
// advance until a human decides. Distinct from `blocked`, which means the run FAILED.
export type BoardTaskStatus = "pending" | "open" | "claimed" | "in_progress" | "awaiting_approval" | "blocked" | "done" | "cancelled";
export type ChannelKind = "direct" | "group" | "broadcast";
export type MessageKind = "message" | "handoff" | "status" | "claim" | "note";

export interface Agent {
  id: string;
  name: string;
  lineage: string;
  model: string | null;
  expertise: string[];
  trust_level: string;
  status: AgentStatus;
  created_at: string;
  updated_at: string;
}

export interface BoardTask {
  id: string;
  title: string;
  description: string;
  required_expertise: string[];
  discipline: string | null;
  priority: string;
  status: BoardTaskStatus;
  claimed_by: string | null;
  claim_version: number;
  result_ref: string | null;
  /** Additive: GET /api/board gains this; default false. Archived cards are excluded unless ?archived=1. */
  archived?: boolean;
  /** W5 longhaul additions: category ID, lane (auto|fast|longhaul), park state, attempt count */
  category_id?: string | null;
  lane?: string | null;
  park_state?: string | null;
  preflight_json?: string | null;
  /** The approval blocking this card, when its session is awaiting_approval.
   * `command` is the real command from params_json -- `proposed_action` is only
   * ever the tool name ("Bash"), which a human cannot decide on. */
  pending_approval?: {
    id: string;
    command: string;
    action_class: string;
    risk?: string | null;
    created_at?: string | null;
  } | null;
  attempt_count?: number;
  created_at: string;
  updated_at: string;
  // --- Team Work OS (migration 123) additions. Every field is optional: an
  // agent card carries none of them, and the API's own model documents them
  // as "None = the card is not yet tied to a goal" (etc.) rather than a
  // guessed default, so the client must not invent one either.
  /** Parent card id — depth is capped at ONE (a parent has no parent). */
  parent_task_id?: string | null;
  /** company_goals.id — the ladder from work to why. */
  goal_id?: string | null;
  /** org_companies.slug via the card's goal (LEFT JOIN, multi-company Work OS
   * 2026-08-13); null/absent = the card is not tied to a company. */
  company_slug?: string | null;
  /** The PERSON accountable (agents use `claimed_by` instead). */
  owner_employee_id?: string | null;
  /** External handle (PR/issue/doc key); unique when present. */
  ref?: string | null;
  /** S | M | L. */
  size?: string;
  /** Non-empty is what makes a card verifiable. */
  acceptance_criteria?: string;
  /** Stamped only by `POST /api/team/tasks/{id}/verify` — never PATCH-able. */
  verified_at?: string | null;
  verified_by?: string | null;
  due_date?: string | null;
  /** standup | transcript | customer_reply | ... */
  source?: string;
}

/**
 * A board card enriched with the live state of its linked control-plane run
 * (GET /api/board). `run_id` is null for hand-created cards with no executor.
 */
/** Multidimensional org envelope (migration 061 / orgdims). */
export type BoardOrgDimensions = {
  organization_context?: {
    company_id?: string | null;
    company_slug?: string | null;
    product_id?: string | null;
    product_slug?: string | null;
    initiative_id?: string | null;
    goal_ids?: string[];
    epic_id?: string | null;
  };
  classification?: {
    primary_workstream?: string | null;
    domains?: string[];
    channels?: string[];
    lifecycle?: string | null;
    priority?: string | null;
    risk_class?: string | null;
  };
  execution_links?: {
    preferred_agent_profiles?: string[];
    loop_template_id?: string | null;
    skill_ids?: string[];
  };
  provenance?: {
    source?: string;
    confidence?: number;
    rationale?: string;
    classifier_agent?: string | null;
    evidence_refs?: string[];
  };
};

/** Provenance link back to the chat that promoted this card (S1 promote). */
export type BoardChatOrigin = {
  chat_id: string;
  title?: string | null;
};

/** Per-card checklist summary surfaced on the card face — the todolist count. */
export type BoardChecklist = {
  done: number;
  total: number;
};

/**
 * Why a card is stuck — a human-authored reason when the owner recorded one
 * (`reason: "human"`, `source: "owner"`, detail = board_tasks.blocked_reason),
 * else a read-only projection of the lane's attempt ledger (falling back to
 * the linked run's own error text).
 *
 * Server contract (omniagentos/intake/service.py::_blocked_reason_map): ONLY
 * ever present on cards whose status is `blocked`. Inferred entries only ever
 * name a FAILURE — an attempt that ended `completed`/`split`/`rerouted`/
 * `superseded` is dropped rather than reported as a reason. So the UI may
 * render it as "this is why it stopped" without qualification.
 */
export type BoardBlockedReason = {
  /** The lane's own end-reason token, e.g. `timeout`, `error`, `budget`. */
  reason: string;
  /** First non-empty line of the failure detail, server-truncated. */
  detail: string | null;
  /** Which ledger/work kind produced it (`longhaul`, `swarm`, `run`, …). */
  source: string | null;
  at: string | null;
};

export interface LiveBoardTask extends BoardTask {
  run_id: string | null;
  run_state: string | null;
  run_agent: string | null;
  run_progress: { steps_done: number; steps_total: number } | null;
  run_error: string | null;
  paused_run: string | null;
  paused_session: string | null;
  /** Additive live work contract; absent on older board API responses. */
  work?: BoardWork | null;
  /** Multidimensional organization / classification chips. */
  org?: BoardOrgDimensions | null;
  /** Project association for project-scoped board views (?project=<id>). */
  project_id?: string | null;
  /** Link back to the chat that promoted this card onto the board.
   * Absent on cards that were not created via chat promotion — the card
   * renders no badge when this field is missing (silent degradation). */
  chat_origin?: BoardChatOrigin | null;
  /** Planner brief / TaskContract summary rendered when the card is expanded.
   * Sourced from intake board payloads; absent on cards with no planner output. */
  planner_brief?: string | null;
  /** Live checklist summary (n/m done) rendered on the card face.
   * Sourced from the activity checklist; absent when no checklist exists. */
  checklist?: BoardChecklist | null;
  /** Why a blocked card is stuck. Null on every card that is not `blocked`,
   * and on blocked cards whose lane never recorded an end reason. */
  blocked_reason?: BoardBlockedReason | null;
}

export type BoardWork = {
  kind: "run" | "session" | "orchestration" | null;
  state: string | null;
  agent: string | null;
  steps_done: number;
  steps_total: number;
  current_step: string | null;
  files_count: number | null;
  cost_usd: number | null;
  last_activity_at: string | null;
  error: string | null;
};

export type BoardFile = {
  rel: string;
  size: number;
  mtime: number;
  kind: "upload" | "output" | "session_file" | string;
};

export type BoardFilesResponse = {
  workspace: string | null;
  files: BoardFile[];
};

export interface ArchiveTasksResult {
  archived: string[];
  skipped: Array<{ id: string; reason: string }>;
  paused_runs: string[];
  paused_sessions: string[];
}

export interface Channel {
  id: string;
  name: string;
  kind: ChannelKind;
  members: string[];
  topic: string;
  created_at: string;
}

export interface Message {
  id: string;
  channel_id: string;
  from_agent: string;
  to_agent: string | null;
  kind: MessageKind;
  body: string;
  ref: string | null;
  ts: string;
}

export interface ClaimResult {
  success: boolean;
  claimed_by?: string;
  claim_version?: number;
  error?: string;
  conflict?: boolean;
}

// W5 Longhaul types
export interface TaskCategory {
  id: string;
  name: string;
  slug: string;
  color: string;
  wip_limit: number;
  created_at: string;
  updated_at: string;
}

export interface TaskTurn {
  id: string;
  scope_type: string;
  scope_id: string;
  seq: number;
  role: "steering" | "status" | "handoff" | "system";
  content: string;
  created_at: string;
  meta_json?: Record<string, unknown>;
  delivery?: { session_id?: string; delivered_at?: string; pending?: boolean };
}

export interface TaskAttempt {
  id: string;
  board_task_id: string;
  seq: number;
  session_id: string | null;
  harness: string;
  model: string;
  account_id: string | null;
  started_at: string;
  ended_at: string | null;
  end_reason: string | null;
  detail: string;
  tier?: string | null;
}

export interface LonghaulTaskDetail {
  attempts: TaskAttempt[];
  workbook_content: string;
  acceptance: string;
  park_state: string | null;
  current_worker?: { model: string; harness: string; account?: string } | null;
}

/** One session working (or having worked) a board card, from
 * GET /api/board/{task_id}/sessions — the unified lookup across the
 * orchestrate / swarm / direct execution paths. */
export interface BoardTaskSession {
  id: string;
  state: string;
  model: string | null;
  provider: string | null;
  title: string | null;
  created_at: string | null;
  updated_at: string | null;
  source: "orchestration" | "swarm" | "direct";
  step_seq?: number | null;
  step_title?: string | null;
  step_status?: string | null;
  tier?: string | null;
  effort?: string | null;
  end_reason?: string | null;
  detail?: string | null;
}

export interface BoardTaskSessions {
  sessions: BoardTaskSession[];
  orchestration: { id: string; status: string; stage: string | null; error: string | null } | null;
  live_session_id: string | null;
}

/** A `note` row correcting an earlier ledger row — attached BY THE CLI (never
 * recomputed client-side) to the row it corrects, from `refs.corrects`. */
export interface LedgerCorrection {
  id: string;
  summary: string;
  at: string;
}

/** One row from GET /api/ledger/tail, relayed verbatim from the session-
 * ledger CLI (~/.omniagentos/ops/bin/ledger) — see session-ledger PLAN.md §3 for the
 * full row schema. The card drawer's event timeline renders
 * `at · agent · event · summary`, corrections attached. */
export interface LedgerEvent {
  v: number;
  at: string;
  ts: string;
  session: string;
  agent: string;
  event: string;
  project: string;
  summary: string;
  refs?: Record<string, string>;
  visibility?: "project" | "estate" | "owner";
  backfilled?: boolean;
  prev?: string | null;
  id: string;
  _corrections?: LedgerCorrection[];
}

/** One OPEN claim from GET /api/ledger/claims — the board header's
 * open-claims strip (`project/surface — session`, stale marker). Called
 * with no owner-related flag at all (pre-merge review decision, 2026-08-04):
 * a owner-held surface is shown, not omitted (hiding it would present the
 * surface as FREE) -- `visibility: "owner"` marks these; only any attached
 * correction TEXT is redacted, by the CLI itself, before this ever reaches
 * the browser. The strip renders every claim regardless of `visibility`. */
export interface LedgerClaim {
  project: string;
  surface: string | null;
  session: string;
  agent: string;
  since: string;
  lease_until: string | null;
  stale: boolean;
  id: string;
  visibility?: "project" | "estate" | "owner";
  _corrections?: LedgerCorrection[];
}
