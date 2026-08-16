/**
 * Pure projection for the readable agent activity timeline.
 *
 * Maps one engine snapshot activity event into a stable, operator-facing row:
 * role, model/account, timestamp, phase, concise action/result, and GitHub-only
 * evidence links. Raw payloads stay attached for a collapsed disclosure and are
 * never treated as HTML by consumers of this module.
 */

const GITHUB_URL_PREFIX = "https://github.com/";

/** Common swarm/engine actions → readable phase when payload.phase is absent. */
const ACTION_PHASE: Record<string, string> = {
  plan_created: "planning",
  run_started: "running",
  task_assigned: "running",
  worker_spawned: "running",
  "attempt.started": "running",
  review_confirmed: "reviewing",
  review_denied: "reviewing",
  task_completed: "done",
  run_completed: "done",
  run_failed: "failed",
  rate_limit: "blocked",
  task_blocked: "blocked",
  approval_parked: "parked",
};

export type ActivityEvidenceLink = {
  label: string;
  /**
   * `null` when the destination was refused (non-GitHub or absent url): the
   * evidence stays visible as inert text, distinguishable from "no evidence",
   * but there is nothing the browser may navigate to.
   */
  href: string | null;
};

export type ActivityTimelineEntry = {
  id: number;
  timestampIso: string | null;
  timestampLabel: string;
  role: string | null;
  model: string | null;
  account: string | null;
  modelAccountLabel: string;
  phase: string | null;
  action: string;
  result: string | null;
  summary: string;
  evidenceLinks: ActivityEvidenceLink[];
  hasRawPayload: boolean;
  rawPayload: Record<string, unknown>;
};

export type ActivityEventInput = {
  id: number;
  action?: string | null;
  created_at?: string | null;
  payload?: Record<string, unknown>;
};

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * Deterministic UTC label. Never uses locale or wall-clock — fixed ISO in,
 * fixed string out (or an em-dash when unparseable).
 */
export function formatActivityTimestamp(iso: string | null | undefined): string {
  if (iso == null || iso === "") return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  const d = new Date(ms);
  return (
    `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
    `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} UTC`
  );
}

/**
 * Prefer an explicit payload.phase; otherwise map known actions. Unknown
 * actions without a phase yield null rather than inventing one.
 */
export function deriveActivityPhase(
  action: string | null | undefined,
  payload: Record<string, unknown>,
): string | null {
  const explicit = payload.phase;
  if (typeof explicit === "string" && explicit.trim()) return explicit;
  if (!action) return null;
  return ACTION_PHASE[action] ?? null;
}

function stringField(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : null;
}

/** Concise result prefers status, then reason, then note — never a raw dump. */
function conciseResult(payload: Record<string, unknown>): string | null {
  return (
    stringField(payload, "status") ??
    stringField(payload, "reason") ??
    stringField(payload, "note")
  );
}

/**
 * A refused destination (non-GitHub or absent url) must not erase the
 * evidence: the entry is kept as an inert, label-only row (`href: null`) so a
 * blocked link stays distinguishable from a genuinely empty evidence list
 * (F005). Entries with nothing displayable at all are dropped.
 */
function projectEvidenceLinks(payload: Record<string, unknown>): ActivityEvidenceLink[] {
  const raw = payload.evidence_links;
  if (!Array.isArray(raw)) return [];
  const links: ActivityEvidenceLink[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) continue;
    const row = entry as Record<string, unknown>;
    const url =
      typeof row.url === "string" && row.url.startsWith(GITHUB_URL_PREFIX) ? row.url : null;
    const label =
      (typeof row.label === "string" && row.label.trim() && row.label) ||
      (typeof row.name === "string" && row.name.trim() && row.name) ||
      url;
    if (!label) continue;
    links.push({ label, href: url });
  }
  return links;
}

function modelAccountLabel(model: string | null, account: string | null): string {
  if (model && account) return `${model} · ${account}`;
  if (model) return model;
  if (account) return account;
  return "—";
}

export function projectActivityTimelineEntry(event: ActivityEventInput): ActivityTimelineEntry {
  const payload =
    event.payload !== null && typeof event.payload === "object" && !Array.isArray(event.payload)
      ? event.payload
      : {};
  const action =
    typeof event.action === "string" && event.action.trim() ? event.action : "event";
  const role = stringField(payload, "role");
  const model = stringField(payload, "model");
  // Never promote account_id — only the human account label.
  const account = stringField(payload, "account");
  const result = conciseResult(payload);
  const phase = deriveActivityPhase(event.action ?? null, payload);
  const timestampIso =
    typeof event.created_at === "string" && event.created_at ? event.created_at : null;
  const timestampLabel = formatActivityTimestamp(timestampIso);
  const summary = result ? `${action} — ${result}` : action;

  return {
    id: event.id,
    timestampIso,
    timestampLabel,
    role,
    model,
    account,
    modelAccountLabel: modelAccountLabel(model, account),
    phase,
    action,
    result,
    summary,
    evidenceLinks: projectEvidenceLinks(payload),
    hasRawPayload: Object.keys(payload).length > 0,
    rawPayload: payload,
  };
}

/** Ascending by event id (cursor order), independent of input order. */
export function projectActivityTimeline(events: ActivityEventInput[]): ActivityTimelineEntry[] {
  return [...events]
    .sort((a, b) => a.id - b.id)
    .map(projectActivityTimelineEntry);
}
