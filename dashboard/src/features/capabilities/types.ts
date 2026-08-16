/**
 * Capability / permission system — dashboard types.
 *
 * Mirrors omniagentos/connectors/__init__.py + broker.py + api/routes/access.py
 * (routes verified against the real backend, not the provisional brief paths).
 * `ActionClass` is imported from `@/lib/contracts` (frozen, owned by the lead)
 * rather than redeclared here — it is the exact same enum the run/step timeline
 * already renders, so a capability's risk class reads identically wherever it
 * appears in the app.
 */
import type { ActionClass } from "@/lib/contracts";

export type { ActionClass };

/** GET /api/access/capabilities -> groups[group_id] */
export interface CapabilityGroupInfo {
  label: string;
  danger: boolean;
}

/** GET /api/access/capabilities -> connectors[].capabilities[] */
export interface CapabilityInfo {
  id: string;
  label: string;
  action_class: ActionClass;
  /** false = declared in the registry but no reviewed call path yet — fail-closed, not broken. */
  callable_now: boolean;
  /**
   * true for consequential capabilities (refund/charge/payout/ad-launch/DNS/DB-write/
   * account-delete). The broker refuses these in code even when granted — granting one
   * only lets the agent *request* the action; it always parks for a human.
   */
  always_human: boolean;
}

/** GET /api/access/capabilities -> connectors[] */
export interface ConnectorInfo {
  id: string;
  label: string;
  group: string;
  /** Environment variable NAMES only — never values. The broker never hands these to the UI. */
  env_names: string[];
  capabilities: CapabilityInfo[];
}

/** GET /api/access/capabilities */
export interface CapabilitiesCatalog {
  groups: Record<string, CapabilityGroupInfo>;
  connectors: ConnectorInfo[];
}

/** .../access -> summary (mirrors broker.describe_grant) */
export interface GrantSummary {
  total: number;
  by_action_class: Partial<Record<ActionClass, number>>;
  connectors: string[];
  groups: string[];
  /** Runs unattended and MUTATES our own systems. */
  auto_writes: string[];
  /** A customer/human sees the result; parks for approval before it runs. */
  needs_approval: string[];
  /** Money, ad spend, infra — hard-blocked in code without a human decision. */
  always_human: string[];
  touches_danger_group: string[];
  not_yet_callable: string[];
  env_names: string[];
}

/**
 * GET /api/access/agents/{agent_id}
 * PUT /api/access/agents/{agent_id}
 * (one entry of) GET /api/access/agents -> { agents: AgentAccess[] }
 */
export interface AgentAccess {
  agent_id: string;
  agent_name: string;
  granted: string[];
  summary: GrantSummary;
}

/** GET /api/access/agents -> the roster view, one call instead of N+1. */
export interface AgentAccessRoster {
  agents: AgentAccess[];
}

/** PUT /api/access/agents/{agent_id} body */
export interface SetGrantRequest {
  granted: string[];
  note?: string;
}

/** GET /api/access/log -> entries[] (append-only grant audit trail). */
export interface AccessLogEntry {
  agent_id: string;
  capability_id: string;
  action: "grant" | "revoke";
  action_class: ActionClass | string;
  actor: string;
  note: string;
  ts: string;
}

export interface AccessLogResponse {
  entries: AccessLogEntry[];
}

/** POST /api/collab/agents body */
export interface CreateAgentRequest {
  name: string;
  lineage: string;
  model: string | null;
  expertise: string[];
  trust_level: string;
  granted: string[];
}

/** POST /api/collab/agents -> an agent row (collab store shape) including `id`. */
export interface CreateAgentResponse {
  id: string;
  name: string;
  lineage: string;
  model: string | null;
  expertise: string[];
  trust_level: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
}

/** Shape of the { error: { code, message } } body the API returns on 4xx/5xx. */
export interface ApiErrorBody {
  error?: { code?: string; message?: string };
}

/**
 * GET /api/access/servers -> servers[]
 * Mirrors omniagentos.sessions.ssh_keys.read_server_inventory() -- one row per host
 * documented in vault/servers/inventory.md. `status` is free text starting with
 * "ACTIVE", "EPHEMERAL", or "LEGACY-STALE" (e.g. "ACTIVE (live commerce)"); group by
 * prefix, don't compare for equality. `key` is a key FILENAME/path only
 * (e.g. "~/.ssh/example_a.pem") -- never key material.
 */
export interface ServerInfo {
  alias: string;
  host: string;
  user: string;
  key: string;
  status: string;
  purpose: string;
  sites: string;
}

/** GET /api/access/servers */
export interface ServersResponse {
  servers: ServerInfo[];
}
