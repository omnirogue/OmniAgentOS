import { API_BASE } from "@/lib/contracts";
import { apiUrl } from "@/lib/apiRoute";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import {
  FIXTURE_CATALOG,
  FIXTURE_SERVERS,
  fixtureAgentAccess,
  fixtureAllAgentAccess,
  fixtureCreateAgent,
  fixtureLogEntries,
  fixtureUpdateAgentAccess,
  USE_FIXTURES,
} from "./fixtures";
import type {
  AccessLogResponse,
  AgentAccess,
  AgentAccessRoster,
  CapabilitiesCatalog,
  CreateAgentRequest,
  CreateAgentResponse,
  ServersResponse,
} from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // fix7: agent-access mutations (PUT/POST) go same-origin (token proxy).
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      detail = res.statusText;
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export const capabilitiesApi = {
  /** GET /api/access/capabilities */
  catalog: () =>
    USE_FIXTURES ? Promise.resolve(FIXTURE_CATALOG) : req<CapabilitiesCatalog>("/api/access/capabilities"),

  /** GET /api/access/agents — the whole roster in one call, not N+1. */
  roster: () =>
    USE_FIXTURES
      ? Promise.resolve<AgentAccessRoster>({ agents: fixtureAllAgentAccess() })
      : req<AgentAccessRoster>("/api/access/agents"),

  /** GET /api/access/agents/{agent_id} */
  agentAccess: (agentId: string, agentName?: string) =>
    USE_FIXTURES
      ? Promise.resolve(fixtureAgentAccess(agentId, agentName))
      : req<AgentAccess>(`/api/access/agents/${encodeURIComponent(agentId)}`),

  /** PUT /api/access/agents/{agent_id} */
  updateAgentCapabilities: (agentId: string, granted: string[], note = "") =>
    USE_FIXTURES
      ? Promise.resolve(fixtureUpdateAgentAccess(agentId, granted, note))
      : req<AgentAccess>(`/api/access/agents/${encodeURIComponent(agentId)}`, {
          method: "PUT",
          body: JSON.stringify({ granted, note }),
        }),

  /** GET /api/access/log?agent_id=... — append-only grant audit trail. */
  accessLog: (agentId?: string) =>
    USE_FIXTURES
      ? Promise.resolve<AccessLogResponse>({ entries: fixtureLogEntries(agentId) })
      : req<AccessLogResponse>(`/api/access/log${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`),

  /** GET /api/access/servers — the operator's server fleet, from vault/servers/inventory.md. */
  servers: () =>
    USE_FIXTURES
      ? Promise.resolve<ServersResponse>({ servers: FIXTURE_SERVERS })
      : req<ServersResponse>("/api/access/servers"),

  /** POST /api/collab/agents */
  createAgent: (payload: CreateAgentRequest) =>
    USE_FIXTURES
      ? Promise.resolve(fixtureCreateAgent(payload))
      : req<CreateAgentResponse>("/api/collab/agents", { method: "POST", body: JSON.stringify(payload) }),
};
