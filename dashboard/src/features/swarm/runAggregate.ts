import type { LiveBoardTask } from "@/features/collab/types";
import type { PhaseOverlay, SwarmColumnId } from "./columns";
import type { SwarmBoardTask, SwarmRunAttempt, SwarmRunAttempts } from "./types";

/**
 * ONE card per swarm run (user directive): the kanban must never break a run
 * into its member task cards. This module groups the board feed by
 * `swarm_run_id` and derives the aggregate the run card renders — progress,
 * column, preview — from the member cards it already has in hand. Session /
 * model / cost stats ride in later from `GET /api/swarm/{id}` (see
 * useSwarmRunDetails) so the card renders instantly and enriches when the
 * detail lands.
 */

export interface SwarmRunGroup {
  runId: string;
  /** The run's root card (`Swarm: <goal>`; swarm_runs.board_task_id) if present. */
  root: LiveBoardTask | null;
  /** Member task cards, root excluded. */
  members: LiveBoardTask[];
  title: string;
  done: number;
  total: number;
  /** Active member "Now: …" lines — the card preview. */
  preview: string[];
  running: number;
  blocked: number;
  column: SwarmColumnId;
}

const ROOT_PREFIX = "Swarm: ";

function isRootCard(task: LiveBoardTask): boolean {
  return task.title.startsWith(ROOT_PREFIX);
}

function memberColumnVotes(members: LiveBoardTask[], overlay: PhaseOverlay): SwarmColumnId | null {
  // An ACTIVE phase (review/testing/integration) from the live overlay wins for
  // the whole run — it is the run's current stage, exactly like /swarm showed.
  const active = members.filter((task) => ["claimed", "in_progress"].includes(task.status));
  for (const phase of ["integration", "testing", "review"] as const) {
    if (active.some((task) => overlay.get(task.id) === phase)) {
      return phase;
    }
  }
  return null;
}

export function runColumn(group: Omit<SwarmRunGroup, "column">, overlay: PhaseOverlay): SwarmColumnId {
  const statuses = new Set(group.members.map((task) => task.status));
  const rootStatus = group.root?.status;
  // Parked on the operator rises above phase/running votes — a swarm with any
  // member awaiting approval is Needs Response, not "still running".
  if (statuses.has("awaiting_approval") || rootStatus === "awaiting_approval") {
    return "needs_you";
  }
  const phase = memberColumnVotes(group.members, overlay);
  if (phase) return phase;
  if (group.running > 0) return "running";
  if (rootStatus === "cancelled" || (group.total > 0 && statuses.size === 1 && statuses.has("cancelled"))) {
    return "cancelled";
  }
  // The run itself FAILED. `SwarmDal.close_out_run_cards` lands a failed run's
  // still-live cards on `blocked` and leaves already-terminal ones alone, so a
  // run that died after its members finished — or before any member card
  // existed — moves ONLY its root card. Without this branch `group.blocked` is
  // 0, every later test misses, and the run falls through to `backlog`: the
  // board files a failure as work that never started. `columnFor` maps a lone
  // `blocked` card onto Blocked; the run card is that run's only card here, so
  // it has to agree.
  if (rootStatus === "blocked") return "blocked";
  if (group.total > 0 && group.done >= group.total && (!rootStatus || rootStatus === "done")) {
    return "completed";
  }
  if (rootStatus === "done") return "completed";
  if (group.blocked > 0) return "blocked";
  if (statuses.has("open") || statuses.has("claimed")) return "ready";
  if (group.total === 0 && rootStatus === "in_progress") return "running";
  return "backlog";
}

export interface SwarmAggregation {
  groups: SwarmRunGroup[];
  /** Cards with no swarm_run_id — rendered as plain task cards. */
  solo: LiveBoardTask[];
}

export function groupSwarmRuns(tasks: LiveBoardTask[], overlay: PhaseOverlay): SwarmAggregation {
  const byRun = new Map<string, LiveBoardTask[]>();
  const solo: LiveBoardTask[] = [];
  for (const task of tasks) {
    const runId = (task as SwarmBoardTask).swarm_run_id ?? null;
    if (!runId) {
      solo.push(task);
      continue;
    }
    const bucket = byRun.get(runId);
    if (bucket) bucket.push(task);
    else byRun.set(runId, [task]);
  }
  const groups: SwarmRunGroup[] = [];
  for (const [runId, cards] of byRun) {
    const root = cards.find(isRootCard) ?? null;
    const members = cards.filter((task) => task !== root);
    const done = members.filter((task) => task.status === "done").length;
    const running = members.filter((task) => ["claimed", "in_progress"].includes(task.status)).length;
    const blocked = members.filter((task) => task.status === "blocked").length;
    const preview = members
      .filter((task) => ["claimed", "in_progress"].includes(task.status))
      .map((task) => task.work?.current_step ?? task.title)
      .filter((line): line is string => Boolean(line))
      .slice(0, 2);
    const partial = {
      runId,
      root,
      members,
      title: root ? root.title.slice(ROOT_PREFIX.length) : members[0]?.title ?? runId,
      done,
      total: members.length,
      preview,
      running,
      blocked,
    };
    groups.push({ ...partial, column: runColumn(partial, overlay) });
  }
  // Live runs first, then most recently created.
  groups.sort((a, b) => (b.running - a.running) || b.runId.localeCompare(a.runId));
  return { groups, solo };
}

export function elapsedLabel(startedAt: string | null | undefined, finishedAt?: string | null): string | null {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  const end = finishedAt ? Date.parse(finishedAt) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * `GET /api/swarm/{id}`'s `attempts` is a MAP keyed by board-task id, not a
 * flat array (see `SwarmRunAttempts`). Every consumer goes through this so a
 * shape it did not expect degrades to `[]` instead of throwing mid-render and
 * white-screening the board. Tolerates null/undefined and non-array buckets.
 */
export function flattenAttempts(attempts: SwarmRunAttempts | null | undefined): SwarmRunAttempt[] {
  if (!attempts) return [];
  if (Array.isArray(attempts)) return attempts;
  if (typeof attempts !== "object") return [];
  return Object.values(attempts).flatMap((bucket) => (Array.isArray(bucket) ? bucket : []));
}

/** Distinct models across a run's attempts, live ones first. */
export function attemptModels(attempts: SwarmRunAttempts | null | undefined): string[] {
  const live: string[] = [];
  const done: string[] = [];
  for (const attempt of flattenAttempts(attempts)) {
    const model = attempt.model?.trim();
    if (!model) continue;
    const bucket = attempt.end_reason == null ? live : done;
    if (!bucket.includes(model)) bucket.push(model);
  }
  return [...live, ...done.filter((model) => !live.includes(model))];
}

export function liveSessionCount(attempts: SwarmRunAttempts | null | undefined): number {
  return flattenAttempts(attempts).filter(
    (attempt) => attempt.end_reason == null && attempt.session_id,
  ).length;
}
