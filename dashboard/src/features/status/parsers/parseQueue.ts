export interface QueueSummary {
  wip: number | null;
  wip_cap: number | null;
  wip_degraded: boolean;
  wip_degraded_detail: string | null;
  rebuilt_at: string | null;
}

/**
 * Extracts ONLY the top-level truth fields from `queue.json` — never the
 * `items` array (2,900+ entries; the point of this function existing is
 * that the caller already decided not to look at it).
 *
 * `wip` passes through absence honestly as `null`. A missing or
 * non-numeric `wip` must NEVER default to 0 — the queue's own
 * `wip_definition` says ledger damage (a torn tail / discarded lines) sets
 * `wip_degraded` and *omits* `wip` specifically so downstream readers
 * refuse the guessed count instead of quietly reporting "queue empty".
 */
export function summarizeQueue(raw: unknown): QueueSummary {
  const obj = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const detail = obj.wip_degraded_detail;
  return {
    wip: typeof obj.wip === "number" ? obj.wip : null,
    wip_cap: typeof obj.wip_cap === "number" ? obj.wip_cap : null,
    wip_degraded: obj.wip_degraded === true,
    wip_degraded_detail: typeof detail === "string" && detail.length > 0 ? detail : null,
    rebuilt_at: typeof obj.rebuilt_at === "string" ? obj.rebuilt_at : null,
  };
}
