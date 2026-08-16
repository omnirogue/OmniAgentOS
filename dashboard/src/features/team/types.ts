/**
 * Team Work OS (P5) — wire shapes for `/api/team/*` (see
 * omniagentos/api/routes/team.py + omniagentos/team/{contracts,store}.py).
 *
 * `GET /api/team/board`'s per-card shape (`QueueCard`) is deliberately
 * minimal server-side — id/title/ref/status/size only. Priority,
 * owner_employee_id, verified_at and blocked_reason are NOT on that response
 * (they live on the richer `GET /api/board` reconciled card instead, see
 * `features/collab/types.ts`'s `BoardTask` additions); `TeamBoard` enriches
 * queue cards with that richer shape client-side rather than the store
 * inventing fields the API does not send.
 */

/** The four seed employees (`omniagentos/company_goals/seed_employees.py`).
 * `TeamBoard` sections the operator/Alice/Bob explicitly per the P5 brief; anyone
 * else the roster returns (Frank today) folds into "Agents & unowned". */
export const NAMED_EMPLOYEE_IDS = ["emp_owner", "emp_alice", "emp_bob"] as const;

export const EMPLOYEE_NAMES: Record<string, string> = {
  emp_owner: "the operator",
  emp_frank: "Frank",
  emp_bob: "Bob",
  emp_alice: "Alice",
};

/** Display name for an owner id, falling back to the raw id for an employee
 * the client-side map does not know about yet (never blank). */
export function employeeName(id: string | null | undefined): string | null {
  if (!id) return null;
  return EMPLOYEE_NAMES[id] ?? id;
}

export interface TeamQueueCard {
  id: string;
  title: string;
  ref: string | null;
  status: string;
  size: string;
  /** Optional server-sent priority (the `/api/team/board` shape is being
   * widened in a sibling package). When absent, `TeamMiniCard` falls back
   * to the reconciled-board enrichment instead of hiding the chip. */
  priority?: string;
  // Additive wire fields (multi-company Work OS, 2026-08-13). All optional:
  // an un-upgraded server omits them and the UI degrades to the old card.
  owner_employee_id?: string | null;
  company_slug?: string | null;
  company_name?: string | null;
}

export interface TeamQueueCounts {
  ready: number;
  active: number;
  blocked: number;
  review: number;
  done_today: number;
}

export interface TeamQueueBuckets {
  employee_id: string;
  ready: TeamQueueCard[];
  active: TeamQueueCard[];
  blocked: TeamQueueCard[];
  review: TeamQueueCard[];
  done_today: TeamQueueCard[];
  counts: TeamQueueCounts;
  ready_below_5: boolean;
  active_below_5?: boolean;
}

export interface TeamQueuePool {
  cards: TeamQueueCard[];
  depth: number;
  low: boolean;
  truncated?: boolean;
}

/** Parsed GET /api/team/board response; raw data is validated at the hook boundary. */
export interface TeamBoardResponse {
  pool: TeamQueuePool | null;
  buckets: Record<string, TeamQueueBuckets>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Strip the additive per-card fields when they arrive with the wrong type,
 * so a bad payload degrades exactly like an old server that omits them. */
function sanitizeQueueCard(card: TeamQueueCard): TeamQueueCard {
  try {
    const parsed = { ...card };
    if (typeof parsed.owner_employee_id !== "string") delete parsed.owner_employee_id;
    if (typeof parsed.priority !== "string") delete parsed.priority;
    if (typeof parsed.company_slug !== "string") delete parsed.company_slug;
    if (typeof parsed.company_name !== "string") delete parsed.company_name;
    return parsed;
  } catch {
    // A hostile getter on the raw payload: pass the card through untouched so
    // the render-level ErrorBoundary owns the failure, as before the widening.
    return card;
  }
}

export function parseTeamBoard(raw: unknown): TeamBoardResponse {
  if (!isRecord(raw)) return { pool: null, buckets: {} };
  const rawPool = raw.pool;
  let pool: TeamBoardResponse["pool"] = null;
  if (isRecord(rawPool) && Array.isArray(rawPool.cards) && typeof rawPool.depth === "number" && typeof rawPool.low === "boolean") {
    const cards = rawPool.cards
      .filter(
        (card): card is TeamQueuePool["cards"][number] =>
          isRecord(card) && typeof card.id === "string" && typeof card.title === "string",
      )
      .map(sanitizeQueueCard);
    pool = {
      cards, depth: rawPool.depth, low: rawPool.low,
      ...(typeof rawPool.truncated === "boolean" ? { truncated: rawPool.truncated } : {}),
    };
  }
  const bucketSource = isRecord(raw.buckets) ? raw.buckets : raw;
  const buckets = Object.fromEntries(
    Object.entries(bucketSource).filter(([, value]) =>
      isRecord(value) && isRecord(value.counts) &&
      ["ready", "active", "blocked", "review", "done_today"].every((key) => Array.isArray(value[key])),
    ),
  ) as Record<string, TeamQueueBuckets>;
  return { pool, buckets };
}

// --- Company → goal → task → subtask tree (GET /api/team/tree) ------------

export interface TeamTreeTask {
  id: string;
  ref: string | null;
  title: string;
  status: string;
  owner_employee_id: string | null;
  size: string;
  verified_at: string | null;
  subtasks: TeamTreeTask[];
}

export interface TeamTreeGoal {
  id: string;
  org_company_id: string;
  title: string;
  horizon: string;
  parent_goal_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  tasks: TeamTreeTask[];
}

export interface TeamTreeCompany {
  id: string;
  slug: string;
  name: string;
  status: string;
  created_at: string;
  goals: TeamTreeGoal[];
}

export interface TeamTree {
  companies: TeamTreeCompany[];
}

// --- Evidence + events (GET/PATCH /api/team/evidence, /tasks/{id}/*) ------

export const EVIDENCE_KINDS = [
  "commit", "pr", "review", "session", "test_run", "deploy",
  "doc", "customer_reply", "research", "note",
] as const;

/** Groups the Evidence tab renders under — one bucket per related kind, plus
 * a catch-all so a kind added server-side later still renders somewhere. */
export const EVIDENCE_GROUPS: Array<{ id: string; label: string; kinds: readonly string[] }> = [
  { id: "commits", label: "Commits", kinds: ["commit"] },
  { id: "prs", label: "PRs", kinds: ["pr", "review"] },
  { id: "sessions", label: "Sessions", kinds: ["session"] },
  { id: "docs", label: "Docs", kinds: ["doc", "research", "customer_reply"] },
  { id: "other", label: "Other", kinds: ["test_run", "deploy", "note"] },
];

export interface TeamEvidence {
  id: string;
  task_id: string | null;
  kind: string;
  ref: string;
  repo: string;
  actor: string;
  title: string;
  attribution: string;
  confidence: number;
  quality_gate: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface TeamEvent {
  id: string;
  task_id: string;
  actor: string;
  event: string;
  from_status: string | null;
  to_status: string | null;
  note: string;
  created_at: string;
}

/** The raw `board_tasks` row POST /verify + /unverify return — richer than
 * the reconciled board shape (every Team Work OS column, unfiltered). */
export interface TeamTaskRow {
  id: string;
  status: string;
  owner_employee_id: string | null;
  verified_at: string | null;
  verified_by: string | null;
  [key: string]: unknown;
}

// --- Scoreboard (GET /api/team/scoreboard) --------------------------------

export interface TeamScoreboardStat {
  score: number;
  baseline_points: number;
  production_x: number | null;
  pct_to_10x: number | null;
}

export interface TeamScoreboardPersonStat extends TeamScoreboardStat {
  employee_id: string;
  counted?: Array<Record<string, unknown>>;
  excluded?: Array<Record<string, unknown>>;
}

export interface TeamScoreboardResponse {
  people: TeamScoreboardPersonStat[];
  team: TeamScoreboardStat;
  period: { start: string; end: string };
  score_version: string;
}

// --- Developer accountability (migration 132, GET /api/team/accountability,
// GET/POST/PATCH /api/team/commitments, POST /tasks/{id}/verify outcome) ----

/** POST /api/team/tasks/{id}/verify body. `outcome` defaults server-side to
 * "pass" (every pre-131 caller sends none) — `reason` is REQUIRED for a fail
 * and the 400 names that. */
export interface VerifyTaskBody {
  verifier: string;
  outcome?: "pass" | "fail";
  reason?: string;
}

/** The tri-state a DONE card is in — mirrors `omniagentos.team.store.
 * completion_state` exactly (module-level there BECAUSE the API, the
 * dashboard badge, the 07:00 report and `commitments.resolve_day` must never
 * disagree about which state a card is in; this is the one client-side
 * derivation, from the same three inputs). A card that is not `done` has no
 * completion state: `null`, never a favourable "unverified". */
export type CompletionState = "verified" | "failed_verification" | "unverified" | null;

/** Migration 132 widened `board_tasks` with the automation-maturity axis and
 * the verification-failure stamps. `LiveBoardTask` (`features/collab/types.ts`)
 * is out of this package's ownership and has not been widened for them, so
 * `TaskOverview` reads/writes these five columns through this narrow shape
 * (a local cast) instead of touching that type. */
export interface TeamTaskAccountabilityFields {
  status?: string;
  verified_at?: string | null;
  verified_by?: string | null;
  automation_maturity?: string | null;
  automation_note?: string | null;
  verification_failed_at?: string | null;
  verification_failed_by?: string | null;
  verification_failed_reason?: string | null;
}

export function completionStateOf(task: TeamTaskAccountabilityFields): CompletionState {
  if (task.status !== "done") return null;
  if (task.verified_at) return "verified";
  if (task.verification_failed_at) return "failed_verification";
  return "unverified";
}

/** Vocabulary is app-side validated (`CollabStore.update_board_task`, like
 * `priority`), nullable = untracked — sending `""` is rejected server-side, so
 * callers must translate the "—" option to `null` before PATCHing. */
export const AUTOMATION_MATURITY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "—" },
  { value: "human", label: "Human" },
  { value: "assisted", label: "Assisted" },
  { value: "partially_automated", label: "Partially automated" },
  { value: "autonomous", label: "Autonomous" },
  { value: "autonomous_verified", label: "Autonomous + verified" },
];

/** One `team_commitments` row (migration 132) — verbatim `SELECT *` shape
 * returned by every commitments endpoint and embedded in `/accountability`. */
export interface TeamCommitment {
  id: string;
  day: string;
  employee_id: string;
  task_id: string | null;
  kind: "task" | "improvement";
  title: string;
  expected_outcome: string;
  status: "committed" | "delivered" | "missed" | "carried";
  source: "auto" | "operator" | "self";
  carried_from: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string;
  created_at: string;
  updated_at: string;
}

/** Per-card evidence DETAIL embedded in `/accountability`'s `done_today` —
 * never a bare count (review S12). */
export interface TeamAccountabilityEvidenceItem {
  kind: string;
  repo: string;
  ref: string;
  quality_gate: string;
}

export interface TeamAccountabilityDoneCard {
  id: string;
  ref: string | null;
  title: string;
  size: string;
  completion_state: CompletionState;
  automation_maturity: string | null;
  automation_note: string | null;
  verification_failed_reason: string | null;
  evidence: TeamAccountabilityEvidenceItem[];
}

/** Blocked cards on `/accountability`. `blocked_reason` is landing alongside
 * this fix (Sol@high cross-review, 2026-08-14) — kept OPTIONAL so the client
 * degrades to the id/ref/title-only tooltip against a server that has not
 * rolled it out yet, same "additive wire field" convention `TeamQueueCard`
 * already uses. */
export interface TeamAccountabilityBlockedCard {
  id: string;
  ref: string | null;
  title: string;
  blocked_reason?: string | null;
}

export interface TeamAccountabilityPace {
  points: number;
  floor: number;
  prorated_target: number;
  on_pace: boolean;
}

export interface TeamAccountabilityPerson {
  employee_id: string;
  name: string;
  commitments: TeamCommitment[];
  improvement_of_day: TeamCommitment | null;
  /** `{}` when the person has no queue bucket yet — treat any count as optional. */
  counts: Partial<TeamQueueCounts>;
  done_today: TeamAccountabilityDoneCard[];
  blocked: TeamAccountabilityBlockedCard[];
  overdue: number;
  learning_captures: number;
  /** Landing alongside `blocked_reason` (Sol@high cross-review, 2026-08-14) —
   * OPTIONAL for the same un-upgraded-server degrade as above. */
  evidence_today?: number;
  /** `null` when pace could not be computed — "unmeasured" and "zero" are
   * different answers, and only one of them is an accusation. */
  points_pace: TeamAccountabilityPace | null;
}

export interface TeamAccountabilityResponse {
  day: string;
  people: TeamAccountabilityPerson[];
}

/** POST /api/team/nl-assign response — a DISCRIMINATED UNION (automation
 * backlog, 2026-08-14): the route now returns either an ASSIGNMENT (an
 * owner, no `kind`) or a PROPOSAL (`kind: "automation_proposal"`, no owner —
 * it lands in `awaiting_approval` for the operator). The assignment variant carries no
 * `kind` field on the wire at all — `kind?: undefined` here is what lets
 * `result.kind === "automation_proposal"` narrow correctly in both
 * directions without a runtime shape-sniff. The composer intercept (M6)
 * renders a different confirmation per variant — see `ChatSurface`. */
export interface TeamNlAssignAssignment {
  kind?: undefined;
  task_id: string;
  owner_employee_id: string;
  title: string;
  acceptance_criteria: string;
  goal_id: string | null;
  due_date: string | null;
  message: string;
}

export interface TeamNlAssignProposal {
  kind: "automation_proposal";
  task_id: string;
  title: string;
  category: string | null;
  assignee_hint: string | null;
  goal_id: string | null;
  acceptance_criteria: string;
  status: string;
  message: string;
}

export type TeamNlAssignResult = TeamNlAssignAssignment | TeamNlAssignProposal;
