/**
 * ONE progress model for every "how is my work going?" surface — the /board
 * summary strip and the cockpit's Live work header.
 *
 * The unit is a REQUESTED TASK, never a sub-task. A swarm run counts as ONE
 * requested task no matter how many member cards its planner produced; a plain
 * intake/longhaul card counts as one. This is the whole point: the cockpit used
 * to count raw board cards, so a single "write three utility modules" ask read
 * as 6 queued + 11 in a category chip, and the operator could not tell how many
 * things they had actually asked for.
 *
 * Sub-task volume is still reported — as `subtasks`, a separate rollup — so a
 * surface can say "3 of 5 requests done · 18/40 sub-tasks" without ever mixing
 * the two counts into one number.
 *
 * Pure functions: no React, no fetch.
 */
import type { LiveBoardTask } from "@/features/collab/types";
import { columnFor, type PhaseOverlay, type SwarmColumnId } from "@/features/swarm/columns";
import { groupSwarmRuns, type SwarmRunGroup } from "@/features/swarm/runAggregate";
import { isNeedsResponse } from "./needsResponse";

/** The three in-flight phase columns roll up into "running" for a summary. */
const RUNNING_COLUMNS: ReadonlySet<SwarmColumnId> = new Set([
  "running",
  "review",
  "testing",
  "integration",
]);
const QUEUED_COLUMNS: ReadonlySet<SwarmColumnId> = new Set(["backlog", "ready"]);

export interface BoardSummary {
  /** Requested tasks: one per swarm run + one per solo card. */
  total: number;
  done: number;
  running: number;
  queued: number;
  blocked: number;
  /** Parked on a human decision — Needs Response (§2). Not folded into blocked. */
  needsResponse: number;
  /** Percent of requested tasks completed (0 when nothing is on the board). */
  pct: number;
  /** Sub-task rollup across swarm runs — reported, never merged into `total`. */
  subtasks: { done: number; total: number };
  /** Swarm runs on the board, for "· N swarms" style copy. */
  runCount: number;
}

export const EMPTY_BOARD_SUMMARY: BoardSummary = {
  total: 0,
  done: 0,
  running: 0,
  queued: 0,
  blocked: 0,
  needsResponse: 0,
  pct: 0,
  subtasks: { done: 0, total: 0 },
  runCount: 0,
};

function tally(summary: BoardSummary, column: SwarmColumnId): void {
  if (column === "completed") summary.done += 1;
  else if (column === "blocked") summary.blocked += 1;
  else if (column === "needs_you") summary.needsResponse += 1;
  else if (RUNNING_COLUMNS.has(column)) summary.running += 1;
  else if (QUEUED_COLUMNS.has(column)) summary.queued += 1;
}

/**
 * Roll a board feed up to requested-task granularity.
 *
 * `groups`/`solo` are returned alongside the counts so a caller that also needs
 * to RENDER the rows does not group the same feed twice (and cannot drift out
 * of sync with the numbers in its own header).
 */
export function summarizeBoard(
  tasks: LiveBoardTask[],
  overlay: PhaseOverlay = new Map(),
): BoardSummary & { groups: SwarmRunGroup[]; solo: LiveBoardTask[] } {
  const { groups, solo } = groupSwarmRuns(tasks, overlay);
  const summary: BoardSummary = {
    ...EMPTY_BOARD_SUMMARY,
    subtasks: { done: 0, total: 0 },
    runCount: groups.length,
  };
  for (const group of groups) {
    // Prefer Needs Response when any member is parked on the operator — the
    // aggregate runColumn can lag and report "running" while a member is parked.
    const groupNeedsYou =
      group.column === "needs_you" ||
      group.members.some((member) => isNeedsResponse(member)) ||
      (group.root ? isNeedsResponse(group.root) : false);
    tally(summary, groupNeedsYou ? "needs_you" : group.column);
    summary.subtasks.done += group.done;
    summary.subtasks.total += group.total;
  }
  for (const task of solo) {
    // Align strip with the queue predicate: pending_approval / work.state lag
    // must not leave the strip at 0 while the Needs Response band shows the card.
    if (isNeedsResponse(task)) summary.needsResponse += 1;
    else tally(summary, columnFor(task, overlay));
  }
  summary.total = groups.length + solo.length;
  summary.pct = summary.total > 0 ? Math.round((summary.done / summary.total) * 100) : 0;
  return { ...summary, groups, solo };
}

/** "1 needs you · 3 active · 1 queued · 2 blocked · 5 done · 4 swarms". */
export function summaryLine(summary: BoardSummary, doneLabel = "done"): string {
  const parts: string[] = [];
  if (summary.needsResponse > 0) {
    parts.push(
      `${summary.needsResponse} need${summary.needsResponse === 1 ? "s" : ""} you`,
    );
  }
  parts.push(
    `${summary.running} active`,
    `${summary.queued} queued`,
    `${summary.blocked} blocked`,
    `${summary.done} ${doneLabel}`,
  );
  if (summary.runCount) {
    parts.push(`${summary.runCount} swarm${summary.runCount === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}
