/** Client for Graph Runtime V2 + Cognitive Budget Manager APIs. */

import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export type GraphView = {
  run_id: string;
  title?: string;
  status?: string;
  completeness_policy?: string;
  template_slug?: string;
  critical_path?: string[];
  nodes: Array<{
    key: string;
    type: string;
    title: string;
    status: string;
    model_role?: string;
    outputs?: Record<string, string>;
    inputs?: Record<string, string>;
    error?: string | null;
  }>;
  edge_flow: Array<{
    from: string;
    to: string;
    from_port: string;
    to_port: string;
    required: boolean;
    artifact_id?: string | null;
    status: string;
  }>;
  artifact_count?: number;
  cost_usd?: number;
};

export const graphApi = {
  health: () => req<Record<string, unknown>>("/api/graph/health"),
  templates: () =>
    req<{ templates: Array<Record<string, unknown>> }>("/api/graph/templates"),
  runs: () => req<{ runs: Array<Record<string, unknown>> }>("/api/graph/runs"),
  view: (runId: string) => req<GraphView>(`/api/graph/runs/${encodeURIComponent(runId)}/view`),
  demoDiamond: (title = "Dashboard diamond") =>
    req<{ ok: boolean; status: string; run: { id: string; status: string } }>(
      "/api/graph/demo/diamond",
      { method: "POST", body: JSON.stringify({ title }) },
    ),
};

export const cbmApi = {
  health: () => req<Record<string, unknown>>("/api/cbm/health"),
  rungs: () =>
    req<{ rungs: Array<Record<string, unknown>>; live: boolean }>("/api/cbm/rungs"),
  leaderboard: () =>
    req<{ leaderboard: Array<Record<string, unknown>> }>("/api/cbm/leaderboard"),
  allocate: (body: Record<string, unknown>) =>
    req<{ ok: boolean; allocation: Record<string, unknown> }>("/api/cbm/allocate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
