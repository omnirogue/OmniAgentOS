/**
 * Connections API client — consumed by the /connections page.
 *
 * Pinned contract (FINAL-PLAN §B):
 *   GET /api/connections
 *   {
 *     "categories": [{
 *       "id": string, "label": string,
 *       "integrations": [{
 *         "id", "name", "logo",
 *         "status": "connected"|"configured"|"not_configured"|"error",
 *         "instances": [{"label", "status"}],
 *         "detail": string, "docs_url": string|null
 *       }]
 *     }],
 *     "connected_count": int, "total_count": int
 *   }
 *
 * When NEXT_PUBLIC_USE_CONNECTIONS_FIXTURES=true, the fixture catalog is
 * rendered in place of the API call — no backend needed for frontend dev.
 */

import { buildFixtureResponse } from "./fixtures";
import type { ConnectionsResponse } from "./types";

export const USE_CONNECTIONS_FIXTURES =
  process.env.NEXT_PUBLIC_USE_CONNECTIONS_FIXTURES === "true";

const DEFAULT_TIMEOUT_MS = 8000;

export class ConnectionsApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ConnectionsApiError";
  }
}

export async function fetchConnections(): Promise<ConnectionsResponse> {
  if (USE_CONNECTIONS_FIXTURES) {
    return buildFixtureResponse();
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    const resp = await fetch("/api/connections", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      cache: "no-store",
    });
    if (!resp.ok) {
      throw new ConnectionsApiError(
        `GET /api/connections failed (${resp.status})`,
        resp.status,
      );
    }
    return (await resp.json()) as ConnectionsResponse;
  } finally {
    clearTimeout(timer);
  }
}
