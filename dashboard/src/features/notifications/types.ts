/** Types for the durable notification feed (see omniagentos/api/routes/notifications.py). */

export type NotificationKind =
  | "approval"
  | "alert"
  | "escalation"
  | "done"
  | "blocked"
  | "info"
  | "swarm_failed";

export type NotificationRefType = "approval" | "run" | "session" | "alert" | "board_task" | null;

/** Resolved, client-facing target computed server-side (service.resolve_target).
 * `actionable` is true only for a still-pending approval; `resolved` is true once
 * the referenced approval is decided/expired/gone (or the board card is
 * done/cancelled) — so a stale notification shows an outcome, never a dead link.
 * A `board_task` (done-kind) target carries the card id, its title, and the
 * deliverable count, and `href` deep-links to the card's files drawer. */
export interface NotificationTarget {
  type: NotificationRefType;
  id: string | null;
  resolved: boolean;
  actionable: boolean;
  state: string | null;
  approval_id?: string;
  action_class?: string;
  proposed_action?: string;
  session_id?: string | null;
  run_id?: string | null;
  board_task_id?: string;
  task_title?: string | null;
  files_count?: number | null;
  href?: string;
}

export interface NotificationRow {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string;
  severity: string;
  ref_type: NotificationRefType;
  ref_id: string | null;
  created_at: string;
  read_at: string | null;
  acted_at: string | null;
  read: boolean;
  acted: boolean;
  payload: Record<string, unknown>;
  target: NotificationTarget;
}
