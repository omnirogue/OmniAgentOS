/**
 * Dashboard view-model for GET /api/memlife/queue.
 *
 * Mirrors the backend's three distinct outcomes — never collapse them:
 *   - store present, N pending → { kind: "ok", pending: N }  (N > 0)
 *   - store present, empty     → { kind: "ok", pending: 0 }
 *   - store missing/unreadable → { kind: "store_unavailable" }
 *
 * Candidate statuses follow omniagentos.memlife.contracts.CandidateStatus.
 * Unknown/absent status is representable and is never a success.
 */

/** Known candidate lifecycle values from the Python contracts. */
export const CANDIDATE_STATUSES = [
  "staged",
  "graduated",
  "rejected",
  "reopened",
  "quarantined",
] as const;

export type KnownCandidateStatus = (typeof CANDIDATE_STATUSES)[number];

export type MemlifeQueueView =
  | { kind: "ok"; pending: number }
  | { kind: "store_unavailable" }
  | { kind: "error"; message: string };
