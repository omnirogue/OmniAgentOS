/**
 * Organization agent-request decision contract (H-17).
 *
 * Backend (`omniagentos/api/routes/org.py`) only accepts approve/reject from
 * status `pending`. Designing / awaiting_approval / decided rows must never
 * expose action buttons — the API returns 409 for those states.
 */

export const OPERATOR_DECIDED_BY = "operator" as const;

/** Backend-valid status that may receive approve/reject. */
export const DECIDABLE_AGENT_REQUEST_STATUS = "pending" as const;

/** Statuses shown in the "open requests" list (informational, not necessarily decidable). */
export const OPEN_AGENT_REQUEST_STATUSES = [
  "pending",
  "designing",
  "awaiting_approval",
] as const;

export function canDecideAgentRequest(status: string | null | undefined): boolean {
  return status === DECIDABLE_AGENT_REQUEST_STATUS;
}

export function isOpenAgentRequest(status: string | null | undefined): boolean {
  return (
    typeof status === "string" &&
    (OPEN_AGENT_REQUEST_STATUSES as readonly string[]).includes(status)
  );
}
