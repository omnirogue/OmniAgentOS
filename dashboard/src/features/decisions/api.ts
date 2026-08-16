/** Defensive fetch client for the Executive Decision Center (EDC) API surface.
 *
 * Built against the §10.2 WIRE CONTRACT (devtasks/decision-center-0813/synthesis.md),
 * not a live backend — the FastAPI routes (omniagentos/edc, a parallel lane) are the
 * integration point and are FAIL-CLOSED until they land: without the BE route the GET
 * 404s/errors here and the tab renders its error state (never a leak, never a crash).
 *
 * LITERAL paths (add these two to tests/api/test_dashboard_contract.py ONCE the BE
 * routes are mounted — adding them before would 404 the contract test):
 *   GET  /api/decisions
 *   POST /api/decisions/{id}/decide
 *
 * PRIVACY: per-user scoping is entirely SERVER-SIDE via the request principal
 * (§8). No function here sends an owner/principal param — reads ride `proxyRead`
 * (the `"decisions"` allowlist entry in app/api/[...path]/route.ts attaches the
 * principal) and mutations ride `proxyAuthorized`. */
import { getJson, postJson, StewardApiError } from "../steward/api";
import { RISK_CLASSES, type RiskClass } from "../steward/types";
import {
  DECISION_ACTIONS,
  DECISION_CLASSIFICATIONS,
  DECISION_CLASSIFIER_KINDS,
  DECISION_STATUSES,
  type AvailableAction,
  type DecideBody,
  type Decision,
  type DecisionAction,
  type DecisionAssignee,
  type DecisionDraft,
  type DecisionVerification,
  type RecommendedAction,
  type SnoozeOption,
} from "./types";

/** Reuse the steward error class so callers get a single `status`-carrying error
 * type (404 → foreign/absent id, 409 → already handled by Slack or another tab). */
export { StewardApiError as DecisionApiError };

/** Shown verbatim when a Decision arrives with no concrete recommendation —
 * invariant 1 must be VISIBLE, never a blank card (§10.1/§10.4). */
export const NO_RECOMMENDATION_SENTINEL = "⚠ No recommendation attached — this should never happen; review before acting.";

// --- primitive coercions (mirrors steward/api.ts's defensive style) ----------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}
function nullableText(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}
function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
function integer(value: unknown, fallback = 0): number {
  return Number.isInteger(value) ? (value as number) : fallback;
}
function boolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}
function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}
function unknownArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function oneOf<T extends readonly string[]>(value: unknown, values: T, fallback: T[number]): T[number] {
  return typeof value === "string" && (values as readonly string[]).includes(value) ? (value as T[number]) : fallback;
}
function mostRestrictiveRiskClass(): RiskClass {
  return RISK_CLASSES[RISK_CLASSES.length - 1]!;
}

// --- normalization ------------------------------------------------------------

/** Invariant-1 enforcement in the UI: a missing/blank `human_line` becomes a
 * loud sentinel and `missing: true`, so the card renders a visible warning
 * rather than a blank recommended-action block. */
export function normalizeRecommended(value: unknown): RecommendedAction {
  const rec = isRecord(value) ? value : {};
  const humanLine = text(rec.human_line).trim();
  return {
    kind: text(rec.kind),
    human_line: humanLine || NO_RECOMMENDATION_SENTINEL,
    params: record(rec.params),
    missing: humanLine.length === 0,
  };
}

/** Accepts either the canonical object form ({action,label,target}) or a bare
 * string ("edit" or "defer:queue") so a terse wire is tolerated. Unknown action
 * verbs are dropped — the FE only ever offers a verb it can render. */
export function normalizeAvailableAction(value: unknown): AvailableAction | null {
  if (typeof value === "string") {
    const [verb, target] = value.split(":", 2);
    if (!(DECISION_ACTIONS as readonly string[]).includes(verb ?? "")) return null;
    return { action: verb as DecisionAction, label: labelFor(verb as DecisionAction, target ?? null), target: target ?? null };
  }
  if (!isRecord(value)) return null;
  const verb = text(value.action);
  if (!(DECISION_ACTIONS as readonly string[]).includes(verb)) return null;
  const target = nullableText(value.target);
  return { action: verb as DecisionAction, label: text(value.label).trim() || labelFor(verb as DecisionAction, target), target };
}

/** Canonical fallback labels; the server SHOULD supply `label`, but a terse
 * string form still renders a sensible button. */
function labelFor(action: DecisionAction, target: string | null): string {
  const base: Record<DecisionAction, string> = {
    execute: "Execute",
    delegate: "Delegate",
    defer: "Defer",
    reply: "Reply",
    approve: "Approve",
    deny: "Deny",
    edit: "Edit",
    snooze: "Snooze",
    dismiss: "Dismiss",
  };
  if (action === "defer" && target) return target === "machine" ? "Defer to machine" : "Defer to queue";
  return base[action];
}

function normalizeAssignee(value: unknown): DecisionAssignee | null {
  if (!isRecord(value)) return null;
  const id = text(value.employee_id);
  if (!id) return null;
  return { employee_id: id, name: text(value.name) || id };
}

function normalizeSnooze(value: unknown): SnoozeOption | null {
  if (!isRecord(value)) return null;
  const until = text(value.until);
  if (!until) return null;
  return { label: text(value.label) || until, until, past_deadline: boolean(value.past_deadline) };
}

function normalizeDraft(value: unknown): DecisionDraft | null {
  if (!isRecord(value)) return null;
  return {
    to: text(value.to),
    subject: text(value.subject),
    body: text(value.body),
    sha256: text(value.sha256),
    approved_sha256: nullableText(value.approved_sha256),
    approved_at: nullableText(value.approved_at),
  };
}

function normalizeVerification(value: unknown): DecisionVerification | null {
  if (!isRecord(value)) return null;
  const state = text(value.state);
  if (!state) return null;
  return { state, detail: text(value.detail) };
}

export function normalizeDecision(value: unknown): Decision {
  const rec = isRecord(value) ? value : {};
  return {
    id: text(rec.id),
    number: integer(rec.number),
    source: text(rec.source),
    source_account: text(rec.source_account),
    occurred_at: nullableText(rec.occurred_at),
    title: text(rec.title),
    context: text(rec.context),
    counterparty: text(rec.counterparty),
    classification: oneOf(rec.classification, DECISION_CLASSIFICATIONS, "maybe"),
    consequence: text(rec.consequence),
    deadline_at: nullableText(rec.deadline_at),
    confidence: number(rec.confidence),
    reason: text(rec.reason),
    classifier: oneOf(rec.classifier, DECISION_CLASSIFIER_KINDS, "deterministic"),
    // An unrecognized/absent risk class fails closed to the most restrictive
    // tier so the execute path can never present a benign confirmation for an
    // unknown action (same reasoning as steward's normalizeProposedPlan).
    risk_class: oneOf(rec.risk_class, RISK_CLASSES, mostRestrictiveRiskClass()),
    recommended: normalizeRecommended(rec.recommended),
    available_actions: unknownArray(rec.available_actions)
      .map(normalizeAvailableAction)
      .filter((item): item is AvailableAction => item !== null),
    status: oneOf(rec.status, DECISION_STATUSES, "open"),
    resolution: nullableText(rec.resolution),
    decided_by: nullableText(rec.decided_by),
    decided_at: nullableText(rec.decided_at),
    notes: text(rec.notes),
    tags: stringArray(rec.tags),
    task_ref: text(rec.task_ref),
    assignees: unknownArray(rec.assignees)
      .map(normalizeAssignee)
      .filter((item): item is DecisionAssignee => item !== null),
    suggested_snoozes: unknownArray(rec.suggested_snoozes)
      .map(normalizeSnooze)
      .filter((item): item is SnoozeOption => item !== null),
    draft: normalizeDraft(rec.draft),
    verification: normalizeVerification(rec.verification),
    created_at: text(rec.created_at),
    updated_at: text(rec.updated_at),
  };
}

// --- fetch functions ----------------------------------------------------------

/** GET /api/decisions — ALL the caller's open decisions in one owner-scoped
 * payload (principal resolved server-side; no owner param). `status` is an
 * optional server-side filter; the default returns the full open set. */
export async function fetchDecisions(status?: string): Promise<Decision[]> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  params.set("limit", "200");
  const qs = params.toString();
  return unknownArray(await getJson(`/api/decisions${qs ? `?${qs}` : ""}`)).map(normalizeDecision);
}

/** POST /api/decisions/{id}/decide — the ONE generic mutation. `decided_by` is
 * stamped server-side from the principal; a 409 means the row was already
 * resolved elsewhere (Slack / another tab) and the caller should refresh. */
export async function decideDecision(id: string, body: DecideBody): Promise<Decision> {
  return normalizeDecision(await postJson(`/api/decisions/${encodeURIComponent(id)}/decide`, body));
}
