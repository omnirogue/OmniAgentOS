"use client";

import { useCallback, useEffect, useState } from "react";
import { capabilitiesApi } from "./client";
import type { AccessLogEntry, AgentAccess, CapabilitiesCatalog, CreateAgentRequest, ServerInfo } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export function useCapabilitiesCatalog() {
  const [catalog, setCatalog] = useState<CapabilitiesCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setCatalog(await capabilitiesApi.catalog());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load the capability catalogue"));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { catalog, loading, error, refresh };
}

export function useAgentAccess(agentId: string | null, agentName?: string) {
  const [access, setAccess] = useState<AgentAccess | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!agentId) {
      setAccess(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setAccess(await capabilitiesApi.agentAccess(agentId, agentName));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load this agent's access"));
    } finally {
      setLoading(false);
    }
  }, [agentId, agentName]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = useCallback(
    async (granted: string[], note = "") => {
      if (!agentId) throw new Error("No agent selected");
      const updated = await capabilitiesApi.updateAgentCapabilities(agentId, granted, note);
      setAccess(updated);
      return updated;
    },
    [agentId],
  );

  return { access, loading, error, refresh, save };
}

/**
 * The whole agent roster's access, one call (GET /api/access/agents) rather than
 * fetching each agent's grant individually — the roster view backs the capabilities
 * catalogue ("which agents currently hold each capability") and the /agents cards.
 */
export function useAgentAccessRoster() {
  const [byAgentId, setByAgentId] = useState<Record<string, AgentAccess>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const { agents } = await capabilitiesApi.roster();
      setByAgentId(Object.fromEntries(agents.map((a) => [a.agent_id, a])));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load agent access"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { byAgentId, loading, error, refresh };
}

export function useAccessLog(agentId: string | null) {
  const [entries, setEntries] = useState<AccessLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const { entries: rows } = await capabilitiesApi.accessLog(agentId ?? undefined);
      setEntries(rows);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load the access log"));
    } finally {
      setLoading(false);
    }
  }, [agentId]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { entries, loading, error, refresh };
}

/** GET /api/access/servers — the operator's server fleet, for the capabilities page's Servers section. */
export function useServers() {
  const [servers, setServers] = useState<ServerInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const { servers: rows } = await capabilitiesApi.servers();
      setServers(rows);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load the server inventory"));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { servers, loading, error, refresh };
}

export function useCreateAgent() {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const create = useCallback(async (payload: CreateAgentRequest) => {
    setSaving(true);
    setError(null);
    try {
      return await capabilitiesApi.createAgent(payload);
    } catch (reason) {
      const message = errorMessage(reason, "Failed to create the agent");
      setError(message);
      throw new Error(message);
    } finally {
      setSaving(false);
    }
  }, []);
  return { create, saving, error };
}
