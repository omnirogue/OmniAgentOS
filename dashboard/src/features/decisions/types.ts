/**
 * Types for the Executive Decision Center (EDC) "Decisions" Inbox tab.
 *
 * These mirror the §10.2 WIRE CONTRACT in devtasks/decision-center-0813/synthesis.md
 * — they are built against that contract, NOT against a live backend (the FastAPI
 * routes in omniagentos/edc are a separate lane landing in parallel). Verify against
 * omniagentos/api/routes/decisions.py once it lands before changing a field here.
 *
 * PRIVACY (locked, §8): a Decision is ALWAYS owner-scoped server-side via the
 * request principal. There is deliberately NO owner/principal field on this type and
 * the client NEVER sends one — the owner is resolved from the signed browser session
 * by the Next proxy + FastAPI, never asserted by the browser.
 */
import type { RiskClass } from "../steward/types";

/** Classification the FE ever sees. The `ignore` class and `suppressed` rows are
 * NEVER serialized to the owner-scoped list (§2.2), so they are absent here. */
export const DECISION_CLASSIFICATIONS = ["urgent", "needs_owner", "maybe"] as const;
export type DecisionClassification = (typeof DECISION_CLASSIFICATIONS)[number];

/** Lifecycle status the FE serializes (the §2.2 canonical vocabulary minus
 * `suppressed`, which never reaches the owner list). */
export const DECISION_STATUSES = [
  "open",
  "snoozed",
  "draft_pending",
  "awaiting_approval",
  "in_progress",
  "done_unverified",
  "done_verified",
  "dismissed",
  "denied",
  "expired",
] as const;
export type DecisionStatus = (typeof DECISION_STATUSES)[number];

/** The nine first-class resolution verbs (§5/§6). The server curates which of
 * these appear in `available_actions` per status + permission matrix; the FE
 * renders EXACTLY what the server sends and never computes eligibility itself. */
export const DECISION_ACTIONS = [
  "execute",
  "delegate",
  "defer",
  "reply",
  "approve",
  "deny",
  "edit",
  "snooze",
  "dismiss",
] as const;
export type DecisionAction = (typeof DECISION_ACTIONS)[number];

export const DECISION_CLASSIFIER_KINDS = ["deterministic", "rule", "llm", "llm_unavailable"] as const;
export type DecisionClassifier = (typeof DECISION_CLASSIFIER_KINDS)[number];

/** The REQUIRED recommended action (invariant 1). `human_line` is the operator-
 * facing sentence. An empty `human_line` is a contract violation the FE surfaces
 * as a LOUD sentinel — never a blank card. */
export interface RecommendedAction {
  kind: string;
  human_line: string;
  params: Record<string, unknown>;
  /** True when the wire delivered no concrete recommendation and `human_line`
   * carries the "no recommendation attached" sentinel (invariant-1 visibility). */
  missing: boolean;
}

/** A single server-curated action offer. `target` disambiguates variants the
 * matrix distinguishes (e.g. defer → `queue` | `machine`); `label` is the exact
 * button text. The FE renders these verbatim in the order received. */
export interface AvailableAction {
  action: DecisionAction;
  label: string;
  target: string | null;
}

/** Server-curated delegate target (from the /task permission matrix). No free
 * text — the picker only offers who the server says this owner may assign to. */
export interface DecisionAssignee {
  employee_id: string;
  name: string;
}

/** Server-computed snooze preset. `past_deadline` arms the danger banner +
 * acknowledgement friction (§6.5) — the FE never computes deadline math. */
export interface SnoozeOption {
  label: string;
  until: string;
  past_deadline: boolean;
}

/** The draft reply, present ONLY while `status === "draft_pending"`. The sole
 * send affordance on the FE is approving this draft with its `sha256` (§6.4). */
export interface DecisionDraft {
  to: string;
  subject: string;
  body: string;
  sha256: string;
  approved_sha256: string | null;
  approved_at: string | null;
}

/** Honest outcome state on a resolved card (§7): activity ≠ outcome. A green
 * "done" is shown ONLY for a verified outcome, never for `done_unverified`. */
export interface DecisionVerification {
  state: string;
  detail: string;
}

export interface Decision {
  id: string;
  /** Monotonic human handle; the delegated card ref is `EDC-<number>`. */
  number: number;
  source: string;
  source_account: string;
  occurred_at: string | null;
  title: string;
  context: string;
  counterparty: string;
  classification: DecisionClassification;
  consequence: string;
  deadline_at: string | null;
  confidence: number;
  reason: string;
  classifier: DecisionClassifier;
  /** Risk tier for the execute path's confirmation friction (§10.4). */
  risk_class: RiskClass;
  recommended: RecommendedAction;
  available_actions: AvailableAction[];
  status: DecisionStatus;
  resolution: string | null;
  decided_by: string | null;
  decided_at: string | null;
  notes: string;
  tags: string[];
  /** `EDC-<number>` once delegated to a collab-board card, else "". */
  task_ref: string;
  assignees: DecisionAssignee[];
  suggested_snoozes: SnoozeOption[];
  draft: DecisionDraft | null;
  verification: DecisionVerification | null;
  created_at: string;
  updated_at: string;
}

/** Grouping the tab renders (§10): urgent + needs-the operator are the visible queue and
 * drive the badge; maybe + snoozed are collapsed sections excluded from the badge. */
export interface DecisionGroups {
  urgent: Decision[];
  needsOwner: Decision[];
  maybe: Decision[];
  snoozed: Decision[];
}

/** Body of the ONE generic decide mutation — POST /api/decisions/{id}/decide.
 * `action` must be one the server offered in `available_actions`; the server
 * stamps `decided_by` from the principal (never sent here). */
export interface DecideBody {
  action: DecisionAction;
  note?: string;
  tags?: string[];
  params?: DecideParams;
}

export interface DecideParams {
  /** snooze */
  until?: string;
  acknowledge_deadline?: boolean;
  /** delegate */
  assignee?: string;
  /** reply */
  intent?: string;
  /** edit */
  edited_recommendation?: string;
  reclassify?: DecisionClassification;
  /** approve (of a draft reply) — voids on any edit that changes the sha */
  draft_sha256?: string;
  /** defer */
  defer_target?: string;
  /** consequential execute */
  confirm?: boolean;
}
