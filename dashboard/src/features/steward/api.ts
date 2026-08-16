/** Defensive fetch client for the REAL Steward API surface — see
 * omniagentos/api/routes/{briefings,goals,comms,suggestions,alerts,voice}.py.
 * Every exported fetch function below is enumerated verbatim (by path) in
 * tests/api/test_dashboard_contract.py's anti-drift list — keep the two in sync
 * by hand whenever a path here changes. */
import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import {
  ackFixtureAlert,
  ackFixtureBriefing,
  approveFixtureSuggestion,
  decideFixtureSuggestion,
  FIXTURE_ALERTS,
  FIXTURE_BRIEFING,
  FIXTURE_BRIEFING_HISTORY,
  FIXTURE_COMMS_MESSAGES,
  FIXTURE_COMMS_SOURCES,
  FIXTURE_GOAL_DETAILS,
  FIXTURE_GOALS,
  FIXTURE_SUGGESTIONS,
  FIXTURE_VOICE_PROVIDERS,
  USE_STEWARD_FIXTURES,
} from "./fixtures";
import {
  ALERT_STATES,
  KB_STATUSES,
  RISK_CLASSES,
  SUGGESTION_STATES,
  type Alert,
  type AlertCount,
  type ApproveSuggestionResult,
  type Briefing,
  type BriefingDelivery,
  type BriefingSection,
  type BriefingSummary,
  type CommsMessage,
  type CommsMessageFilters,
  type CommsSource,
  type Goal,
  type GoalDetail,
  type GoalFact,
  type GoalRun,
  type MetricSnapshot,
  type ProposedPlan,
  type RiskClass,
  type Suggestion,
  type VoiceProviderStatus,
} from "./types";

export class StewardApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "StewardApiError";
  }
}

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
function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function integer(value: unknown, fallback = 0): number {
  return Number.isInteger(value) ? (value as number) : fallback;
}
function boolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}
function oneOf<T extends readonly string[]>(value: unknown, values: T, fallback: T[number]): T[number] {
  return typeof value === "string" && (values as readonly string[]).includes(value) ? (value as T[number]) : fallback;
}
function mostRestrictiveRiskClass(): RiskClass {
  return RISK_CLASSES[RISK_CLASSES.length - 1]!;
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

/** Shared same-origin fetch plumbing. Exported so sibling features (EDC
 * `features/decisions`) reuse the exact proxy/error handling instead of
 * duplicating it — hoist to `src/lib/` only on a third consumer (§10.1). */
export async function getJson(path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new StewardApiError("The Steward API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new StewardApiError("Could not reach the Steward API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch {
      /* Retain the response status text. */
    }
    throw new StewardApiError(message, response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new StewardApiError("The Steward API returned invalid JSON.", response.status);
  }
}

export async function postJson(path: string, body: unknown): Promise<unknown> {
  let response: Response;
  try {
    // fix7: every Steward mutation (suggestion approve/dismiss/generate, briefing
    // generate/ack, alert ack, goal facts) is token-gated, so it goes SAME-ORIGIN
    // to the Next.js proxy which attaches the session token server-side.
    response = await fetchWithTimeout(apiUrl(API_BASE, path, "POST"), {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new StewardApiError("The Steward API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new StewardApiError("Could not reach the Steward API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const parsed: unknown = await response.json();
      if (isRecord(parsed) && isRecord(parsed.error) && typeof parsed.error.message === "string") message = parsed.error.message;
    } catch {
      /* Retain the response status text. */
    }
    throw new StewardApiError(message, response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new StewardApiError("The Steward API returned invalid JSON.", response.status);
  }
}

// --- normalization -----------------------------------------------------------

function normalizeBriefingSection(value: unknown): BriefingSection {
  const rec = isRecord(value) ? value : {};
  return { title: text(rec.title), body: text(rec.body) };
}

export function normalizeBriefingSummary(value: unknown): BriefingSummary {
  const rec = isRecord(value) ? value : {};
  return {
    subject: text(rec.subject),
    headline: text(rec.headline),
    sections: unknownArray(rec.sections).map(normalizeBriefingSection),
    composed_by: text(rec.composed_by, "deterministic"),
  };
}

function normalizeBriefingDelivery(value: unknown): BriefingDelivery {
  const rec = isRecord(value) ? value : {};
  return {
    channel: text(rec.channel, "unknown"),
    ok: boolean(rec.ok),
    detail: text(rec.detail),
    artifact_id: nullableText(rec.artifact_id),
  };
}

export function normalizeBriefing(value: unknown): Briefing {
  const rec = isRecord(value) ? value : {};
  return {
    id: text(rec.id),
    briefing_date: text(rec.briefing_date),
    vault_path: nullableText(rec.vault_path),
    summary: normalizeBriefingSummary(rec.summary),
    deliveries: unknownArray(rec.deliveries).map(normalizeBriefingDelivery),
    voice_artifact_id: nullableText(rec.voice_artifact_id),
    acked_at: nullableText(rec.acked_at),
  };
}

function normalizeMetricSnapshot(value: unknown): MetricSnapshot {
  const rec = isRecord(value) ? value : {};
  return {
    id: integer(rec.id),
    goal_id: nullableText(rec.goal_id),
    source: text(rec.source),
    metric: text(rec.metric),
    value: number(rec.value),
    unit: text(rec.unit),
    window: text(rec.window, "day"),
    captured_at: text(rec.captured_at),
    meta: record(rec.meta),
  };
}

function normalizeSnapshotRecord(value: unknown): Record<string, MetricSnapshot | null> {
  const rec = isRecord(value) ? value : {};
  const out: Record<string, MetricSnapshot | null> = {};
  for (const [key, raw] of Object.entries(rec)) {
    out[key] = raw == null ? null : normalizeMetricSnapshot(raw);
  }
  return out;
}

function normalizeSnapshotSeriesRecord(value: unknown): Record<string, MetricSnapshot[]> {
  const rec = isRecord(value) ? value : {};
  const out: Record<string, MetricSnapshot[]> = {};
  for (const [key, raw] of Object.entries(rec)) {
    out[key] = unknownArray(raw).map(normalizeMetricSnapshot);
  }
  return out;
}

export function normalizeGoal(value: unknown): Goal {
  const rec = isRecord(value) ? value : {};
  const northStar = isRecord(rec.north_star) ? rec.north_star : {};
  return {
    id: text(rec.id),
    name: text(rec.name),
    description: text(rec.description),
    discipline_id: nullableText(rec.discipline_id),
    north_star: { source: text(northStar.source), metric: text(northStar.metric) },
    target: record(rec.target),
    keywords: stringArray(rec.keywords),
    priority: integer(rec.priority, 100),
    status: text(rec.status, "active"),
    created_at: text(rec.created_at),
    updated_at: text(rec.updated_at),
    latest: normalizeSnapshotRecord(rec.latest),
    trend: normalizeSnapshotSeriesRecord(rec.trend),
  };
}

function normalizeGoalFact(value: unknown): GoalFact | null {
  if (!isRecord(value)) return null;
  return { fact_id: integer(value.fact_id), statement: text(value.statement) };
}

function normalizeGoalRun(value: unknown): GoalRun {
  const rec = isRecord(value) ? value : {};
  return {
    id: text(rec.id),
    task_id: text(rec.task_id),
    state: text(rec.state, "queued"),
    harness: text(rec.harness),
    model: nullableText(rec.model),
    queued_at: text(rec.queued_at),
    started_at: nullableText(rec.started_at),
    finished_at: nullableText(rec.finished_at),
    cost_usd: nullableNumber(rec.cost_usd),
  };
}

/** The operator must be able to read the exact prompt/harness/action_class that
 * will run BEFORE approving (DES-001/DES-002) — normalize defensively so a
 * malformed or partial proposed_plan never silently hides the prompt. */
function normalizeProposedPlan(value: unknown): ProposedPlan | null {
  if (!isRecord(value)) return null;
  const actionClass = text(value.action_class);
  return {
    prompt: text(value.prompt),
    harness: text(value.harness, "cli-claude"),
    // This badge describes the prompt about to reach a live agent.  A missing,
    // blank, or whitespace-only class must not present that run as benign.
    action_class: actionClass.trim() || mostRestrictiveRiskClass(),
  };
}

export function normalizeSuggestion(value: unknown): Suggestion {
  const rec = isRecord(value) ? value : {};
  return {
    id: text(rec.id),
    title: text(rec.title),
    rationale: text(rec.rationale),
    evidence: unknownArray(rec.evidence),
    proposed_plan: normalizeProposedPlan(rec.proposed_plan),
    risk_class: oneOf(rec.risk_class, RISK_CLASSES, mostRestrictiveRiskClass()),
    source: text(rec.source),
    goal_id: nullableText(rec.goal_id),
    alert_id: Number.isInteger(rec.alert_id) ? (rec.alert_id as number) : null,
    outcome: record(rec.outcome),
    state: oneOf(rec.state, SUGGESTION_STATES, "open"),
    created_at: text(rec.created_at),
    decided_at: nullableText(rec.decided_at),
    decided_by: nullableText(rec.decided_by),
    task_id: nullableText(rec.task_id),
    run_id: nullableText(rec.run_id),
  };
}

export function normalizeGoalDetail(value: unknown): GoalDetail {
  const rec = isRecord(value) ? value : {};
  return {
    ...normalizeGoal(rec),
    facts: unknownArray(rec.facts).map(normalizeGoalFact).filter((fact): fact is GoalFact => fact !== null),
    suggestions: unknownArray(rec.suggestions).map(normalizeSuggestion),
    recent_runs: unknownArray(rec.recent_runs).map(normalizeGoalRun),
  };
}

export function normalizeCommsMessage(value: unknown): CommsMessage {
  const rec = isRecord(value) ? value : {};
  return {
    id: integer(rec.id),
    source: text(rec.source),
    external_id: text(rec.external_id),
    thread_id: text(rec.thread_id),
    sender: text(rec.sender),
    recipients: stringArray(rec.recipients),
    sent_at: nullableText(rec.sent_at),
    subject: text(rec.subject),
    body_text: nullableText(rec.body_text),
    attachments: unknownArray(rec.attachments),
    kb_status: oneOf(rec.kb_status, KB_STATUSES, "none"),
    episode_id: Number.isInteger(rec.episode_id) ? (rec.episode_id as number) : null,
    created_at: text(rec.created_at),
  };
}

export function normalizeCommsSource(value: unknown): CommsSource {
  const rec = isRecord(value) ? value : {};
  return {
    name: text(rec.name),
    kind: text(rec.kind),
    status: text(rec.status),
    config: record(rec.config),
    last_poll_at: nullableText(rec.last_poll_at),
    last_error: nullableText(rec.last_error),
  };
}

export function normalizeApproveResult(value: unknown): ApproveSuggestionResult {
  const rec = isRecord(value) ? value : {};
  return {
    suggestion: normalizeSuggestion(rec.suggestion),
    task_id: text(rec.task_id),
    run_id: text(rec.run_id),
  };
}

export function normalizeAlert(value: unknown): Alert {
  const rec = isRecord(value) ? value : {};
  return {
    id: integer(rec.id),
    rule: text(rec.rule),
    severity: text(rec.severity, "info"),
    title: text(rec.title),
    body: text(rec.body),
    evidence: record(rec.evidence),
    cooldown_key: nullableText(rec.cooldown_key),
    state: oneOf(rec.state, ALERT_STATES, "open"),
    created_at: text(rec.created_at),
    acked_at: nullableText(rec.acked_at),
  };
}

export function normalizeAlertCount(value: unknown): AlertCount {
  const rec = isRecord(value) ? value : {};
  return { open: Math.max(0, integer(rec.open)) };
}

export function normalizeVoiceProviderStatus(value: unknown): VoiceProviderStatus {
  const rec = isRecord(value) ? value : {};
  return { provider: text(rec.provider), status: text(rec.status), detail: text(rec.detail) };
}

// --- fetch functions ----------------------------------------------------------
// LITERAL paths (kept in sync by hand with tests/api/test_dashboard_contract.py):
//   GET  /api/briefings/latest
//   GET  /api/briefings
//   POST /api/briefings/generate
//   POST /api/briefings/{id}/ack
//   GET  /api/goals
//   GET  /api/goals/{id}
//   GET  /api/comms/messages
//   GET  /api/comms/sources
//   GET  /api/suggestions
//   POST /api/suggestions/{id}/approve
//   POST /api/suggestions/{id}/dismiss
//   GET  /api/alerts
//   GET  /api/alerts/count
//   POST /api/alerts/{id}/ack
//   GET  /api/voice/audio/{id}
//   GET  /api/voice/providers

export async function fetchLatestBriefing(): Promise<Briefing | null> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_BRIEFING;
  try {
    return normalizeBriefing(await getJson("/api/briefings/latest"));
  } catch (reason) {
    if (reason instanceof StewardApiError && reason.status === 404) return null;
    throw reason;
  }
}

export async function fetchBriefingHistory(limit = 30): Promise<Briefing[]> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_BRIEFING_HISTORY;
  const data = await getJson(`/api/briefings?limit=${Math.max(1, Math.min(500, Math.floor(limit)))}`);
  return unknownArray(data).map(normalizeBriefing);
}

export async function generateBriefing(dryRun = false): Promise<BriefingSummary> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_BRIEFING.summary;
  return normalizeBriefingSummary(await postJson("/api/briefings/generate", { dry_run: dryRun }));
}

export async function ackBriefing(id: string): Promise<Briefing> {
  if (USE_STEWARD_FIXTURES) return ackFixtureBriefing(id);
  return normalizeBriefing(await postJson(`/api/briefings/${encodeURIComponent(id)}/ack`, {}));
}

export async function fetchGoals(): Promise<Goal[]> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_GOALS;
  return unknownArray(await getJson("/api/goals")).map(normalizeGoal);
}

export async function fetchGoalDetail(id: string): Promise<GoalDetail> {
  if (USE_STEWARD_FIXTURES) {
    const found = FIXTURE_GOAL_DETAILS[id];
    if (found) return found;
    throw new StewardApiError("goal not found", 404);
  }
  return normalizeGoalDetail(await getJson(`/api/goals/${encodeURIComponent(id)}`));
}

function commsQuery(filters: CommsMessageFilters): string {
  const params = new URLSearchParams();
  if (filters.source) params.set("source", filters.source);
  if (filters.q) params.set("q", filters.q);
  if (filters.thread_id) params.set("thread_id", filters.thread_id);
  if (filters.kb_status) params.set("kb_status", filters.kb_status);
  if (filters.limit) params.set("limit", String(Math.max(1, Math.min(500, Math.floor(filters.limit)))));
  if (filters.before_id != null) params.set("before_id", String(Math.floor(filters.before_id)));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchCommsMessages(filters: CommsMessageFilters = {}): Promise<CommsMessage[]> {
  if (USE_STEWARD_FIXTURES) {
    return FIXTURE_COMMS_MESSAGES.filter((row) => {
      if (filters.source && row.source !== filters.source) return false;
      if (filters.kb_status && row.kb_status !== filters.kb_status) return false;
      if (filters.thread_id && row.thread_id !== filters.thread_id) return false;
      if (filters.q && !`${row.subject} ${row.body_text ?? ""} ${row.sender}`.toLowerCase().includes(filters.q.toLowerCase())) return false;
      return true;
    });
  }
  return unknownArray(await getJson(`/api/comms/messages${commsQuery(filters)}`)).map(normalizeCommsMessage);
}

export async function fetchCommsSources(): Promise<CommsSource[]> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_COMMS_SOURCES;
  return unknownArray(await getJson("/api/comms/sources")).map(normalizeCommsSource);
}

export async function fetchSuggestions(state?: string): Promise<Suggestion[]> {
  if (USE_STEWARD_FIXTURES) return state ? FIXTURE_SUGGESTIONS.filter((row) => row.state === state) : FIXTURE_SUGGESTIONS;
  const qs = state ? `?state=${encodeURIComponent(state)}` : "";
  return unknownArray(await getJson(`/api/suggestions${qs}`)).map(normalizeSuggestion);
}

export async function approveSuggestion(id: string, approvedBy: string): Promise<ApproveSuggestionResult> {
  if (USE_STEWARD_FIXTURES) return approveFixtureSuggestion(id, approvedBy);
  return normalizeApproveResult(await postJson(`/api/suggestions/${encodeURIComponent(id)}/approve`, { approved_by: approvedBy }));
}

export async function dismissSuggestion(id: string, decidedBy: string, reason?: string): Promise<Suggestion> {
  if (USE_STEWARD_FIXTURES) return decideFixtureSuggestion(id, "dismissed", decidedBy, reason);
  return normalizeSuggestion(
    await postJson(`/api/suggestions/${encodeURIComponent(id)}/dismiss`, { decided_by: decidedBy, reason: reason ?? null }),
  );
}

export async function fetchAlerts(state?: string): Promise<Alert[]> {
  if (USE_STEWARD_FIXTURES) return state ? FIXTURE_ALERTS.filter((row) => row.state === state) : FIXTURE_ALERTS;
  const qs = state ? `?state=${encodeURIComponent(state)}` : "";
  return unknownArray(await getJson(`/api/alerts${qs}`)).map(normalizeAlert);
}

export async function fetchAlertCount(): Promise<AlertCount> {
  if (USE_STEWARD_FIXTURES) return { open: FIXTURE_ALERTS.filter((row) => row.state === "open").length };
  return normalizeAlertCount(await getJson("/api/alerts/count"));
}

export async function ackAlert(id: number, by?: string): Promise<Alert> {
  if (USE_STEWARD_FIXTURES) return ackFixtureAlert(id, by);
  return normalizeAlert(await postJson(`/api/alerts/${id}/ack`, by ? { by } : {}));
}

export async function fetchVoiceProviders(): Promise<VoiceProviderStatus[]> {
  if (USE_STEWARD_FIXTURES) return FIXTURE_VOICE_PROVIDERS;
  return unknownArray(await getJson("/api/voice/providers")).map(normalizeVoiceProviderStatus);
}

/** GET /api/voice/audio/{id} is consumed as an <audio> element src, never fetched
 * directly in JS — this just builds the URL against the same API_BASE every other
 * client call uses. */
export function voiceAudioUrl(artifactId: string): string {
  return `${API_BASE}/api/voice/audio/${encodeURIComponent(artifactId)}`;
}
