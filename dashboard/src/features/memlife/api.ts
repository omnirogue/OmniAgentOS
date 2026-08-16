/**
 * API client for the memory-lifecycle review queue.
 *
 * Contract (omniagentos/api/routes/memlife.py):
 *   GET  /api/memlife/queue              → 200 {pending: N} | non-200 {error: store_unavailable}
 *   POST /api/memlife/{id}/graduate|reject|reopen
 *
 * Same-origin through the Next proxy so the browser never holds the session token.
 * The three queue states must stay distinct after parse — a missing store is not empty.
 */

import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import type { MemlifeQueueView } from "./types";

export class MemlifeApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = "MemlifeApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Pure parser for the queue response. Kept separate so tests can pin each state
 * without a network. Never invents `{pending: 0}` from an error body.
 */
export function parseQueueResponse(status: number, body: unknown): MemlifeQueueView {
  if (isRecord(body) && body.error === "store_unavailable") {
    // Backend returns this with a non-200 status. Treat the string as decisive
    // even if a proxy mangled the status — still never collapse to empty.
    return { kind: "store_unavailable" };
  }

  if (status === 200 && isRecord(body) && typeof body.pending === "number") {
    if (!Number.isFinite(body.pending) || body.pending < 0) {
      return { kind: "error", message: "queue response has invalid pending count" };
    }
    return { kind: "ok", pending: body.pending };
  }

  if (status !== 200) {
    let message = `Request failed with status ${status}`;
    if (isRecord(body)) {
      if (typeof body.error === "string") message = body.error;
      else if (isRecord(body.error) && typeof body.error.message === "string") {
        message = body.error.message;
      } else if (typeof body.detail === "string") {
        message = body.detail;
      }
    }
    return { kind: "error", message };
  }

  // 200 without a finite pending count is unparseable — not a true empty queue.
  return { kind: "error", message: "unparseable queue response" };
}

export async function fetchMemlifeQueue(): Promise<MemlifeQueueView> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}/api/memlife/queue`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new MemlifeApiError(reason.message, 0, "timeout");
    }
    throw new MemlifeApiError(
      reason instanceof Error ? reason.message : "network error",
      0,
      "network",
    );
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  const parsed = parseQueueResponse(response.status, body);
  if (parsed.kind === "error" && response.status >= 500 && parsed.message !== "unparseable queue response") {
    // Surface unexpected 5xx as thrown so the page can show a generic ErrorState
    // distinct from the deliberate store_unavailable path.
    if (parsed.message !== "store_unavailable") {
      throw new MemlifeApiError(parsed.message, response.status);
    }
  }
  if (parsed.kind === "error" && response.status >= 400 && response.status < 500) {
    throw new MemlifeApiError(parsed.message, response.status);
  }
  return parsed;
}

export type DecisionBody = {
  decided_by?: string;
  reason?: string;
};

async function postDecision(
  candidateId: string,
  action: "graduate" | "reject" | "reopen",
  body: DecisionBody = {},
): Promise<{ id: string; status: string; lesson_id?: string }> {
  const response = await fetchWithTimeout(
    `${API_BASE}/api/memlife/${encodeURIComponent(candidateId)}/${action}`,
    {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        decided_by: body.decided_by ?? "human",
        reason: body.reason ?? "",
      }),
    },
  );

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    if (isRecord(payload)) {
      if (payload.error === "store_unavailable" || payload.detail === "store_unavailable") {
        throw new MemlifeApiError("store_unavailable", response.status, "store_unavailable");
      }
      if (typeof payload.detail === "string") message = payload.detail;
      else if (isRecord(payload.error) && typeof payload.error.message === "string") {
        message = payload.error.message;
      }
    }
    throw new MemlifeApiError(message, response.status);
  }

  if (!isRecord(payload) || typeof payload.id !== "string" || typeof payload.status !== "string") {
    throw new MemlifeApiError("unparseable decision response", response.status);
  }
  return {
    id: payload.id,
    status: payload.status,
    lesson_id: typeof payload.lesson_id === "string" ? payload.lesson_id : undefined,
  };
}

export function graduateCandidate(id: string, body?: DecisionBody) {
  return postDecision(id, "graduate", body);
}

export function rejectCandidate(id: string, body?: DecisionBody) {
  return postDecision(id, "reject", body);
}

export function reopenCandidate(id: string, body?: DecisionBody) {
  return postDecision(id, "reopen", body);
}
