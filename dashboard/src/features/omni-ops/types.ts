/** Client contract for the Grok operator control-plane (SEPARATE-PRODUCT).
 *
 * Mirrors omniagentos/api/routes/grok_ops.py. Only the GET surface + the
 * answer/revoke mutations the dashboard exposes are typed here. */

/** The closed union of `agent_interactions.status`, as declared by the CHECK
 * constraint in `omniagentos/db/migrations/058_execution_contract.sql`. Kept
 * here so client-side status logic is an allowlist over the real union rather
 * than an ad-hoc string comparison — `InteractionsInbox` previously tested for
 * `"pending"`, which the schema forbids, and so could never answer anything.
 * `InteractionsInbox.test.tsx` reads the CHECK out of that migration and fails
 * if this list drifts from it. */
export const OMNIAGENTOS_INTERACTION_STATUSES = [
  "active",
  "delivered",
  "answered",
  "canceled",
  "expired",
] as const;

export type GrokInteractionStatus = (typeof OMNIAGENTOS_INTERACTION_STATUSES)[number];

/** Mirrors the server's own answerability predicate — `InteractionsStore.create_answer`
 * updates `WHERE id = ? AND status IN ('active', 'delivered')`
 * (`omniagentos/interactions/store.py`). An allowlist, so a status this client
 * does not recognise is not answerable rather than silently permitted. */
const ANSWERABLE_STATUSES: ReadonlySet<string> = new Set<GrokInteractionStatus>([
  "active",
  "delivered",
]);

/** True iff the store would accept an answer for an interaction in this state. */
export function isAnswerableInteractionStatus(status: string): boolean {
  return ANSWERABLE_STATUSES.has(status);
}

export interface GrokInteraction {
  id: string;
  work_ref_type: string;
  work_ref_id: string;
  direction: string;
  kind: string;
  body: string;
  blocking_policy: string;
  status: string;
  session_id: string | null;
  author: string | null;
  parent_id: string | null;
  expires_at: string | null;
  created_at: string;
  delivered_at: string | null;
  metadata: Record<string, unknown>;
}

export interface GrokGrant {
  id: string;
  capability: string;
  label: string;
  project_id: string | null;
  approval_id: string;
  max_actions: number;
  actions_used: number;
  max_spend_usd: number;
  spend_used_usd: number;
  expires_at: string;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface GrokInteractionAnswer {
  id: string;
  body: string;
  author: string | null;
  created_at: string;
}

export interface GrokHealth {
  product: string;
  separate: boolean;
  surfaces: string[];
  decision_center: {
    implemented: boolean;
    status: string;
    handoff: string;
  };
}
