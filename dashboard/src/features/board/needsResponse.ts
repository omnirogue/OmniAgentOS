/**
 * Needs Response — conversations that cannot proceed until the operator acts.
 *
 * Mirrors `omniagentos/intake/needs_response.py`. Strict predicate:
 *   status === awaiting_approval
 *   OR pending_approval is bound
 *   OR live work/run_state is awaiting_approval
 *
 * Blocked cards (FAILED) and open suggestions are backlog — not included.
 * Ordering is the product: no client sort control.
 */
import type { LiveBoardTask } from "@/features/collab/types";

const ACTION_CLASS_RANK: Record<string, number> = {
  read_only: 0,
  sandboxed_creation: 1,
  internal_reversible: 2,
  external_reversible: 3,
  consequential: 4,
  irreversible: 5,
};

const PRIORITY_RANK: Record<string, number> = {
  urgent: 0,
  high: 1,
  normal: 2,
  medium: 2,
  low: 3,
};

/** Terminal/backlog statuses never Need Response — even with a stale approval. */
const TERMINAL_OR_BACKLOG = new Set(["blocked", "done", "cancelled", "pending"]);

/** True iff this conversation cannot proceed until the operator acts. */
export function isNeedsResponse(task: LiveBoardTask): boolean {
  if (TERMINAL_OR_BACKLOG.has(task.status)) return false;
  if (task.status === "awaiting_approval") return true;
  if (task.pending_approval) return true;
  if (task.work?.state === "awaiting_approval") return true;
  if (task.run_state === "awaiting_approval") return true;
  return false;
}

function actionClassRank(task: LiveBoardTask): number {
  const actionClass = task.pending_approval?.action_class ?? "";
  return ACTION_CLASS_RANK[actionClass] ?? 2;
}

function waitingSince(task: LiveBoardTask): string {
  const fromApproval = task.pending_approval?.created_at;
  if (fromApproval && fromApproval.trim()) return fromApproval;
  if (task.updated_at && task.updated_at.trim()) return task.updated_at;
  if (task.created_at && task.created_at.trim()) return task.created_at;
  return "9999-12-31T23:59:59Z";
}

function priorityRank(task: LiveBoardTask): number {
  return PRIORITY_RANK[String(task.priority || "normal").toLowerCase()] ?? 2;
}

/** Sort key: riskier first, then oldest wait, then priority, then id. */
export function needsResponseSortKey(task: LiveBoardTask): [number, string, number, string] {
  return [-actionClassRank(task), waitingSince(task), priorityRank(task), task.id];
}

/** Filter to Needs Response and rank. Pure — no I/O. */
export function selectNeedsResponse(tasks: LiveBoardTask[]): LiveBoardTask[] {
  return tasks.filter(isNeedsResponse).sort((a, b) => {
    const ka = needsResponseSortKey(a);
    const kb = needsResponseSortKey(b);
    for (let i = 0; i < ka.length; i += 1) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
    return 0;
  });
}

/**
 * Cockpit / list rank: Needs Response rises above everything else.
 * Lower number sorts first. No sort control in the UI — this IS the order.
 */
export function listAttentionRank(task: LiveBoardTask): number {
  if (isNeedsResponse(task)) return -1;
  if (["claimed", "in_progress"].includes(task.status)) return 0;
  if (["pending", "open"].includes(task.status)) return 1;
  if (task.status === "blocked") return 2;
  if (task.status === "cancelled") return 3;
  return 4; // done last
}
