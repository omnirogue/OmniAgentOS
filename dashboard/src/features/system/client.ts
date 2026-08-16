/**
 * System-surface fetchers. Reads go through `api.get` (same-origin Next proxy,
 * which attaches the session token server-side for the token-gated
 * /api/system/* reads). The single mutation — PATCH an agent's editable
 * frontmatter — posts to the dedicated same-origin proxy route
 * (app/api/system/agents/[name]/route.ts), mirroring the kill-route pattern.
 */

import { api } from "@/lib/api";
import type {
  ActivityResponse,
  AgentsResponse,
  ImproversResponse,
  PatchAgentBody,
  PatchAgentResult,
  PatchImproverPromptResult,
  SkillsResponse,
  SystemMap,
} from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseAgentPatchResult(value: unknown): PatchAgentResult {
  if (
    !isRecord(value) ||
    !isRecord(value.agent) ||
    typeof value.committed !== "boolean" ||
    !Array.isArray(value.changed) ||
    !value.changed.every((entry) => typeof entry === "string")
  ) {
    throw new Error("unparseable agent update response");
  }
  return value as PatchAgentResult;
}

function parseImproverPromptResult(value: unknown): PatchImproverPromptResult {
  if (
    !isRecord(value) ||
    typeof value.label !== "string" ||
    typeof value.prompt_path !== "string" ||
    typeof value.committed !== "boolean" ||
    typeof value.bytes !== "number" ||
    !Number.isFinite(value.bytes) ||
    value.bytes < 0
  ) {
    throw new Error("unparseable improver prompt response");
  }
  return value as PatchImproverPromptResult;
}

export const fetchSystemMap = () => api.get<SystemMap>("/api/system/map");
export const fetchSystemAgents = () => api.get<AgentsResponse>("/api/system/agents");
export const fetchSystemSkills = () => api.get<SkillsResponse>("/api/system/skills");
export const fetchSystemImprovers = () => api.get<ImproversResponse>("/api/system/improvers");

export function fetchAgentActivity(params: {
  agent?: string;
  day?: string;
  limit?: number;
}): Promise<ActivityResponse> {
  const search = new URLSearchParams();
  if (params.agent) search.set("agent", params.agent);
  if (params.day) search.set("day", params.day);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return api.get<ActivityResponse>(`/api/system/agent-activity${qs ? `?${qs}` : ""}`);
}

export async function patchSystemAgent(
  name: string,
  body: PatchAgentBody,
): Promise<PatchAgentResult> {
  const res = await fetch(`/api/system/agents/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const parsed = await res.json();
      detail = parsed?.error?.message ?? JSON.stringify(parsed);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return parseAgentPatchResult(await res.json());
}

/**
 * Overwrite an improver's editable system prompt. Posts to the dedicated
 * same-origin proxy route (app/api/system/improvers/[label]/prompt/route.ts),
 * kill-route pattern — the session token is attached server-side and the target
 * file path is resolved server-side from launchd discovery, never sent here.
 */
export async function patchImproverPrompt(
  label: string,
  content: string,
): Promise<PatchImproverPromptResult> {
  const res = await fetch(`/api/system/improvers/${encodeURIComponent(label)}/prompt`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const parsed = await res.json();
      detail = parsed?.error?.message ?? JSON.stringify(parsed);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return parseImproverPromptResult(await res.json());
}
