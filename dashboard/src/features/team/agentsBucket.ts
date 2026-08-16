/**
 * "Agents & unowned" bucket assembly — pure, no React/fetch (P5 brief:
 * bucket-mapping tests). `GET /api/team/board` only ever returns buckets for
 * employees on the roster (`TeamStore.team_queues`: "an agent card with no
 * owner_employee_id belongs to no person's queue"), so it never carries the
 * agent-claimed or genuinely-unowned cards this section is named for. This
 * module fills that gap client-side from the already-fetched reconciled
 * board (`GET /api/board`, via `useLiveBoard`), reimplementing the SAME
 * bucket mapping the server uses (`_QUEUE_BUCKETS` in
 * omniagentos/team/store.py) ONLY for the cards that endpoint excludes.
 */
import type { LiveBoardTask } from "@/features/collab/types";
import type { TeamQueueBuckets, TeamQueueCard, TeamQueueCounts } from "./types";

function emptyCounts(): TeamQueueCounts {
  return { ready: 0, active: 0, blocked: 0, review: 0, done_today: 0 };
}

function emptyBuckets(employeeId: string): TeamQueueBuckets {
  return {
    employee_id: employeeId,
    ready: [],
    active: [],
    blocked: [],
    review: [],
    done_today: [],
    counts: emptyCounts(),
    ready_below_5: false,
  };
}

/** Mirrors `_QUEUE_BUCKETS`: open→ready, claimed/in_progress→active,
 * blocked→blocked, awaiting_approval→review, done (updated today)→done_today.
 * Everything else (pending/cancelled, or done on an earlier day) has no
 * bucket, exactly like the server. `today` is UTC `YYYY-MM-DD`, injectable
 * for tests. */
export function bucketKeyForStatus(
  status: string,
  updatedAt: string,
  today: string,
): keyof TeamQueueCounts | null {
  switch (status) {
    case "open":
      return "ready";
    case "claimed":
    case "in_progress":
      return "active";
    case "blocked":
      return "blocked";
    case "awaiting_approval":
      return "review";
    case "done":
      return updatedAt.slice(0, 10) === today ? "done_today" : null;
    default:
      return null;
  }
}

function toQueueCard(task: LiveBoardTask): TeamQueueCard {
  return {
    id: task.id,
    title: task.title,
    ref: task.ref ?? null,
    status: task.status,
    size: task.size ?? "M",
    // Additive QueueCard fields (2026-08-13) — mirrored here so client-built
    // agent cards render the same chips as server-built ones.
    owner_employee_id: task.owner_employee_id ?? null,
    priority: task.priority,
    company_slug: task.company_slug ?? null,
  };
}

/**
 * Merge every OTHER employee's server bucket (on the roster, but not one of
 * the three sectioned people — Frank today) with every genuinely unowned live
 * card, bucketed the same way the server buckets a person's queue. Archived
 * cards are excluded (the server's own bucket query does the same). Returns
 * `null` when there is nothing to show, so the caller can render nothing
 * rather than an empty shell.
 */
export function buildAgentsBucket(
  otherBuckets: TeamQueueBuckets[],
  liveTasks: LiveBoardTask[],
  today: string = new Date().toISOString().slice(0, 10),
  poolCardIds: ReadonlySet<string> = new Set(),
): TeamQueueBuckets | null {
  const merged = emptyBuckets("__agents__");
  for (const bucket of otherBuckets) {
    for (const key of ["ready", "active", "blocked", "review", "done_today"] as const) {
      merged[key].push(...bucket[key].filter((card) => !poolCardIds.has(card.id)));
    }
  }
  for (const task of liveTasks) {
    if (task.owner_employee_id) continue; // owned — already in a person's bucket
    if (task.archived) continue;
    if (poolCardIds.has(task.id)) continue; // rendered in Pool, never twice
    const key = bucketKeyForStatus(task.status, task.updated_at, today);
    if (key) merged[key].push(toQueueCard(task));
  }
  const total =
    merged.ready.length + merged.active.length + merged.blocked.length +
    merged.review.length + merged.done_today.length;
  if (total === 0) return null;
  merged.counts = {
    ready: merged.ready.length,
    active: merged.active.length,
    blocked: merged.blocked.length,
    review: merged.review.length,
    done_today: merged.done_today.length,
  };
  return merged;
}
