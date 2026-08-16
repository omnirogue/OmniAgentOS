/**
 * Wire shape for `GET /api/local/status` — the Status homepage's one data
 * source. Every top-level section is an explicit `Result<T>` rather than a
 * bare value: a fetch failure must never render as a fake-healthy default
 * (favourable absence is the estate's #1 defect class), so every section
 * that can fail says so with `{ ok: false, error }` instead of silently
 * defaulting a count to 0 or a state to "up".
 */

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

/** One of implementer / reviewer / planning. */
export interface LoopRoleStatus {
  /** tmux session `loop-<role>` liveness — the actual daemon, not the log. */
  alive: boolean;
  /** ISO timestamp of the last `iteration end` line in the role's log tail,
   * or null when no iteration has completed inside the tail window yet. */
  lastIterEnd: string | null;
  /** Exit code of that last iteration, or null alongside a null
   * `lastIterEnd`. Never inferred — only ever the literal `rc=<n>`. */
  lastRc: number | null;
}

export interface LoopsStatus {
  implementer: Result<LoopRoleStatus>;
  reviewer: Result<LoopRoleStatus>;
  planning: Result<LoopRoleStatus>;
}

export interface GateStatus {
  /** Last up-to-5 lines from the gate-loop log tail matching "still
   * gating" / "no trains assembled" / "gate slots full", oldest first. */
  trainLines: string[];
  /** Last `"at": "<ISO>"` timestamp seen in the tail — the gate loop's own
   * last tick, independent of whether that tick found anything to say. */
  lastAt: string | null;
}

export interface QueueStatus {
  /** Work-in-progress count. `null` means the source withheld it (ledger
   * damage set `wip_degraded`) — this must NEVER be defaulted to 0, which
   * would misread as "queue empty" instead of "count withheld". */
  wip: number | null;
  wip_cap: number | null;
  wip_degraded: boolean;
  wip_degraded_detail: string | null;
  rebuilt_at: string | null;
}

export interface LandingsStatus {
  /** Commits on origin/main since UTC 00:00 today. */
  countToday: number;
  /** `<hash> <HH:MMZ> <subject>` of the single most recent origin/main
   * commit, or null when the repo has no commits at all (never happens in
   * practice, but honest over a fake placeholder). */
  lastLanding: string | null;
}

/** Raw parsed hang-recycler.log tick, or the raw line when it didn't parse
 * as JSON (e.g. a tail cut mid-write). */
export type RecyclerEntry = Record<string, unknown>;
export type RecyclerStatus = RecyclerEntry | { raw: string };

export interface StatusResponse {
  generatedAt: string;
  loops: LoopsStatus;
  gate: Result<GateStatus>;
  queue: Result<QueueStatus>;
  landings: Result<LandingsStatus>;
  recycler: Result<RecyclerStatus>;
  /** Up to 5 most recent "- " bullets from ALERTS.md, each capped at 300
   * chars. */
  alerts: Result<string[]>;
}
