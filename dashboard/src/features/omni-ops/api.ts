/** Defensive fetch client for the Grok operator control-plane.
 * Reads go direct (no session token); mutations route through the same-origin
 * proxy so the session token is attached server-side (fix7 pattern). */

import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import type {
  GrokGrant,
  GrokHealth,
  GrokInteraction,
  GrokInteractionAnswer,
} from "./types";

export class GrokOpsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "GrokOpsApiError";
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown, fallback = ""): string {
  return typeof v === "string" && v.length > 0 ? v : fallback;
}
function nullableStr(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}
function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function normalizeInteraction(raw: unknown): GrokInteraction | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    work_ref_type: str(raw.work_ref_type, "unknown"),
    work_ref_id: str(raw.work_ref_id),
    direction: str(raw.direction, "user_to_agent"),
    kind: str(raw.kind, "nudge"),
    body: str(raw.body),
    blocking_policy: str(raw.blocking_policy, "none"),
    // No "pending" fallback: it is not a member of the schema's status union,
    // so it silently disabled every status-driven control. `status` is NOT NULL
    // in `agent_interactions`; a payload without one is malformed, and the
    // restrictive reading is "unknown" — not answerable, not resolved.
    status: str(raw.status, "unknown"),
    session_id: nullableStr(raw.session_id),
    author: nullableStr(raw.author),
    parent_id: nullableStr(raw.parent_id),
    expires_at: nullableStr(raw.expires_at),
    created_at: str(raw.created_at),
    delivered_at: nullableStr(raw.delivered_at),
    metadata: isRecord(raw.metadata) ? raw.metadata : {},
  };
}

function normalizeGrant(raw: unknown): GrokGrant | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    capability: str(raw.capability),
    label: str(raw.label),
    project_id: nullableStr(raw.project_id),
    approval_id: str(raw.approval_id),
    max_actions: num(raw.max_actions),
    actions_used: num(raw.actions_used),
    max_spend_usd: num(raw.max_spend_usd),
    spend_used_usd: num(raw.spend_used_usd),
    expires_at: str(raw.expires_at),
    status: str(raw.status, "active"),
    metadata: isRecord(raw.metadata) ? raw.metadata : {},
    created_at: str(raw.created_at),
  };
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new GrokOpsApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const grokOpsApi = {
  async health(): Promise<GrokHealth> {
    const raw = await req<unknown>("/api/grok/health");
    if (!isRecord(raw)) {
      return { product: "OmniAgentOS", separate: true, surfaces: [], decision_center: { implemented: false, status: "unknown", handoff: "unknown" } };
    }
    return {
      product: str(raw.product, "OmniAgentOS"),
      separate: raw.separate === true,
      surfaces: Array.isArray(raw.surfaces) ? raw.surfaces.filter((s): s is string => typeof s === "string") : [],
      decision_center: isRecord(raw.decision_center)
        ? {
            implemented: raw.decision_center.implemented === true,
            status: str(raw.decision_center.status, "unknown"),
            handoff: str(raw.decision_center.handoff, "unknown"),
          }
        : { implemented: false, status: "unknown", handoff: "unknown" },
    };
  },

  async interactions(filters?: {
    work_ref_type?: string;
    work_ref_id?: string;
    session_id?: string;
    blocking_only?: boolean;
  }): Promise<GrokInteraction[]> {
    const params = new URLSearchParams();
    if (filters?.work_ref_type) params.set("work_ref_type", filters.work_ref_type);
    if (filters?.work_ref_id) params.set("work_ref_id", filters.work_ref_id);
    if (filters?.session_id) params.set("session_id", filters.session_id);
    if (filters?.blocking_only) params.set("blocking_only", "true");
    const qs = params.toString();
    const raw = await req<unknown[]>(`/api/grok/interactions${qs ? `?${qs}` : ""}`);
    return Array.isArray(raw) ? raw.map(normalizeInteraction).filter((i): i is GrokInteraction => i !== null) : [];
  },

  async answerInteraction(id: string, body: string, author?: string): Promise<GrokInteractionAnswer | null> {
    const raw = await req<unknown>(`/api/grok/interactions/${encodeURIComponent(id)}/answer`, {
      method: "POST",
      body: JSON.stringify({ body, author: author ?? undefined }),
    });
    if (!isRecord(raw)) return null;
    return {
      id: str(raw.id, id),
      body: str(raw.body),
      author: nullableStr(raw.author),
      created_at: str(raw.created_at),
    };
  },

  async grants(filters?: { capability?: string; project_id?: string }): Promise<GrokGrant[]> {
    const params = new URLSearchParams();
    if (filters?.capability) params.set("capability", filters.capability);
    if (filters?.project_id) params.set("project_id", filters.project_id);
    const qs = params.toString();
    const raw = await req<unknown[]>(`/api/grok/grants${qs ? `?${qs}` : ""}`);
    return Array.isArray(raw) ? raw.map(normalizeGrant).filter((g): g is GrokGrant => g !== null) : [];
  },

  async revokeGrant(grantId: string, reason = "operator_revoke"): Promise<GrokGrant | null> {
    const raw = await req<unknown>(
      `/api/grok/grants/${encodeURIComponent(grantId)}/revoke${reason ? `?reason=${encodeURIComponent(reason)}` : ""}`,
      { method: "POST" },
    );
    return normalizeGrant(raw);
  },
};
