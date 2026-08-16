import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "@/lib/fetchTimeout";
import type {
  EngineApproval,
  EngineCapabilities,
  EngineEvidence,
  EngineRunContext,
  EngineRunSnapshot,
  EngineRunSummary,
} from "./types";

/** Links this product can vouch for. Anything else is data, not a destination. */
const GITHUB_URL_PREFIX = "https://github.com/";

export class ControlPlaneApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ControlPlaneApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetchWithTimeout(apiUrl(API_BASE, path, "GET"), {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new ControlPlaneApiError("The control plane did not respond in time.", 0);
    }
    throw new ControlPlaneApiError("Could not reach the control plane.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { error?: { message?: string }; detail?: string };
      message = body.error?.message ?? body.detail ?? message;
    } catch {
      // Keep the status text when the upstream response is not JSON.
    }
    throw new ControlPlaneApiError(message, response.status);
  }
  return (await response.json()) as T;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/**
 * The client re-applies the server's link rule rather than trusting it. Both
 * sides are cheap, and a link is the one field that turns projected data into
 * a place the operator's browser will actually go.
 */
function githubUrl(value: unknown): string | null {
  return typeof value === "string" && value.startsWith(GITHUB_URL_PREFIX) ? value : null;
}

function mapList<T>(value: unknown, map: (entry: Record<string, unknown>) => T): T[] {
  return Array.isArray(value) ? value.map((entry) => map(record(entry))) : [];
}

export function normalizeEngineCapabilities(value: unknown): EngineCapabilities {
  const raw = record(value);
  const caps = record(raw.capabilities);
  const links = record(raw.links);
  return {
    api_version: text(raw.api_version) ?? "unknown",
    product: text(raw.product) ?? "Unknown product",
    read_only: bool(raw.read_only) ?? true,
    capabilities: {
      swarm: bool(caps.swarm) ?? false,
      parallel_execution: bool(caps.parallel_execution) ?? false,
      execution_enabled: bool(caps.execution_enabled) ?? false,
      worktree_isolation_enabled: bool(caps.worktree_isolation_enabled) ?? false,
      memory: bool(caps.memory) ?? false,
      reflection: bool(caps.reflection) ?? false,
      improvements: bool(caps.improvements) ?? false,
      activity_cursor: bool(caps.activity_cursor) ?? false,
    },
    links: {
      create_run: text(links.create_run),
      run: text(links.run),
      activity: text(links.activity),
      snapshot: text(links.snapshot),
      memory_search: text(links.memory_search),
      improvements: text(links.improvements),
    },
  };
}

function normalizeContext(value: unknown): EngineRunContext {
  const raw = record(value);
  // CP-004: the real projector (`_CONTEXT_FIELDS` in engine.py) never emits
  // base_sha -- only project the fields it actually produces.
  return {
    repository: text(raw.repository),
    branch: text(raw.branch),
    head_sha: text(raw.head_sha),
  };
}

function normalizeEvidence(value: unknown): EngineEvidence {
  const raw = record(value);
  return {
    commits: mapList(raw.commits, (entry) => ({
      sha: text(entry.sha) ?? "",
      message: text(entry.message),
      url: githubUrl(entry.url),
      url_refused: bool(entry.url_refused) === true,
    })),
    files: mapList(raw.files, (entry) => ({
      path: text(entry.path) ?? "",
      url: githubUrl(entry.url),
      url_refused: bool(entry.url_refused) === true,
    })),
    tests: mapList(raw.tests, (entry) => ({
      name: text(entry.name) ?? "",
      status: text(entry.status),
      url: githubUrl(entry.url),
      url_refused: bool(entry.url_refused) === true,
    })),
    reports: mapList(raw.reports, (entry) => ({
      name: text(entry.name) ?? "",
      status: text(entry.status),
      url: githubUrl(entry.url),
      url_refused: bool(entry.url_refused) === true,
    })),
  };
}

/** No receipt means no approval, whatever the `approved` flag claims. */
function normalizeApproval(value: unknown): EngineApproval {
  const raw = record(value);
  const hasReceipt =
    raw.receipt !== null && typeof raw.receipt === "object" && !Array.isArray(raw.receipt);
  if (!hasReceipt) return { approved: false, receipt: null };
  const receipt = record(raw.receipt);
  return {
    approved: bool(raw.approved) ?? false,
    receipt: {
      reviewer: text(receipt.reviewer),
      run_id: text(receipt.run_id),
      head_sha: text(receipt.head_sha),
      recorded_at: text(receipt.recorded_at),
    },
  };
}

/**
 * Re-apply the GitHub link rule on in-payload evidence_links the same way
 * top-level evidence entries are handled. Non-GitHub urls become null so the
 * browser never receives an untrusted destination from this client.
 */
function normalizeActivityEvidenceLinks(value: unknown): unknown {
  if (!Array.isArray(value)) return value;
  return value.map((entry) => {
    const row = record(entry);
    return {
      ...row,
      url: githubUrl(row.url),
    };
  });
}

function normalizeActivityPayload(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    malformedSnapshot("activity event payload is not an object");
  }
  const payload = value as Record<string, unknown>;
  if (!("evidence_links" in payload)) return payload;
  return {
    ...payload,
    evidence_links: normalizeActivityEvidenceLinks(payload.evidence_links),
  };
}

/**
 * Activity is the cursor stream. Coercing a malformed page into a healthy
 * empty one (`[]`, `id: 0`) would let the poll advance `next_activity_cursor`
 * past events it never saw and permanently skip them. A malformed feed must
 * reject the whole snapshot instead, so the hooks' error path keeps the last
 * confirmed snapshot and the cursor stays put (F004).
 */
function malformedSnapshot(detail: string): never {
  throw new ControlPlaneApiError(`The control plane returned a malformed snapshot: ${detail}.`, 0);
}

function normalizeActivity(value: unknown): EngineRunSnapshot["activity"] {
  if (!Array.isArray(value)) malformedSnapshot("activity is not a list");
  return value.map((entry) => {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      malformedSnapshot("activity event is not an object");
    }
    const row = entry as Record<string, unknown>;
    if (
      typeof row.id !== "number" ||
      !Number.isFinite(row.id) ||
      !Number.isInteger(row.id) ||
      row.id < 0
    ) {
      malformedSnapshot("activity event id is not a non-negative integer");
    }
    return {
      id: row.id,
      action: text(row.action),
      created_at: text(row.created_at),
      target_type: text(row.target_type),
      target_id: text(row.target_id),
      payload: normalizeActivityPayload(row.payload),
    };
  });
}

/** The cursor the next poll resumes from must itself be trustworthy (F004). */
function normalizeActivityCursor(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    !Number.isInteger(value) ||
    value < 0
  ) {
    malformedSnapshot("next_activity_cursor is not a non-negative integer");
  }
  return value;
}

function normalizeEngineRunSnapshot(value: unknown): EngineRunSnapshot {
  const raw = record(value);
  return {
    ...(raw as unknown as EngineRunSnapshot),
    activity: normalizeActivity(raw.activity),
    next_activity_cursor: normalizeActivityCursor(raw.next_activity_cursor),
    context: normalizeContext(raw.context),
    evidence: normalizeEvidence(raw.evidence),
    approval: normalizeApproval(raw.approval),
  };
}

function normalizeEngineRuns(value: unknown): { runs: EngineRunSummary[] } {
  const raw = record(value);
  return {
    runs: mapList(raw.runs, (entry) => ({
      id: text(entry.id) ?? "",
      status: text(entry.status) ?? "unknown",
      goal: text(entry.goal),
      project_id: text(entry.project_id),
      created_at: text(entry.created_at),
      started_at: text(entry.started_at),
      finished_at: text(entry.finished_at),
      error: text(entry.error),
    })).filter((run) => run.id !== ""),
  };
}

export const fetchEngineCapabilities = () =>
  getJson<unknown>("/api/engine/capabilities").then(normalizeEngineCapabilities);

/** Bounded to the server's 1..50 window so a caller cannot ask for an unbounded list. */
export const fetchEngineRuns = (limit = 20) =>
  getJson<unknown>(`/api/engine/runs?limit=${Math.min(50, Math.max(1, Math.trunc(limit) || 1))}`)
    .then(normalizeEngineRuns);

export const fetchEngineRunSnapshot = (runId: string, after = 0, limit = 200) =>
  getJson<unknown>(
    `/api/engine/runs/${encodeURIComponent(runId)}/snapshot?after=${Math.max(0, after)}&limit=${Math.min(500, Math.max(1, limit))}`,
  ).then(normalizeEngineRunSnapshot);
