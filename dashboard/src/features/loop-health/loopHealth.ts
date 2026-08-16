/**
 * Pure derivation for the `/today` Loops card (R8 — terminal-grade
 * visibility). React-free and network-free so every branch is unit
 * testable without a DOM.
 *
 * Reads ONLY fields already returned by three endpoints that already exist
 * in `contracts/openapi.json` — `GET /api/routines`, `GET /api/routines/runs`
 * (the cross-routine aggregate), `GET /api/approvals?state=pending` — and
 * changes no response shape.
 *
 * Doctrine encoded here (see UI-REVIEW-fable.md / UI-REVIEW-kimi.md, both
 * 2026-08-01, and SUCCESS-CRITERIA-AND-TESTS.md §R8):
 *   - parked/needs-you is AMBER, informational — parking is the system
 *     working, never red.
 *   - disabled is grey, dimmed but present — never silently "forgotten".
 *   - auto_paused is RED — the loop broke.
 *   - a routine whose newest run is older than 2x its own cron interval
 *     renders STALE, never its last known status — a dead scheduler must
 *     look broken, not fine.
 *   - `acceptance_rate === null` renders "no judged runs", never "0%".
 *   - no composite "health score" — every state is legible on its own.
 */

import { nextCronFire } from "@/features/routines/countdown";
import type { RecentRunItem, Routine } from "@/features/routines/types";
import type { Approval } from "@/lib/contracts";
import { isLoopRoutine, loopInstanceId } from "./isLoopRoutine";

/** The `approvals.risk` string that marks a row as loop-runtime-owned
 * (mirrors `omniagentos_loops/approvals.py::LOOP_APPROVAL_KIND`). */
const LOOP_APPROVAL_RISK = "loop_approval";

/** Cells rendered in the tick strip — "≈3 hours of a 10-minute loop" (Kimi). */
const TICK_STRIP_SIZE = 20;

/** A routine whose newest run is older than this multiple of its own cron
 * interval renders STALE (brief, verbatim). */
const STALE_MULTIPLIER = 2;

export type LoopState =
  | "needs_you"
  | "stale"
  | "auto_paused"
  | "disabled"
  | "never_fired"
  | "ok";

/** Dot/badge color family. `neutral` = grey (dimmed, not alarming). */
export type LoopTone = "ok" | "warn" | "danger" | "neutral";

/** Worst (most in need of attention) first — drives row ordering. */
const STATE_PRIORITY: Record<LoopState, number> = {
  needs_you: 0,
  stale: 1,
  auto_paused: 2,
  disabled: 3,
  never_fired: 4,
  ok: 5,
};

export const STATE_TONE: Record<LoopState, LoopTone> = {
  needs_you: "warn", // parked/pending approval — the system working, not broken
  stale: "danger", // no data — must look broken, never fine
  auto_paused: "danger",
  disabled: "neutral", // dimmed but present
  never_fired: "danger", // active but has produced zero runs ever
  ok: "ok",
};

export const STATE_LABEL: Record<LoopState, string> = {
  needs_you: "Needs you",
  stale: "STALE",
  auto_paused: "Auto-paused",
  disabled: "Disabled",
  never_fired: "Never fired",
  ok: "OK",
};

/**
 * Three-valued (plus pending) per-tick outcome, derived client-side from the
 * only fields `RecentRunItem` carries (`gate_passed`, `accepted`,
 * `finished_at`) — mirrors the common-case branches of the backend's own
 * `omniagentos.scheduler.routines.classify_run_outcome`.
 *
 * KNOWN LIMITATION, accepted deliberately: the backend's full classifier
 * also reads `stop_reason` to catch neutral-by-reason-code rows whose
 * `gate_passed`/`accepted` are non-null (e.g. a specific park/idle code).
 * `RecentRunItem` does not carry `stop_reason` — adding it is a response
 * shape change, out of scope for this lane (same tradeoff the fable review
 * accepted for approval matching). In practice this only mis-classifies
 * that one edge case; "settled with no verdict" (the common park/idle
 * shape) is still caught exactly. `parked` and `idle` are therefore NOT
 * distinguishable from each other client-side (both are `neutral`) — the
 * tick strip and last-outcome label say "Parked/idle", not one or the other.
 */
export type TickOutcome = "favourable" | "neutral" | "adverse" | "pending";

export function classifyTickOutcome(run: RecentRunItem): TickOutcome {
  if (!run.finished_at) return "pending";
  if (run.gate_passed === null && run.accepted === null) return "neutral";
  if (run.accepted === true) return "favourable";
  return "adverse";
}

export const TICK_TONE: Record<TickOutcome, LoopTone> = {
  favourable: "ok",
  neutral: "warn",
  adverse: "danger",
  pending: "neutral",
};

export const TICK_LABEL: Record<TickOutcome, string> = {
  favourable: "Completed",
  neutral: "Parked/idle",
  adverse: "Aborted",
  pending: "Running",
};

export type LoopTick = {
  runId: string | null;
  outcome: TickOutcome;
  finishedAt: string | null;
};

export type LoopHealthRow = {
  routine: Routine;
  instanceId: string;
  state: LoopState;
  tone: LoopTone;
  stateLabel: string;
  /** Most recent tick's outcome, or `null` when the loop has never run. */
  lastOutcome: TickOutcome | null;
  lastOutcomeLabel: string;
  /** Oldest → newest, capped at `TICK_STRIP_SIZE`. */
  ticks: LoopTick[];
  /** `null` = no judged runs yet (empty denominator) — never render as 0%. */
  acceptanceRate: number | null;
  acceptanceText: string;
  /** `null` = UNKNOWN (at least one contributing run's cost was never
   * reported — migration 119). Never render as `$0.00`; see totalCostText. */
  totalCostUsd: number | null;
  totalCostText: string;
  /** Human line describing freshness — "STALE — …" or "last fired …". */
  stalenessText: string;
  isStale: boolean;
  pendingApprovals: Approval[];
};

export type LoopHealthResult = {
  loops: LoopHealthRow[];
  /** `risk === "loop_approval"` approvals that match no known loop instance
   * id (a rename, or a loop the routines list doesn't carry). Never
   * dropped — surfaced separately so a rename degrades to "unattributed",
   * never to silence. */
  unmatchedLoopApprovals: Approval[];
};

/** `mm`/`h mm`/`d h` — no seconds, no fractional units. */
export function formatDuration(ms: number): string {
  const totalMinutes = Math.max(0, Math.round(ms / 60_000));
  if (totalMinutes < 1) return "<1m";
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
}

/**
 * The routine's own cadence in ms, measured around `from` so a lopsided
 * cron (e.g. `0 3 * * *` checked at 2am) still reports its true ~24h
 * interval rather than the minutes until its next fire. `null` when the
 * cron is unparseable — mirrors `nextCronFire`'s own null contract so a
 * caller never fabricates an interval for an expression it can't read.
 */
export function cronIntervalMs(cron: string, from: Date): number | null {
  const first = nextCronFire(cron, from);
  if (!first) return null;
  const second = nextCronFire(cron, first);
  if (!second) return null;
  const interval = second.getTime() - first.getTime();
  return interval > 0 ? interval : null;
}

function acceptanceText(rate: number | null): string {
  if (rate === null) return "no judged runs";
  return `${Math.round(rate * 100)}% accepted`;
}

/**
 * ISSUE-8: `null` means UNKNOWN, never a manufactured `$0.00` — same
 * convention as `features/routines/format.ts`'s `costPerAcceptedChangeLabel`.
 * A genuinely exact zero (every contributing run reported a real, known
 * cost that summed to zero) still renders as "$0.00 total".
 */
export function totalCostText(totalCostUsd: number | null): string {
  if (totalCostUsd === null) return "unknown total cost";
  return `$${totalCostUsd.toFixed(2)} total`;
}

function matchesLoop(approval: Approval, instanceId: string): boolean {
  return (
    approval.risk === LOOP_APPROVAL_RISK &&
    approval.proposed_action.startsWith(`${instanceId}/`)
  );
}

function routineCron(routine: Routine): string | null {
  if (routine.trigger_type !== "cron") return null;
  const cron = routine.trigger_config?.cron;
  return typeof cron === "string" ? cron : null;
}

function buildRow(
  routine: Routine,
  runs: RecentRunItem[],
  loopApprovals: Approval[],
  now: Date,
): LoopHealthRow {
  const instanceId = loopInstanceId(routine);
  const routineRuns = runs.filter((run) => run.routine_id === routine.id);
  // `runs` arrives newest-first (the cross-routine aggregate's own
  // contract); take the most recent N, then present oldest -> newest so the
  // strip reads left-to-right like a timeline.
  const ticks: LoopTick[] = routineRuns
    .slice(0, TICK_STRIP_SIZE)
    .map((run) => ({
      runId: run.run_id,
      outcome: classifyTickOutcome(run),
      finishedAt: run.finished_at,
    }))
    .reverse();
  const lastOutcome = ticks.length > 0 ? ticks[ticks.length - 1]!.outcome : null;

  const pendingApprovals = loopApprovals.filter((approval) => matchesLoop(approval, instanceId));

  const cron = routineCron(routine);
  let isStale = false;
  let stalenessText: string;
  if (routine.status !== "active") {
    // Staleness is only meaningful for a routine that is SUPPOSED to be
    // ticking. A disabled loop not firing is expected, not broken.
    stalenessText = routine.last_fired
      ? `last fired ${formatDuration(now.getTime() - Date.parse(routine.last_fired))} ago`
      : "never fired";
  } else if (!routine.last_fired) {
    stalenessText = "never fired";
  } else if (!cron) {
    // Event-triggered or an unparseable cron: no interval to compare
    // against, so emit no overdue verdict — just the raw age (fable spec).
    stalenessText = `last fired ${formatDuration(now.getTime() - Date.parse(routine.last_fired))} ago`;
  } else {
    const intervalMs = cronIntervalMs(cron, now);
    const ageMs = now.getTime() - Date.parse(routine.last_fired);
    if (intervalMs === null) {
      stalenessText = `last fired ${formatDuration(ageMs)} ago`;
    } else {
      isStale = ageMs > intervalMs * STALE_MULTIPLIER;
      stalenessText = isStale
        ? `STALE — no data in ${formatDuration(ageMs)} (expected every ${formatDuration(intervalMs)})`
        : `last fired ${formatDuration(ageMs)} ago, expected every ${formatDuration(intervalMs)}`;
    }
  }

  let state: LoopState;
  if (pendingApprovals.length > 0) {
    state = "needs_you";
  } else if (isStale) {
    state = "stale";
  } else if (routine.status === "auto_paused") {
    state = "auto_paused";
  } else if (routine.status === "disabled") {
    state = "disabled";
  } else if (routine.status === "active" && !routine.last_fired) {
    state = "never_fired";
  } else {
    state = "ok";
  }

  return {
    routine,
    instanceId,
    state,
    tone: STATE_TONE[state],
    stateLabel: STATE_LABEL[state],
    lastOutcome,
    lastOutcomeLabel: lastOutcome ? TICK_LABEL[lastOutcome] : "No runs yet",
    ticks,
    acceptanceRate: routine.acceptance_rate,
    acceptanceText: acceptanceText(routine.acceptance_rate),
    totalCostUsd: routine.total_cost_usd,
    totalCostText: totalCostText(routine.total_cost_usd),
    stalenessText,
    isStale,
    pendingApprovals,
  };
}

/** Derive the full Loops card model from the three raw API responses. Pure —
 * callers own fetching, polling, and error handling. */
export function deriveLoopHealth(
  routines: Routine[],
  runs: RecentRunItem[],
  approvals: Approval[],
  now: Date = new Date(),
): LoopHealthResult {
  const loopApprovals = approvals.filter((approval) => approval.risk === LOOP_APPROVAL_RISK);
  const rows = routines
    .filter(isLoopRoutine)
    .map((routine) => buildRow(routine, runs, loopApprovals, now))
    .sort((a, b) => STATE_PRIORITY[a.state] - STATE_PRIORITY[b.state]);

  const matchedIds = new Set(rows.flatMap((row) => row.pendingApprovals.map((a) => a.id)));
  const unmatchedLoopApprovals = loopApprovals.filter((approval) => !matchedIds.has(approval.id));

  return { loops: rows, unmatchedLoopApprovals };
}
