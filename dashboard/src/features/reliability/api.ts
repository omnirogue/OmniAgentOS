/**
 * Client for V2 reliability / company APIs.
 * All calls go same-origin through the Next catch-all proxy (token injection
 * is server-side only — browser never sees session or autonomy tokens).
 */

import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import type {
  ApiAudit,
  ApiAutonomySetting,
  ApiImprovement,
  ApiReliabilityEvent,
  ApiVote,
  EventHubStatus,
  HealthSummary,
  ReliabilityIncident,
  ReliabilityOpenEvents,
  ReliabilityWatchSummary,
} from "../../lib/reliabilityContracts";

export class ReliabilityApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null = null,
    readonly detail: unknown = undefined,
  ) {
    super(message);
    this.name = "ReliabilityApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Preserve explicit nulls; never invent zeros for unavailable aggregates (B02/B06). */
function nullableNumber(value: unknown): number | null {
  if (value == null) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nullableText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function errorEnvelope(body: unknown): { message: string | null; code: string | null; detail: unknown } {
  if (!isRecord(body) || !isRecord(body.error)) {
    return { message: null, code: null, detail: undefined };
  }
  return {
    message: typeof body.error.message === "string" ? body.error.message : null,
    code: typeof body.error.code === "string" ? body.error.code : null,
    detail: body.error.detail,
  };
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function unknownArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function qs(params?: Record<string, string | undefined>): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(
    (e): e is [string, string] => typeof e[1] === "string" && e[1].length > 0,
  );
  if (entries.length === 0) return "";
  return `?${new URLSearchParams(entries).toString()}`;
}

async function getJson(path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new ReliabilityApiError(
        "The reliability API did not respond in time — it may be down or overloaded.",
        0,
      );
    }
    throw new ReliabilityApiError("Could not reach the reliability API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    let code: string | null = null;
    let detail: unknown;
    try {
      const body: unknown = await response.json();
      const env = errorEnvelope(body);
      if (env.message) message = env.message;
      code = env.code;
      detail = env.detail;
    } catch {
      /* keep status text */
    }
    throw new ReliabilityApiError(message, response.status, code, detail);
  }
  try {
    return await response.json();
  } catch {
    throw new ReliabilityApiError("The reliability API returned invalid JSON.", response.status);
  }
}

async function mutateJson(path: string, method: "POST" | "PUT", body?: unknown): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, {
      method,
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new ReliabilityApiError(
        "The reliability API did not respond in time — it may be down or overloaded.",
        0,
      );
    }
    throw new ReliabilityApiError("Could not reach the reliability API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    let code: string | null = null;
    let detail: unknown;
    try {
      const bodyJson: unknown = await response.json();
      const env = errorEnvelope(bodyJson);
      if (env.message) message = env.message;
      code = env.code;
      detail = env.detail;
    } catch {
      /* keep status text */
    }
    throw new ReliabilityApiError(message, response.status, code, detail);
  }
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

// ── Normalizers ─────────────────────────────────────────────────────────────

function normalizeEvent(raw: unknown): ApiReliabilityEvent {
  const r = record(raw);
  return {
    id: text(r.id),
    failure_class: text(r.failure_class, "unknown"),
    severity: (text(r.severity, "info") as ApiReliabilityEvent["severity"]) || "info",
    signature: text(r.signature),
    occurrence_key: text(r.occurrence_key) || undefined,
    source: text(r.source),
    ref_type: nullableText(r.ref_type),
    ref_id: nullableText(r.ref_id),
    evidence_json: record(r.evidence_json),
    status: (text(r.status, "open") as ApiReliabilityEvent["status"]) || "open",
    recovery_json: record(r.recovery_json),
    improvement_id: nullableText(r.improvement_id),
    audit_id: nullableText(r.audit_id),
    detected_at: text(r.detected_at),
    updated_at: text(r.updated_at),
  };
}

function normalizeAudit(raw: unknown): ApiAudit {
  const r = record(raw);
  return {
    id: text(r.id),
    kind: text(r.kind),
    status: text(r.status),
    window_start: text(r.window_start),
    window_end: text(r.window_end),
    stats_json: record(r.stats_json),
    findings: number(r.findings),
    report_note_path: nullableText(r.report_note_path),
    started_at: text(r.started_at),
    finished_at: nullableText(r.finished_at),
  };
}

function normalizeVote(raw: unknown): ApiVote {
  const r = record(raw);
  const scoresRaw = r.scores_json;
  let scores: Record<string, number | unknown> = {};
  if (isRecord(scoresRaw)) scores = scoresRaw;
  else if (typeof scoresRaw === "string") {
    try {
      const parsed: unknown = JSON.parse(scoresRaw);
      if (isRecord(parsed)) scores = parsed;
    } catch {
      scores = {};
    }
  }
  return {
    id: text(r.id),
    improvement_id: text(r.improvement_id),
    panel_attempt_id: text(r.panel_attempt_id),
    judge_agent: text(r.judge_agent),
    model_family: text(r.model_family),
    model: text(r.model),
    verdict: (text(r.verdict, "needs_human") as ApiVote["verdict"]) || "needs_human",
    scores_json: scores,
    reasoning: text(r.reasoning),
    conditions: text(r.conditions),
    created_at: text(r.created_at),
  };
}

function normalizeImprovement(raw: unknown): ApiImprovement {
  const r = record(raw);
  const proposal = record(r.proposal_json);
  const votesRaw = r.votes;
  return {
    id: text(r.id),
    origin: text(r.origin),
    kind: text(r.kind),
    title: text(r.title, "Untitled"),
    summary: text(r.summary),
    root_cause: text(r.root_cause),
    proposal_json: proposal,
    // Favourable-absence guard (Class A): a missing/unparseable risk_level
    // must never silently normalize to a healthy default before it reaches
    // toGovernedProposalView. Preserve null so "unknown" stays unknown.
    risk_level: nullableNumber(r.risk_level),
    status: text(r.status, "proposed"),
    version: number(r.version),
    ranking_score: number(r.ranking_score),
    sandbox_json: record(r.sandbox_json),
    votes_summary_json: record(r.votes_summary_json),
    votes: Array.isArray(votesRaw) ? votesRaw.map(normalizeVote) : undefined,
    rollback_point_id: nullableText(r.rollback_point_id),
    applied_sha: nullableText(r.applied_sha),
    monitor_until: nullableText(r.monitor_until),
    decided_by: nullableText(r.decided_by),
    created_by: text(r.created_by, "system"),
    created_at: text(r.created_at),
    updated_at: text(r.updated_at),
    applied_at: nullableText(r.applied_at),
    resolved_at: nullableText(r.resolved_at),
    before: nullableText(r.before) ?? nullableText(proposal.before),
    after: nullableText(r.after) ?? nullableText(proposal.after),
  };
}

function normalizeAutonomy(raw: unknown): ApiAutonomySetting {
  const r = record(raw);
  return {
    id: text(r.id),
    scope_type: text(r.scope_type, "global"),
    scope_id: text(r.scope_id),
    mode: text(r.mode, "approve"),
    max_auto_risk: number(r.max_auto_risk),
    updated_by: text(r.updated_by),
    updated_at: text(r.updated_at),
  };
}




function listPayload(raw: unknown, keys: string[]): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (!isRecord(raw)) return [];
  for (const key of keys) {
    if (Array.isArray(raw[key])) return raw[key] as unknown[];
  }
  return [];
}

// ── Reliability ─────────────────────────────────────────────────────────────

function normalizeOpenEvents(raw: unknown): ReliabilityOpenEvents | undefined {
  if (!isRecord(raw)) return undefined;
  return {
    info: nullableNumber(raw.info),
    warning: nullableNumber(raw.warning),
    critical: nullableNumber(raw.critical),
  };
}

function normalizeWatch(raw: unknown): ReliabilityWatchSummary | null {
  if (!isRecord(raw)) return null;
  return {
    state: text(raw.state, "unavailable") || "unavailable",
    heartbeat_at: nullableText(raw.heartbeat_at),
    cursor_at: nullableText(raw.cursor_at),
    age_seconds: nullableNumber(raw.age_seconds),
    stale_after_seconds: nullableNumber(raw.stale_after_seconds),
    error: nullableText(raw.error),
  };
}

function normalizeIncidents(raw: unknown): ReliabilityIncident[] {
  return unknownArray(raw)
    .map((item) => {
      const r = record(item);
      const code = text(r.code);
      if (!code) return null;
      return {
        code,
        component: text(r.component, "unknown"),
        severity: text(r.severity, "critical"),
        message: text(r.message, code),
      };
    })
    .filter((item): item is ReliabilityIncident => item != null);
}

/**
 * Real frontend adapter for B02/L03 `reliability-summary.v1`.
 * Preserves null/unavailable incident counts and nested watch/incident state.
 * Exported for byte-faithful fixture tests (do not invent healthy zeros).
 */
export function normalizeHealthSummary(raw: unknown): HealthSummary {
  const r = record(raw);
  const openEvents = normalizeOpenEvents(r.open_events);
  const open_events_state = text(r.open_events_state) || undefined;

  // Prefer nested open_events.* then flattened legacy fields; never coerce null→0.
  const open_critical =
    openEvents != null ? openEvents.critical : nullableNumber(r.open_critical);
  const open_warning =
    openEvents != null ? openEvents.warning : nullableNumber(r.open_warning);
  const open_info = openEvents != null ? openEvents.info : nullableNumber(r.open_info);

  const healthRaw = text(r.health, "");
  // Fail closed: unknown/missing health is degraded, never invented "healthy".
  const health =
    healthRaw === "healthy" ||
    healthRaw === "degraded" ||
    healthRaw === "critical" ||
    healthRaw === "recovering"
      ? healthRaw
      : "degraded";

  const watch = normalizeWatch(r.watch);
  const degraded_reasons = unknownArray(r.degraded_reasons)
    .map((item) => (typeof item === "string" ? item : null))
    .filter((item): item is string => item != null);
  const incidents = normalizeIncidents(r.incidents);

  const watch_cursor_age_s =
    watch?.age_seconds != null
      ? watch.age_seconds
      : nullableNumber(r.watch_cursor_age_s);

  return {
    contract_version: nullableText(r.contract_version),
    generated_at: nullableText(r.generated_at),
    open_critical,
    open_warning,
    open_info,
    open_events: openEvents,
    open_events_state,
    last_audit_status: nullableText(r.last_audit_status),
    last_audit_at: nullableText(r.last_audit_at),
    last_audit_kind: nullableText(r.last_audit_kind),
    last_audit_findings: nullableNumber(r.last_audit_findings),
    last_audit_state: text(r.last_audit_state) || undefined,
    last_audit: isRecord(r.last_audit) ? r.last_audit : r.last_audit === null ? null : undefined,
    watch_cursor_at: nullableText(r.watch_cursor_at) ?? watch?.cursor_at ?? null,
    watch_cursor_age_s,
    watch_heartbeat: nullableText(r.watch_heartbeat) ?? watch?.heartbeat_at ?? null,
    watch,
    health,
    degraded_reasons: degraded_reasons.length ? degraded_reasons : undefined,
    incidents: incidents.length ? incidents : undefined,
    mode: text(r.mode) === "auto" ? "auto" : text(r.mode) === "approve" ? "approve" : undefined,
    max_auto_risk: r.max_auto_risk == null ? undefined : number(r.max_auto_risk),
  };
}

/** Normalize L12 `health.event_hub` (or a raw EventHubStatus object). */
export function normalizeEventHubStatus(raw: unknown): EventHubStatus | null {
  if (!isRecord(raw)) return null;
  const hub = isRecord(raw.event_hub) ? raw.event_hub : raw;
  if (!isRecord(hub)) return null;
  const state = text(hub.state, "unknown") || "unknown";
  const degraded =
    hub.degraded === true ||
    state === "degraded" ||
    text(raw.status) === "degraded";
  return {
    contract_version:
      typeof hub.contract_version === "number" ? hub.contract_version : undefined,
    state,
    degraded,
    consecutive_failures: nullableNumber(hub.consecutive_failures),
    last_error: nullableText(hub.last_error),
    last_failure_at: nullableText(hub.last_failure_at),
    last_success_at: nullableText(hub.last_success_at),
    tailer_alive: typeof hub.tailer_alive === "boolean" ? hub.tailer_alive : undefined,
    tailer_restarts: nullableNumber(hub.tailer_restarts),
    max_tailer_restarts: nullableNumber(hub.max_tailer_restarts),
    subscriber_count: nullableNumber(hub.subscriber_count),
    degraded_after_failures: nullableNumber(hub.degraded_after_failures),
    restart_after_failures: nullableNumber(hub.restart_after_failures),
  };
}

export async function fetchHealthSummary(): Promise<HealthSummary> {
  const raw = await getJson("/api/reliability/summary");
  return normalizeHealthSummary(raw);
}

/** L12 operator-visible event hub status from `GET /api/health`. */
export async function fetchEventHubStatus(): Promise<EventHubStatus | null> {
  const raw = await getJson("/api/health");
  return normalizeEventHubStatus(raw);
}

export async function fetchReliabilityEvents(params?: {
  status?: string;
  severity?: string;
  q?: string;
  limit?: string;
}): Promise<ApiReliabilityEvent[]> {
  const raw = await getJson(
    `/api/reliability/events${qs({
      status: params?.status,
      severity: params?.severity,
      q: params?.q,
      limit: params?.limit ?? "50",
    })}`,
  );
  return listPayload(raw, ["events", "items"]).map(normalizeEvent);
}

export async function ignoreReliabilityEvent(id: string): Promise<ApiReliabilityEvent | null> {
  const raw = await mutateJson(
    `/api/reliability/events/${encodeURIComponent(id)}/ignore`,
    "POST",
    {},
  );
  return raw == null ? null : normalizeEvent(raw);
}

export async function fetchAudits(limit = 10): Promise<ApiAudit[]> {
  const raw = await getJson(`/api/reliability/audits${qs({ limit: String(limit) })}`);
  return listPayload(raw, ["audits", "items"]).map(normalizeAudit);
}

// ── Improvements / autonomy ─────────────────────────────────────────────────

export async function fetchImprovements(status?: string): Promise<ApiImprovement[]> {
  const raw = await getJson(`/api/improvements${qs({ status })}`);
  return listPayload(raw, ["improvements", "items"]).map(normalizeImprovement);
}

export async function decideImprovement(
  id: string,
  action: "approve" | "reject" | "apply" | "rollback" | "pull",
  body?: { decided_by?: string; note?: string },
): Promise<ApiImprovement | null> {
  // Governance rule (docs/ui-redesign/improvements-governed-evidence-design.md
  // §2.3): every decision action requires an explicit typed identity — never
  // a silent "operator"/"human" default that masks who decided. All five
  // actions (approve/reject/apply/rollback/pull) route through the identity
  // dialog; the server enforces the same non-empty rule independently.
  const typedIdentity = body?.decided_by?.trim() ?? "";
  if (!typedIdentity) {
    throw new ReliabilityApiError(
      `decided_by is required to ${action} an improvement — decisions must carry an explicit identity.`,
      0,
    );
  }
  const raw = await mutateJson(`/api/improvements/${encodeURIComponent(id)}/${action}`, "POST", {
    decided_by: typedIdentity,
    note: body?.note,
  });
  return raw == null ? null : normalizeImprovement(raw);
}

/** Flatten a tree-scope entry ({id, mode, max_auto_risk}) into an ApiAutonomySetting row. */
function flattenScopeEntry(item: unknown, scopeType: string): ApiAutonomySetting {
  const r = record(item);
  return {
    id: text(r.id),
    scope_type: scopeType,
    scope_id: text(r.id),
    mode: text(r.mode, "approve"),
    max_auto_risk: number(r.max_auto_risk),
    updated_by: "",
    updated_at: "",
  };
}

export async function fetchAutonomy(): Promise<ApiAutonomySetting[]> {
  const raw = await getJson("/api/autonomy");
  if (Array.isArray(raw)) return raw.map(normalizeAutonomy);
  if (isRecord(raw) && Array.isArray(raw.settings)) {
    return raw.settings.map(normalizeAutonomy);
  }
  // Actual GET /api/autonomy shape (omniagentos/api/routes/autonomy.py
  // `get_autonomy`): a tree, {global, departments, agents, kinds} — not a
  // flat setting row. Flatten it into the same ApiAutonomySetting[] surface
  // callers of this function already expect.
  if (isRecord(raw) && isRecord(raw.global)) {
    const settings: ApiAutonomySetting[] = [
      flattenScopeEntry({ id: "", ...record(raw.global) }, "global"),
    ];
    for (const [key, scopeType] of [
      ["departments", "department"],
      ["agents", "agent"],
      ["kinds", "kind"],
    ] as const) {
      for (const item of unknownArray(raw[key])) {
        settings.push(flattenScopeEntry(item, scopeType));
      }
    }
    return settings;
  }
  if (isRecord(raw) && text(raw.scope_type)) return [normalizeAutonomy(raw)];
  return [];
}

export async function updateAutonomy(payload: {
  scope_type: string;
  scope_id?: string;
  mode: string;
  max_auto_risk: number;
  updated_by?: string;
}): Promise<ApiAutonomySetting> {
  const raw = await mutateJson("/api/autonomy", "PUT", {
    ...payload,
    updated_by: payload.updated_by ?? "operator",
  });
  return normalizeAutonomy(raw);
}

// ── Organization ────────────────────────────────────────────────────────────
// Note: Deleted orphaned org client functions (fetchOrgTree, fetchOrgAgents, toggleAgent, fetchAgentActivity,
// fetchAgentRequests, createAgentRequest, decideAgentRequest). A future features/org/ tree view writes its own client.
