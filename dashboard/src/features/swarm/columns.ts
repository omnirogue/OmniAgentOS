/**
 * The vision columns of the combined /board kanban and the mapping from the
 * EXISTING BoardTaskStatus (+ the card's swarm phase) onto them. There is NO
 * parallel status store: Review / Testing / Integration are phase overlays read
 * off the card's `swarm_phase` (or the live overview overlay) while a card is
 * `in_progress`; a card with no swarm metadata degrades into Running — which is
 * what lets plain intake/longhaul cards share these columns. Pure functions —
 * no React, no fetch.
 *
 * `swarm_phase` / `swarm_integration` arrive as top-level fields, projected out
 * of `board_tasks.swarm_json` server-side; board lists no longer ship the raw
 * envelope. Readers below still fall back to it for payloads that do.
 */
import type { BoardTaskStatus, LiveBoardTask } from "@/features/collab/types";
import type { SwarmBoardTask, SwarmFleet, SwarmJson, SwarmRunStatus } from "./types";

/** Any live board card — plain intake/longhaul cards have no swarm metadata and
 * degrade cleanly through the mapping below. The combined /board passes these
 * directly; /swarm passes the swarm-typed subset. */
type BoardCard = LiveBoardTask | SwarmBoardTask;

function swarmJsonOf(task: BoardCard): SwarmBoardTask["swarm_json"] {
  return (task as SwarmBoardTask).swarm_json;
}

export type SwarmColumnId =
  | "backlog"
  | "ready"
  | "running"
  | "needs_you"
  | "review"
  | "testing"
  | "integration"
  | "completed"
  | "cancelled"
  | "blocked";

export interface SwarmColumn {
  id: SwarmColumnId;
  label: string;
  /** A real theme token (see design/theme.css) — never a raw hex. */
  tone: string;
}

export const VISION_COLUMNS: SwarmColumn[] = [
  { id: "backlog", label: "Backlog", tone: "var(--text-faint)" },
  { id: "ready", label: "Ready", tone: "var(--accent)" },
  { id: "running", label: "Running", tone: "var(--running)" },
  { id: "needs_you", label: "Needs you", tone: "var(--awaiting)" },
  { id: "review", label: "In Review", tone: "var(--awaiting)" },
  { id: "testing", label: "Testing", tone: "var(--validating)" },
  { id: "integration", label: "Integration", tone: "var(--promote)" },
  { id: "completed", label: "Completed", tone: "var(--ok)" },
  { id: "cancelled", label: "Cancelled", tone: "var(--text-faint)" },
  { id: "blocked", label: "Blocked", tone: "var(--danger)" },
];

const terminalTaskStatuses = ["blocked", "done", "cancelled"] as const satisfies readonly BoardTaskStatus[];
const terminalColumns = ["completed", "cancelled", "blocked"] as const satisfies readonly SwarmColumnId[];

/** Frozen cross-work-package contracts: board task values, not run-state aliases. */
export const TERMINAL_TASK_STATUSES: ReadonlySet<string> = new Set(terminalTaskStatuses);
export const TERMINAL_COLUMNS: ReadonlySet<string> = new Set(terminalColumns);

// "blocked" is parked-and-resumable, NOT settled: it belongs in TERMINAL_TASK_STATUSES
// (fetch gating, resume affordance) but must never be auto-hidden or counted as done.
const settledTaskStatuses = ["done", "cancelled"] as const satisfies readonly BoardTaskStatus[];
export const SETTLED_TASK_STATUSES: ReadonlySet<string> = new Set(settledTaskStatuses);

const resumableTaskStatuses = ["blocked", "cancelled"] as const satisfies readonly BoardTaskStatus[];
export const RESUMABLE_TASK_STATUSES: ReadonlySet<string> = new Set(resumableTaskStatuses);

export interface BoardColumnCount {
  total: number;
  completedToday: number;
}

function emptyColumnCounts(): Record<SwarmColumnId, BoardColumnCount> {
  return Object.fromEntries(
    VISION_COLUMNS.map((column) => [column.id, { total: 0, completedToday: 0 }]),
  ) as Record<SwarmColumnId, BoardColumnCount>;
}

export function isSameLocalDay(value: string, now: Date): boolean {
  const updatedAt = new Date(value);
  if (!Number.isFinite(updatedAt.getTime()) || !Number.isFinite(now.getTime())) return false;
  return (
    updatedAt.getFullYear() === now.getFullYear() &&
    updatedAt.getMonth() === now.getMonth() &&
    updatedAt.getDate() === now.getDate()
  );
}

/** Count the already-filtered board payload by its rendered vision column. Used
 * only on the non-aggregated renderer path. */
export function columnCounts(
  tasks: readonly LiveBoardTask[],
  overlay?: PhaseOverlay,
  now: Date = new Date(),
): Record<SwarmColumnId, BoardColumnCount> {
  const counts = emptyColumnCounts();
  for (const task of tasks) {
    const column = columnFor(task, overlay);
    counts[column].total += 1;
    if (task.status === "done" && isSameLocalDay(task.updated_at, now)) {
      counts[column].completedToday += 1;
    }
  }
  return counts;
}

export interface SwarmPhasePresentation {
  column: "review" | "testing" | "integration";
  label: string;
  tone: "awaiting" | "validating" | "promote";
}

const REVIEW_PHASE: SwarmPhasePresentation = { column: "review", label: "In review", tone: "awaiting" };
const TESTING_PHASE: SwarmPhasePresentation = { column: "testing", label: "Testing", tone: "validating" };
const INTEGRATION_PHASE: SwarmPhasePresentation = { column: "integration", label: "Integration", tone: "promote" };
const PHASE_PRESENTATIONS: Readonly<Record<string, SwarmPhasePresentation>> = {
  review: REVIEW_PHASE,
  in_review: REVIEW_PHASE,
  reviewing: REVIEW_PHASE,
  "in review": REVIEW_PHASE,
  testing: TESTING_PHASE,
  test: TESTING_PHASE,
  verify: TESTING_PHASE,
  verifying: TESTING_PHASE,
  integration: INTEGRATION_PHASE,
  integrating: INTEGRATION_PHASE,
  integrate: INTEGRATION_PHASE,
  merge: INTEGRATION_PHASE,
  merging: INTEGRATION_PHASE,
};

/** The one phase-to-column/label/tone projection used by columns and badges. */
export function swarmPhasePresentation(
  phase: string | null | undefined,
): SwarmPhasePresentation | null {
  if (!phase) return null;
  return PHASE_PRESENTATIONS[phase.trim().toLowerCase()] ?? null;
}

/** Parse board_tasks.swarm_json, which may arrive as a JSON string (TEXT column)
 * or an already-parsed object depending on the serializer. Never throws.
 *
 * Board LIST payloads no longer carry the envelope at all (see
 * `SwarmBoardTask.swarm_json`) — this is the FALLBACK for run-detail/single-card
 * rows that still do. Read phases through `swarmPhase`, not this. */
export function parseSwarmJson(value: SwarmBoardTask["swarm_json"]): SwarmJson {
  if (!value) return {};
  if (typeof value === "object") return value;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as SwarmJson) : {};
  } catch {
    return {};
  }
}

/** A live `task_id -> phase` overlay, sourced from `GET /api/swarm/overview`'s
 * `tasks` (C2). The scheduler knows a running card's phase (review/testing/…)
 * before it is ever mirrored into `swarm_json`, so the overlay wins when present
 * and the board's own `swarm_json.swarm_phase` is the fallback. */
export type PhaseOverlay = ReadonlyMap<string, string>;

export function swarmPhase(task: BoardCard, overlay?: PhaseOverlay): string | null {
  // Precedence: live overlay > server-projected `swarm_phase` (board lists) >
  // the raw envelope (run-detail rows, which still ship it whole).
  const phase =
    overlay?.get(task.id) ??
    (task as SwarmBoardTask).swarm_phase ??
    parseSwarmJson(swarmJsonOf(task)).swarm_phase;
  return typeof phase === "string" && phase.trim() ? phase.trim().toLowerCase() : null;
}

export function isIntegrationTask(task: BoardCard): boolean {
  // Projected flag wins; `json_extract` renders the JSON boolean as 1/0, so
  // accept either. Only fall back to the envelope when the card has no
  // projection (null/undefined = "the server did not say").
  const projected = (task as SwarmBoardTask).swarm_integration;
  if (projected !== undefined && projected !== null) return Boolean(projected);
  return parseSwarmJson(swarmJsonOf(task)).integration === true;
}

/** Map one board card onto its vision column. An optional live phase overlay
 * (overview.tasks) drives the Review/Testing/Integration split for in-flight
 * cards; without it the card's own `swarm_phase` is used. */
export function columnFor(task: BoardCard, overlay?: PhaseOverlay): SwarmColumnId {
  switch (task.status) {
    case "pending":
      return "backlog";
    case "open":
      return "ready";
    case "awaiting_approval":
      // Parked on a human decision. Deliberately NOT phase-overlaid: whatever the
      // swarm thinks it is doing, nothing advances until someone decides.
      return "needs_you";
    case "blocked":
      return "blocked";
    case "done":
      return "completed";
    case "cancelled":
      return "cancelled";
    case "claimed":
    case "in_progress": {
      const phase = swarmPhase(task, overlay);
      const presentation = swarmPhasePresentation(phase);
      if (presentation) return presentation.column;
      if (isIntegrationTask(task)) return "integration";
      // No swarm metadata (or an unrecognized phase) -> Running.
      return "running";
    }
    default:
      return "running";
  }
}

/** A fleet row as shown in the top strip — either derived from board cards or
 * overlaid with `GET /api/swarm` metadata. */
export interface FleetRunView {
  id: string;
  shortId: string;
  status: SwarmRunStatus | null;
  goal: string | null;
  /** Per-vision-column task counts (0 for API-only rows with no cards yet). */
  counts: Record<SwarmColumnId, number>;
  done: number;
  total: number;
  costUsd: number | null;
  budgetUsdMax: number | null;
  /** Where the row's authority came from. */
  source: "board" | "api";
}

function emptyCounts(): Record<SwarmColumnId, number> {
  return {
    backlog: 0,
    ready: 0,
    running: 0,
    needs_you: 0,
    review: 0,
    testing: 0,
    integration: 0,
    completed: 0,
    cancelled: 0,
    blocked: 0,
  };
}

export function shortRunId(id: string): string {
  const tail = id.startsWith("swr_") ? id.slice(4) : id;
  return tail.length > 8 ? tail.slice(0, 8) : tail;
}

/** Group swarm board cards by `swarm_run_id` into fleet rows with per-column
 * counts and a done/total for the progress bar. Cards without a run id are
 * ignored (they are not part of any swarm). */
export function deriveBoardRuns(
  tasks: SwarmBoardTask[],
  overlay?: PhaseOverlay,
): FleetRunView[] {
  const byRun = new Map<string, SwarmBoardTask[]>();
  for (const task of tasks) {
    const runId = task.swarm_run_id;
    if (!runId) continue;
    const bucket = byRun.get(runId);
    if (bucket) bucket.push(task);
    else byRun.set(runId, [task]);
  }
  return [...byRun.entries()]
    .map(([id, runTasks]) => {
      const counts = emptyCounts();
      for (const task of runTasks) counts[columnFor(task, overlay)] += 1;
      const total = runTasks.length;
      const done = counts.completed;
      return {
        id,
        shortId: shortRunId(id),
        status: null,
        goal: null,
        counts,
        done,
        total,
        costUsd: null,
        budgetUsdMax: null,
        source: "board" as const,
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));
}

/**
 * Merge the board-derived fleet rows (authoritative per-column counts) with the
 * `GET /api/swarm` fleet, when the endpoint exists. API metadata (status, goal,
 * cost, budget) overlays the matching board row; API runs with no board cards
 * yet — e.g. `queued` runs held by fleet admission control — are appended as
 * count-less rows so the strip still shows them. Returns the board rows
 * unchanged when `fleet` is null (the C1 default until WP6a).
 */
export function mergeFleet(
  boardRuns: FleetRunView[],
  fleet: SwarmFleet | null,
): FleetRunView[] {
  if (!fleet) return boardRuns;
  const byId = new Map<string, FleetRunView>(boardRuns.map((run) => [run.id, run]));
  for (const summary of fleet.runs) {
    const existing = byId.get(summary.id);
    if (existing) {
      existing.status = summary.status ?? existing.status;
      existing.goal = summary.goal ?? existing.goal;
      existing.costUsd = summary.cost_usd ?? existing.costUsd;
      existing.budgetUsdMax = summary.budget_usd_max ?? existing.budgetUsdMax;
      continue;
    }
    const total = summary.progress?.total ?? 0;
    const done = summary.progress?.done ?? 0;
    const counts = emptyCounts();
    counts.completed = done;
    byId.set(summary.id, {
      id: summary.id,
      shortId: shortRunId(summary.id),
      status: summary.status ?? null,
      goal: summary.goal ?? null,
      counts,
      done,
      total,
      costUsd: summary.cost_usd ?? null,
      budgetUsdMax: summary.budget_usd_max ?? null,
      source: "api",
    });
  }
  return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id));
}
