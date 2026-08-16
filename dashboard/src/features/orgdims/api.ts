/** Client for /api/orgdims multidimensional organization APIs. */

import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";

export type GrokAgent = {
  id: string;
  slug: string;
  name: string;
  lineage: string;
  model: string;
  role: string;
  metacog_primary: boolean;
  expertise: string[];
  workstreams: string[];
  risk_ceiling: string;
  charter: string;
  enabled: boolean;
};

export type MatrixView = {
  columns: string[];
  rows: Array<{
    row_key: string;
    counts: Record<string, number>;
    total: number;
    cells: Record<string, Array<Record<string, unknown>>>;
  }>;
  uncategorized: Array<Record<string, unknown>>;
  card_total: number;
};

export type PortfolioView = {
  by_workstream: Record<string, Record<string, number>>;
  by_company: Record<string, Record<string, number>>;
  by_risk: Record<string, number>;
  grok_preferred_cards: number;
  primary_orchestrator: string;
  workstreams: string[];
};

export type LoopTemplate = {
  id: string;
  title: string;
  purpose: string;
  topology: string;
  workstreams: string[];
  risk_classes: string[];
};

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

export const orgdimsApi = {
  health: () => req<Record<string, unknown>>("/api/orgdims/health"),
  seed: () => req<Record<string, unknown>>("/api/orgdims/seed", { method: "POST" }),
  companies: () =>
    req<{ companies: Array<Record<string, unknown>> }>("/api/orgdims/companies"),
  workstreams: () =>
    req<{ workstreams: Array<{ slug: string; label: string; aliases: string[] }> }>(
      "/api/orgdims/workstreams",
    ),
  grokAgents: (primaryOnly = false) =>
    req<{ agents: GrokAgent[]; primary_orchestrator: string }>(
      `/api/orgdims/agents/grok?primary_only=${primaryOnly ? "true" : "false"}`,
    ),
  loops: () => req<{ loops: LoopTemplate[] }>("/api/orgdims/loops"),
  matrix: () => req<MatrixView>("/api/orgdims/views/matrix"),
  portfolio: () => req<PortfolioView>("/api/orgdims/views/portfolio"),
  bulkReclassify: (body?: { only_missing?: boolean; limit?: number }) =>
    req<{
      ok: boolean;
      scanned: number;
      classified: number;
      skipped: number;
      errors: Array<{ id: string; error: string }>;
    }>("/api/orgdims/classify/bulk", {
      method: "POST",
      body: JSON.stringify(body ?? { only_missing: true, limit: 500 }),
    }),
  classifyBoard: (body: {
    task_id: string;
    title?: string;
    description?: string;
    discipline?: string;
    priority?: string;
    company_slug?: string;
    product_slug?: string;
  }) =>
    req<Record<string, unknown>>("/api/orgdims/classify/board_task", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patchBoard: (taskId: string, body: Record<string, unknown>) =>
    req<Record<string, unknown>>(`/api/orgdims/board/${encodeURIComponent(taskId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listObjects: (objectType: "skill" | "agent" | "loop") =>
    req<{ items: Array<Record<string, unknown>> }>(`/api/orgdims/objects/${objectType}`),
  putObject: (objectType: string, objectId: string, dimensions: Record<string, unknown>) =>
    req<Record<string, unknown>>(
      `/api/orgdims/objects/${encodeURIComponent(objectType)}/${encodeURIComponent(objectId)}`,
      { method: "PUT", body: JSON.stringify({ dimensions }) },
    ),
  classifyObject: (body: {
    object_type: string;
    object_id: string;
    title?: string;
    description?: string;
  }) =>
    req<Record<string, unknown>>("/api/orgdims/classify/object", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  recommendLoop: (workstream?: string, risk_class?: string) =>
    req<Record<string, unknown>>("/api/orgdims/loops/recommend", {
      method: "POST",
      body: JSON.stringify({ workstream, risk_class }),
    }),
};
