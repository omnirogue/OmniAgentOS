/**
 * API client. SIGNATURES FROZEN (Wave 0) — p08 implements/extends bodies and may
 * add helpers, but existing exported names/signatures must not change (p09 codes
 * against them concurrently).
 *
 * AC-reliability: `fetchJson` now goes through `fetchWithTimeout` (10s hard
 * client-side timeout) instead of a bare `fetch`, and throws the additive
 * `ApiError` helper below (still an `Error` subclass, so every existing
 * `reason instanceof Error` check keeps working) so callers that care can
 * branch on `.status` — e.g. the Sessions page distinguishing a 401
 * (no local session token in this browser) from a real outage.
 *
 * W8: added generic get/post/put/delete methods for V2 reliability pages.
 */

import {
  API_BASE,
  type Approval,
  type Health,
  type PauseState,
  type RunDetail,
  type RunSummary,
  type Session,
  type TaskRow,
} from "./contracts";
import { fetchWithTimeout, FetchTimeoutError } from "./fetchTimeout";

/** Additive helper (see file doc comment) — not part of the frozen surface,
 * just a richer error type `fetchJson` throws internally. `status === 0`
 * means the request never reached the server (network failure or timeout). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetchWithTimeout(url, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new ApiError("The API did not respond in time — it may be down or overloaded.", 0);
    }
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new ApiError(`Could not reach the API: ${detail}`, 0);
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      detail = res.statusText;
    }
    throw new ApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as T;
}

function req<T>(path: string, init?: RequestInit): Promise<T> {
  return fetchJson<T>(`${API_BASE}${path}`, init);
}

// SEC-005: decide/kill hit a same-origin Next.js route handler (dashboard/src/app/api/**)
// instead of the FastAPI server directly, so the local session token stays server-side
// and never reaches the browser. Path shape mirrors the FastAPI route on purpose.
function proxyReq<T>(path: string, init?: RequestInit): Promise<T> {
  return fetchJson<T>(path, init);
}

export const api = {
  health: () => req<Health>("/api/health"),
  pause: () => req<PauseState>("/api/pause"),
  // fix6: PUT /api/pause is token-gated server-side, so the mutation goes through
  // the same-origin Next.js route handler (which attaches the token) — the GET
  // stays a direct FastAPI read.
  setPause: (paused: boolean, reason?: string) =>
    proxyReq<PauseState>("/api/pause", { method: "PUT", body: JSON.stringify({ paused, reason }) }),
  tasks: (params?: Record<string, string>) =>
    req<TaskRow[]>(`/api/tasks${qs(params)}`),
  runs: (params?: Record<string, string>) =>
    req<RunSummary[]>(`/api/runs${qs(params)}`),
  run: (id: string) => req<RunDetail>(`/api/runs/${id}`),
  sessions: (state?: string) => req<Session[]>(`/api/sessions${state ? qs({ state }) : ""}`),
  session: (id: string) => req<Session>(`/api/sessions/${encodeURIComponent(id)}`),
  runTranscript: async (id: string) => (await req<RunDetail>(`/api/runs/${encodeURIComponent(id)}`)).events,
  sessionTranscript: (id: string) => req<unknown[]>(`/api/sessions/${encodeURIComponent(id)}/transcript`),
  steerSession: (id: string, message: string) =>
    proxyReq<{ ok: boolean; message_id: string; queued_at: string }>(`/api/sessions/${encodeURIComponent(id)}/message`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  killSession: (id: string) => proxyReq<Session>(`/api/sessions/${encodeURIComponent(id)}/kill`, { method: "POST", body: "{}" }),
  // Additive (scp-ui-0815): POST /api/sessions/{id}/update {title?, company?}
  // -> full Session. Same-origin proxy per SEC-005; a null `company` clears
  // the operator's manual override.
  updateSession: (id: string, patch: { title?: string; company?: string | null }) =>
    proxyReq<Session>(`/api/sessions/${encodeURIComponent(id)}/update`, {
      method: "POST",
      body: JSON.stringify(patch),
    }),
  // fix6: POST /api/runs/{id}/cancel is token-gated server-side, so it goes
  // through the same-origin proxy that attaches the local session token.
  cancelRun: (id: string) =>
    proxyReq<{ ok: boolean }>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" }),
  approvals: (state = "pending") => req<Approval[]>(`/api/approvals?state=${state}`),
  decideApproval: (id: string, decision: "approved" | "rejected", note?: string) =>
    proxyReq<Approval>(`/api/approvals/${encodeURIComponent(id)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, note, decided_by: "operator" }),
    }),
  // W8: Generic methods for V2 reliability pages (proxy through same-origin)
  get: <T = unknown>(path: string) => proxyReq<T>(path),
  post: <T = unknown>(path: string, body?: Record<string, unknown>) =>
    proxyReq<T>(path, { method: "POST", body: JSON.stringify(body || {}) }),
  put: <T = unknown>(path: string, body?: Record<string, unknown>) =>
    proxyReq<T>(path, { method: "PUT", body: JSON.stringify(body || {}) }),
  delete: <T = unknown>(path: string) => proxyReq<T>(path, { method: "DELETE" }),
};

function qs(params?: Record<string, string>): string {
  if (!params || Object.keys(params).length === 0) return "";
  return `?${new URLSearchParams(params).toString()}`;
}
