import type { CapabilityHealth, CapabilityStatus, HealthFiltersState } from "./types";

/** Broken-first ordering, per the W7 brief: DOWN -> DEGRADED -> STALE ->
 * CANNOT_EVALUATE -> UNVERIFIED -> OK. This is the DEFAULT sort — it must
 * apply with zero user interaction, so it is a pure function the page can
 * call unconditionally on load rather than something gated behind a click. */
export const BROKEN_FIRST_ORDER: CapabilityStatus[] = [
  "DOWN",
  "DEGRADED",
  "STALE",
  "CANNOT_EVALUATE",
  "UNVERIFIED",
  "OK",
];

const STATUS_RANK: Record<CapabilityStatus, number> = Object.fromEntries(
  BROKEN_FIRST_ORDER.map((status, index) => [status, index]),
) as Record<CapabilityStatus, number>;

export function statusRank(status: CapabilityStatus): number {
  return STATUS_RANK[status];
}

/** Stable broken-first sort: within a status, falls back to id for a
 * deterministic, reproducible order (no accidental reliance on registry
 * enumeration order). */
export function sortBrokenFirst(capabilities: CapabilityHealth[]): CapabilityHealth[] {
  return [...capabilities].sort((a, b) => {
    const diff = statusRank(a.status) - statusRank(b.status);
    if (diff !== 0) return diff;
    return a.id.localeCompare(b.id);
  });
}

export type SortField = "status" | "last_checked" | "company" | "name";

/** Explicit sort the user can pick via the Table's sortable columns. Status
 * still sorts broken-first (ascending == the same clinically-worst-first
 * order the default view uses) rather than plain alphabetical, so "sort by
 * status" never contradicts the page's own broken-first framing. */
export function sortCapabilities(capabilities: CapabilityHealth[], field: SortField, direction: "asc" | "desc" = "asc"): CapabilityHealth[] {
  const sorted = [...capabilities].sort((a, b) => {
    let diff: number;
    switch (field) {
      case "status":
        diff = statusRank(a.status) - statusRank(b.status);
        break;
      case "last_checked":
        diff = (a.last_checked ?? "").localeCompare(b.last_checked ?? "");
        break;
      case "company":
        diff = a.company.localeCompare(b.company);
        break;
      case "name":
        diff = a.name.localeCompare(b.name);
        break;
      default:
        diff = 0;
    }
    if (diff !== 0) return diff;
    return a.id.localeCompare(b.id);
  });
  return direction === "desc" ? sorted.reverse() : sorted;
}

export const EMPTY_FILTERS: HealthFiltersState = { company: "", kind: "", status: "" };

/** Filters compose (AND) — company, kind, and status each narrow the set
 * independently. An empty string for a dimension means "no filter" for it. */
export function filterCapabilities(capabilities: CapabilityHealth[], filters: HealthFiltersState): CapabilityHealth[] {
  return capabilities.filter((entry) => {
    if (filters.company && entry.company !== filters.company) return false;
    if (filters.kind && entry.kind !== filters.kind) return false;
    if (filters.status && entry.status !== filters.status) return false;
    return true;
  });
}

export type StatusCounts = Record<CapabilityStatus, number>;

export function countByStatus(capabilities: CapabilityHealth[]): StatusCounts {
  const counts = Object.fromEntries(BROKEN_FIRST_ORDER.map((status) => [status, 0])) as StatusCounts;
  for (const entry of capabilities) counts[entry.status] += 1;
  return counts;
}
