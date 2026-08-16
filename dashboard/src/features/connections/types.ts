/**
 * Connections feature — type definitions.
 *
 * Mirrors the pinned GET /api/connections contract in FINAL-PLAN.md §B.
 */

export type ConnectionStatus =
  | "connected"
  | "configured"
  | "not_configured"
  | "error";

export interface ConnectionInstance {
  label: string;
  status: ConnectionStatus;
}

export interface ConnectionIntegration {
  id: string;
  name: string;
  logo: string;
  status: ConnectionStatus;
  instances: ConnectionInstance[];
  detail: string;
  docs_url: string | null;
  /** Populated by the fixture fallback — the real API does not return these. */
  unlocks?: string;
}

export interface ConnectionCategory {
  id: string;
  label: string;
  integrations: ConnectionIntegration[];
}

export interface ConnectionsResponse {
  categories: ConnectionCategory[];
  connected_count: number;
  total_count: number;
}

/** Status label mapping — status value -> human-readable tone label. */
export const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connected: "Connected",
  configured: "Configured",
  not_configured: "Not configured",
  error: "Error",
};
