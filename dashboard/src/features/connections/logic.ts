/**
 * Connections — pure logic for search, status rollup, and derived views.
 *
 * All functions are pure (no side effects) so they can be unit-tested against
 * fixture data. The page uses these via useMemo to guarantee instant filtering.
 */

import type {
  ConnectionCategory,
  ConnectionIntegration,
  ConnectionStatus,
  ConnectionsResponse,
} from "./types";

/**
 * Apply a case-insensitive search query across categories and integrations.
 * An empty query returns the input unchanged. Categories with no remaining
 * integrations are dropped from the output.
 */
export function filterConnections(
  response: ConnectionsResponse,
  query: string,
): ConnectionsResponse {
  const q = query.trim().toLowerCase();
  if (!q) return response;

  const categories: ConnectionCategory[] = response.categories
    .map((cat) => {
      const matches = cat.integrations.filter((i) => {
        return (
          i.name.toLowerCase().includes(q) ||
          i.id.toLowerCase().includes(q) ||
          cat.label.toLowerCase().includes(q) ||
          i.instances.some((inst) => inst.label.toLowerCase().includes(q))
        );
      });
      return { ...cat, integrations: matches };
    })
    .filter((cat) => cat.integrations.length > 0);

  const total = categories.reduce((acc, c) => acc + c.integrations.length, 0);
  const connected = categories.reduce(
    (acc, c) =>
      acc + c.integrations.filter((i) => i.status === "connected").length,
    0,
  );
  return { categories, connected_count: connected, total_count: total };
}

/**
 * Status badge tone mapping — drives the ds-badge CSS class on the chip.
 *
 *   connected     -> "ok"         (green, success)
 *   configured    -> "running"    (blue, info — some keys are present, keep going)
 *   not_configured-> "neutral"   (gray, resting)
 *   error         -> "warn"      (yellow, needs attention)
 */
export function statusBadgeTone(
  status: ConnectionStatus,
): "ok" | "running" | "neutral" | "warn" {
  switch (status) {
    case "connected":
      return "ok";
    case "configured":
      return "running";
    case "not_configured":
      return "neutral";
    case "error":
      return "warn";
  }
}

/**
 * Roll a per-integration status label used in the summary chip and the
 * accessibility text on the tile. For single-instance integrations this is
 * the STATUS_LABEL; for multi-instance it reports the connected count.
 */
export function statusSummaryLabel(integration: ConnectionIntegration): string {
  if (integration.instances.length === 0) {
    switch (integration.status) {
      case "connected":
        return "Connected";
      case "configured":
        return "Partially configured";
      case "not_configured":
        return "Not configured";
      case "error":
        return "Vault error";
    }
  }
  const connectedCount = integration.instances.filter(
    (i) => i.status === "connected",
  ).length;
  return `${connectedCount}/${integration.instances.length} instances`;
}

/**
 * Flat list of all integrations across categories, preserving category order.
 * Useful for ⌘K palette and keyboard navigation.
 */
export function flattenIntegrations(
  response: ConnectionsResponse,
): { categoryLabel: string; integration: ConnectionIntegration }[] {
  const out: { categoryLabel: string; integration: ConnectionIntegration }[] = [];
  for (const cat of response.categories) {
    for (const integ of cat.integrations) {
      out.push({ categoryLabel: cat.label, integration: integ });
    }
  }
  return out;
}
