/** TypeScript mirror of GET /api/system-jobs (omniagentos/api/routes/system_jobs.py,
 * backed by omniagentos/scheduler/system_jobs.py) plus the pure display helpers the
 * System loops panel needs. Health semantics are HANDOFF/LOOPS-VISIBILITY.md §L2:
 * a job with no observability is "unknown" and must never render as healthy. */
import type { BadgeTone } from "../../design";
import type { StatusDotState } from "../../design";

export type SystemJobHealth = "healthy" | "stale" | "failing" | "unknown" | "not_loaded";

export type KnownSystemJobExecutor =
  | "launchd"
  | "remote_cron"
  | "remote_docker"
  | "csi_pipeline"
  | "local_cron";

/**
 * The backend may add new executor values over time (e.g. `local_cron`);
 * widened to `string` so an unrecognized executor renders generically via
 * `executorLabel`'s fallback rather than failing typecheck or breaking at
 * runtime. `KnownSystemJobExecutor` still documents the values we style
 * specially.
 */
export type SystemJobExecutor = KnownSystemJobExecutor | (string & {});

export type SystemJobSchedule = {
  kind: "interval" | "calendar" | "cron" | "window" | "unknown";
  seconds: number | null;
  description: string;
};

export type SystemJob = {
  key: string;
  name: string;
  executor: SystemJobExecutor;
  category: string;
  label: string | null;
  purpose: string;
  source: string;
  module: string | null;
  schedule: SystemJobSchedule;
  env_overrides: string[];
  /**
   * Three states, and `null` is NOT a weaker `false`:
   *   true  = measured, present in `launchctl list`
   *   false = measured, absent from it — configured but currently off
   *   null  = NOT MEASURED: either not a launchd job (remote/CSI), or the
   *           launchctl probe could not run on this host.
   *
   * Never read `null` as `false`. `health_reason` distinguishes the two null
   * causes, and `SystemJobsSnapshot.launchctl.available` says whether the probe
   * ran at all. Rendering null as "off" is the bug this field was fixed for:
   * an unreadable probe used to report every job as configured-but-off.
   */
  loaded: boolean | null;
  plist_present: boolean;
  last_exit_status: number | null;
  last_run_at: string | null;
  next_fire_at: string | null;
  /** One-line last outcome (parsed log tail / docker status text). `null`
   * when unavailable — never coerced to an empty string, so callers can
   * distinguish "no data" from "genuinely blank result". Optional because
   * older snapshots (and any cached fixture) predate this field. */
  last_result?: string | null;
  health: SystemJobHealth;
  health_reason: string;
  /** Has an objective gate → should become a managed DB routine (see report). */
  managed_candidate: boolean;
  candidate_reason: string;
};

export type SystemJobsSnapshot = {
  generated_at: string | null;
  /**
   * Did the machine-state probe run? When `available` is false every job's
   * `loaded` is null and `counts.loaded` is 0 because nothing was measured —
   * not because nothing is loaded. `reason` names why it could not run.
   */
  launchctl?: { available: boolean; reason: string };
  /**
   * Did a read-only remote probe (SSH/docker) run for remote jobs this
   * snapshot? When `available` is false, remote jobs' health may be stale or
   * `unknown` because nothing was measured this cycle — `reason` names why.
   * Optional: older snapshots predate remote probing entirely.
   */
  remote_probe?: { available: boolean; reason: string; probed_at: string | null };
  counts: {
    total: number;
    loaded: number;
    /** Jobs whose loaded state is unknown; non-zero means `loaded` is a floor, not a total. */
    loaded_unknown?: number;
    failing: number;
    stale: number;
    /** Additive optional counts the backend may also carry; the frontend
     * derives its own summary strip client-side from `jobs` and treats these
     * as informational only, never authoritative over the derived counts. */
    healthy?: number;
    unknown?: number;
    not_loaded?: number;
  };
  jobs: SystemJob[];
};

const HEALTH_TONES: Record<SystemJobHealth, BadgeTone> = {
  healthy: "ok",
  stale: "warn",
  failing: "danger",
  unknown: "neutral",
  not_loaded: "neutral",
};

const HEALTH_LABELS: Record<SystemJobHealth, string> = {
  healthy: "Healthy",
  stale: "Stale",
  failing: "Failing",
  unknown: "Unknown",
  not_loaded: "Not loaded",
};

const HEALTH_DOT_STATES: Record<SystemJobHealth, StatusDotState> = {
  healthy: "ok",
  stale: "warn",
  failing: "danger",
  unknown: "queued",
  not_loaded: "paused",
};

export function systemJobHealthTone(health: SystemJobHealth): BadgeTone {
  return HEALTH_TONES[health] ?? "neutral";
}

export function systemJobHealthLabel(health: SystemJobHealth): string {
  return HEALTH_LABELS[health] ?? health;
}

export function systemJobDotState(health: SystemJobHealth): StatusDotState {
  return HEALTH_DOT_STATES[health] ?? "queued";
}

/**
 * A `Map`, not an object literal: an object-literal lookup keyed by an
 * arbitrary server-supplied string (`EXECUTOR_LABELS[executor]`) resolves
 * inherited `Object.prototype` members for values like `"__proto__"`,
 * `"constructor"`, or `"toString"` — those aren't `undefined`, so the `??`
 * fallback never fires and React is handed a function/object instead of a
 * string, which throws at render. A `Map` has no prototype chain to leak
 * through, so `.get()` on an unknown/hostile key is always a clean `undefined`.
 */
const EXECUTOR_LABELS = new Map<KnownSystemJobExecutor, string>([
  ["launchd", "launchd"],
  ["remote_cron", "remote cron"],
  ["remote_docker", "remote docker"],
  ["csi_pipeline", "CSI pipeline"],
  ["local_cron", "local cron"],
]);

/** Unknown executor values (future backend additions, or any string that is
 * not one of `KnownSystemJobExecutor`) render generically
 * ("some_new_executor" -> "some new executor") instead of breaking. */
function humanizeUnknownExecutor(executor: string): string {
  // Collapse runs of underscores (leading/trailing/doubled) rather than a
  // naive replaceAll, which would turn e.g. "__proto__" into "  proto  ".
  return executor.replace(/_+/g, " ").trim() || executor;
}

export function executorLabel(executor: SystemJobExecutor): string {
  return EXECUTOR_LABELS.get(executor as KnownSystemJobExecutor) ?? humanizeUnknownExecutor(executor);
}

/** "Loaded" / "Not loaded" / null (not applicable — remote and CSI jobs). */
export function loadedLabel(loaded: boolean | null): string | null {
  if (loaded === null) return null;
  return loaded ? "Loaded" : "Not loaded";
}

export { formatRelativeTime, formatCountdownTo, healthSeverity } from "../../lib/format";
