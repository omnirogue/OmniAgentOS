import type {
  EventHubStatus,
  HealthState,
  ReliabilityWatchSummary,
} from "../../lib/reliabilityContracts";

/** Explicit watch-cursor labels — never `undefineds ago` or silent empty strings. */
export function formatWatchCursorAge(ageS: number | null | undefined): string {
  if (ageS == null || !Number.isFinite(ageS)) return "never run";
  if (ageS <= 0) return "just now";
  return `${ageS}s ago`;
}

/**
 * Incident count tiles: preserve unavailable as a truthful label, never silent 0.
 * Real zero remains "0".
 */
export function formatIncidentCount(count: number | null | undefined): string {
  if (count == null || !Number.isFinite(count)) return "unavailable";
  return String(count);
}

/** Map healthy summary to operator-facing "last known good"; pass through degraded states. */
export function formatOverallHealth(health: HealthState | string | null | undefined): string {
  if (!health || health === "healthy") return "last known good";
  return health;
}

/** Explicit watch component state from reliability-summary.v1.watch. */
export function formatWatchState(
  watch: ReliabilityWatchSummary | null | undefined,
  ageS?: number | null,
): string {
  const state = watch?.state;
  if (state === "never_run") return "never run";
  if (state === "last_known_good") return "last known good";
  if (state === "stale") return "stale";
  if (state === "unavailable") return "unavailable";
  if (state === "current") {
    return formatWatchCursorAge(watch?.age_seconds ?? ageS);
  }
  if (state) return state;
  return formatWatchCursorAge(ageS);
}

/** L12 event hub / eventbus.status operator label. */
export function formatEventHubState(
  hub: EventHubStatus | null | undefined,
): string {
  if (!hub) return "unknown";
  if (hub.degraded || hub.state === "degraded") {
    const reason =
      typeof hub.last_error === "string" && hub.last_error.trim()
        ? ` — ${hub.last_error}`
        : "";
    return `degraded${reason}`;
  }
  if (hub.state === "ok") return "ok";
  return hub.state || "unknown";
}
