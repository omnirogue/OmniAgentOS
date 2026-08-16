/**
 * Pure filtering + run-option derivation for the combined live board (P5). The
 * board is the ONE kanban: every card — plain intake, longhaul, or swarm
 * member — renders across the ten VISION_COLUMNS, and these client-side filters
 * (title / discipline / category / project / swarm-run) narrow the set.
 * No React, no fetch — trivially testable.
 *
 * The lane filter is GONE with the select that drove it: lane duplicated the
 * Swarm badge already on every card. The company filter is BACK (multi-company
 * Work OS, 2026-08-13) — but on the server-truth `task.company_slug` (card →
 * goal → org_company), not the old org-envelope field the board stopped
 * rendering; a chip group on /board drives it.
 */
import type { LiveBoardTask } from "@/features/collab/types";
import type { SwarmBoardTask } from "@/features/swarm/types";

/** Sentinel `owner` value for "Agents" — every card with no
 * `owner_employee_id` (agent-claimed or genuinely unowned), NOT a real
 * employee id, so it can never collide with one on the roster. */
export const AGENTS_OWNER_FILTER = "__agents__";

export interface BoardFilters {
  discipline: string;
  titleQuery: string;
  categoryId: string;
  runId: string;
  /** Project scope — narrows the board to cards belonging to one project. */
  projectId: string;
  /** Team Work OS owner scope: "" = All, an employee id = that person,
   * `AGENTS_OWNER_FILTER` = every unowned/agent card. */
  owner: string;
  /** Company scope — "" = All, else an org_companies.slug matched against
   * `task.company_slug` (absent on an un-upgraded server ⇒ never matches). */
  companyId: string;
}

export const EMPTY_BOARD_FILTERS: BoardFilters = {
  discipline: "",
  titleQuery: "",
  categoryId: "",
  runId: "",
  projectId: "",
  owner: "",
  companyId: "",
};

function swarmRunId(task: LiveBoardTask): string | null {
  return (task as SwarmBoardTask).swarm_run_id ?? null;
}

/** Does `task` match the owner filter? `""` (All) always matches;
 * `AGENTS_OWNER_FILTER` matches a null/undefined `owner_employee_id`; any
 * other value is an exact employee-id match. Exported standalone (not just
 * folded into `filterBoardTasks`) so the owner chips can compute per-chip
 * counts against the same predicate the filter itself uses. */
export function matchesOwnerFilter(task: LiveBoardTask, owner: string): boolean {
  if (!owner) return true;
  if (owner === AGENTS_OWNER_FILTER) return !task.owner_employee_id;
  return task.owner_employee_id === owner;
}

/** Apply every active filter (empty fields are no-ops). Order is cheap→dear. */
export function filterBoardTasks(tasks: LiveBoardTask[], filters: BoardFilters): LiveBoardTask[] {
  const query = filters.titleQuery.trim().toLowerCase();
  return tasks.filter((task) => {
    if (filters.projectId && task.project_id !== filters.projectId) return false;
    if (filters.companyId && task.company_slug !== filters.companyId) return false;
    if (filters.discipline && task.discipline !== filters.discipline) return false;
    if (filters.categoryId && task.category_id !== filters.categoryId) return false;
    if (filters.runId && swarmRunId(task) !== filters.runId) return false;
    if (!matchesOwnerFilter(task, filters.owner)) return false;
    if (query && !task.title.toLowerCase().includes(query)) return false;
    return true;
  });
}

export interface RunOption {
  id: string;
  label: string;
}

function shortRun(id: string): string {
  const tail = id.startsWith("swr_") ? id.slice(4) : id;
  return tail.slice(0, 8);
}

/**
 * The distinct swarm runs present on the board, for the run-select. Labels use
 * the fleet goal string when known (GET /api/swarm via mergeFleet), else a short
 * run id. Sorted by id for a stable option order.
 */
export function deriveRunOptions(
  tasks: LiveBoardTask[],
  goals?: ReadonlyMap<string, string | null | undefined>,
): RunOption[] {
  const ids = new Set<string>();
  for (const task of tasks) {
    const runId = swarmRunId(task);
    if (runId) ids.add(runId);
  }
  return [...ids].sort().map((id) => {
    const goal = goals?.get(id);
    const label = goal && goal.trim() ? goal.trim() : `Run ${shortRun(id)}`;
    return { id, label: label.length > 48 ? `${label.slice(0, 47)}…` : label };
  });
}

/**
 * Option for a `?run=` deep link whose run has no cards on the board (a stale
 * link, or a finished run whose cards were archived). Surfacing it in the run
 * select keeps the URL-seeded filter visible and clearable instead of an
 * invisible dead-end filter behind a "No matching cards" wall.
 */
export function missingRunOption(runId: string): RunOption {
  return { id: runId, label: `Run ${shortRun(runId)} (not on board)` };
}

/** Matches auto-discovered observational cards like
 * "[claude · external] youruser · ttys020" — terminal-session sightings
 * surfaced by the discovery pipeline, not real filed tasks. Hidden by
 * default on the board (operator complaint: "tasks that don't exist");
 * exported standalone so the toggle can compute a count against the same
 * predicate the filter uses. */
export function isObservationalCard(title: string): boolean {
  return /^\[[^\]]+ · external\]/.test(title);
}
