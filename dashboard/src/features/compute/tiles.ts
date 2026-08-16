/**
 * Pure data shaping for the /compute (Estate Compute) surface — no React, no
 * fetch, easy to unit test (see tiles.test.ts alongside). Three jobs:
 *   1. Turn a ComputeEstate into the 4 summary tiles (free slots, queue
 *      depth, estate runners, local box load).
 *   2. Turn pool machines + the local envelope into one normalized card list
 *      for the machine grid.
 *   3. Classify a CI runner into busy/idle/offline + the badge tone.
 *
 * UNKNOWN is never coerced to 0 or success-styled (dashboard-conventions.md
 * Rule 2) — every shape function below renders a null/unmeasured number as
 * "—", never a fabricated zero or a green tile.
 */

import { formatPercent, formatRelativeTime } from "@/lib/format";
import type { BadgeTone } from "@/design";
import type {
  CiRunner,
  ComputeEstate,
  ComputeLocal,
  ComputePool,
  ComputeRunners,
  ComputeMachine,
} from "./types";

export type Tone = "ok" | "warn" | "danger" | "default";

/**
 * Doctrine load-ratio tone mapping (frozen thresholds, wire contract §2):
 *   ratio < 0.6             → ok      (green)
 *   0.6 <= ratio <= 0.8      → warn    (amber)
 *   ratio > 0.8              → danger  (red)
 *   null (unmeasured)        → default (neutral "—", never success-styled)
 *
 * The boundaries are inclusive toward warn on both sides: exactly 0.6 is
 * NOT "< 0.6" so it is warn, not ok; exactly 0.8 is NOT "> 0.8" so it is
 * warn, not danger.
 */
export function loadRatioTone(ratio: number | null | undefined): Tone {
  if (ratio == null) return "default";
  if (ratio < 0.6) return "ok";
  if (ratio <= 0.8) return "warn";
  return "danger";
}

/** "—" for a null/undefined ratio, else a formatted percent via the central
 * formatter (Rule 3 — machine metrics render through lib/format.ts, never
 * formatted inline). A ratio can exceed 1.0 (load average above core count)
 * — formatPercent renders that honestly too, e.g. "125.0%". */
export function formatLoadRatio(ratio: number | null | undefined): string {
  return formatPercent(ratio ?? null);
}

/** A machine reads as stale once its last-seen age exceeds this many
 * seconds — the boundary itself (exactly 120) is NOT stale. */
export const STALE_AGE_S = 120;

export function isStale(lastSeenAgeS: number | null | undefined): boolean {
  return lastSeenAgeS != null && lastSeenAgeS > STALE_AGE_S;
}

export function isDraining(machine: { drain: number | null | undefined }): boolean {
  return (machine.drain ?? 0) > 0;
}

/** "—" for a null/undefined count, else the plain integer string — machine
 * counts (in-flight/max slots/done_1h/done_6h/queue legs) are never coerced
 * to 0 when the backend never measured them. */
export function fmtCount(v: number | null | undefined): string {
  return v == null ? "—" : String(v);
}

// ── Machine cards ────────────────────────────────────────────────────────

export type MachineCardView = {
  key: string;
  name: string;
  os: string | null;
  labels: string[];
  cores: number | null;
  perfCores: number | null;
  loadRatio: number | null;
  loadTone: Tone;
  memFreeGb: number | null;
  inFlight: number | null;
  maxConcurrent: number | null;
  lastSeenAt: string | null;
  lastSeenAgeS: number | null;
  stale: boolean;
  draining: boolean;
  done1h: number | null;
  done6h: number | null;
  isLocal: boolean;
  /**
   * False ONLY for the local card when the `local` envelope itself reports
   * `available: false` (UI-3 review fix). A pool-sourced card is always
   * `true` — a machine listed in the pool's response is, by construction,
   * present, even if every one of its individual metrics is unmeasured
   * (null). The grid must render an `available: false` card as a neutral
   * refusal (name + `reason`, no metrics), never a live-looking unmeasured
   * machine — an all-null card and a genuinely-refused card are different
   * facts and must not render identically.
   */
  available: boolean;
  reason: string | null;
};

/** A stable, never-null/never-empty key for a pool machine —
 * `machine_id`/`hostname` are both nullable on the wire (CROSS-1: the pool
 * server's raw JSON is relayed via an unvalidated `dict.get`, so an
 * upstream omission comes back `None`). `||` (not `??`) on purpose — an
 * empty string is treated the same as null/undefined here, matching the
 * name-resolution fallback below. Falls back to a positional synthetic id
 * rather than ever handing React a `null` key. */
function poolMachineKey(m: Pick<ComputeMachine, "machine_id" | "hostname">, index: number): string {
  return m.machine_id || m.hostname || `unknown-machine-${index}`;
}

/** A pool machine → the card view. `index` is this machine's position in
 * `pool.machines` — only used as the last-resort fallback key/name when
 * BOTH identity fields are null or empty (CROSS-1). */
export function machineCardFromPool(m: ComputeMachine, index: number): MachineCardView {
  return {
    key: poolMachineKey(m, index),
    name: m.hostname || m.machine_id || "Unknown machine",
    os: m.os || null,
    labels: m.labels ?? [],
    cores: m.ncpu,
    perfCores: m.perf_cores,
    loadRatio: m.load_ratio,
    loadTone: loadRatioTone(m.load_ratio),
    memFreeGb: m.mem_free_gb,
    inFlight: m.in_flight,
    maxConcurrent: m.max_concurrent,
    lastSeenAt: m.last_seen_at,
    lastSeenAgeS: m.last_seen_age_s,
    stale: isStale(m.last_seen_age_s),
    draining: isDraining(m),
    done1h: m.done_1h,
    done6h: m.done_6h,
    isLocal: false,
    available: true,
    reason: null,
  };
}

/** A neutral refusal card — no metrics, just the backend's own reason.
 * Shared by any envelope-level "this card cannot be measured" state; today
 * only `machineCardFromLocal` reaches it (UI-3), since pool machines have no
 * per-machine availability concept of their own. */
function refusalCard(key: string, name: string, isLocal: boolean, reason: string | null): MachineCardView {
  return {
    key,
    name,
    os: null,
    labels: [],
    cores: null,
    perfCores: null,
    loadRatio: null,
    loadTone: "default",
    memFreeGb: null,
    inFlight: null,
    maxConcurrent: null,
    lastSeenAt: null,
    lastSeenAgeS: null,
    stale: false,
    draining: false,
    done1h: null,
    done6h: null,
    isLocal,
    available: false,
    reason,
  };
}

/** The local box → the same card shape, honestly missing every field the
 * `local` envelope never carries (os/mem/in-flight/last-seen/throughput —
 * this box IS the dashboard's own host, so it has no separate heartbeat to
 * go stale and no pool bookkeeping of its own). When `local.available` is
 * false, renders as a neutral refusal card instead (UI-3) — never a
 * live-looking card with every metric coincidentally null. */
export function machineCardFromLocal(local: ComputeLocal): MachineCardView {
  if (!local.available) {
    return refusalCard("local", "This machine", true, local.reason);
  }
  return {
    key: "local",
    name: local.hostname ?? "This machine",
    os: null,
    labels: [],
    cores: local.ncpu,
    perfCores: null,
    loadRatio: local.load_ratio,
    loadTone: loadRatioTone(local.load_ratio),
    memFreeGb: null,
    inFlight: null,
    maxConcurrent: null,
    lastSeenAt: null,
    lastSeenAgeS: null,
    stale: false,
    draining: false,
    done1h: null,
    done6h: null,
    isLocal: true,
    available: true,
    reason: null,
  };
}

/** Pool machines (when the pool envelope is available) + the local box,
 * always in that order — the grid's full row set. A down pool never hides
 * the local card: `local` is its own independent envelope. */
export function deriveMachineCards(estate: ComputeEstate): MachineCardView[] {
  const poolCards = estate.pool.available
    ? estate.pool.machines.map((m, index) => machineCardFromPool(m, index))
    : [];
  return [...poolCards, machineCardFromLocal(estate.local)];
}

/** last-seen freshness caption — a real timestamp goes through the central
 * relative-time formatter (Rule 3); the local card has no heartbeat of its
 * own, so it reads "live" rather than a fabricated relative time. */
export function lastSeenCaption(view: Pick<MachineCardView, "lastSeenAt" | "isLocal">, now: Date = new Date()): string {
  if (view.isLocal) return "live";
  return formatRelativeTime(view.lastSeenAt, now);
}

// ── Runners ──────────────────────────────────────────────────────────────

export type RunnerState = "busy" | "idle" | "offline";

/** `status !== "online"` wins over `busy` — an offline runner cannot be
 * meaningfully "busy" regardless of what a stale `busy` flag says. Compared
 * case-insensitively since the backend's exact casing is not pinned. */
export function runnerState(runner: Pick<CiRunner, "status" | "busy">): RunnerState {
  if ((runner.status ?? "").toLowerCase() !== "online") return "offline";
  return runner.busy ? "busy" : "idle";
}

/** busy = "running" Badge tone (blue/in-progress — NOT danger, a busy
 * runner is doing its job); idle = neutral; offline = danger. */
export function runnerBadgeTone(runner: Pick<CiRunner, "status" | "busy">): BadgeTone {
  const state = runnerState(runner);
  if (state === "busy") return "running";
  if (state === "offline") return "danger";
  return "neutral";
}

// ── Summary row ──────────────────────────────────────────────────────────

export type SummaryTile = {
  key: string;
  title: string;
  primary: string;
  caption: string;
  tone: Tone;
  /** Mirrors the underlying envelope's own `available` flag — false powers
   * the same dashed "unavailable" tile styling the testing feature uses,
   * even when the tone itself is neutral. */
  available: boolean;
};

/** Pool free/total slots — "—" whenever either half is unmeasured or the
 * pool itself is down (never a fabricated fraction). */
export function shapeFreeSlots(pool: ComputePool): SummaryTile {
  if (!pool.available) {
    return {
      key: "free-slots",
      title: "Free slots",
      primary: "—",
      caption: pool.reason ?? "pool unavailable",
      tone: "default",
      available: false,
    };
  }
  const { free_slots, total_slots, in_flight } = pool.capacity;
  const primary = free_slots == null || total_slots == null ? "—" : `${free_slots} / ${total_slots}`;
  return {
    key: "free-slots",
    title: "Free slots",
    primary,
    caption: `${fmtCount(in_flight)} in flight`,
    tone: "default",
    available: true,
  };
}

/** Queue depth = queued + claimed + running — "—" when any of the three
 * legs is unmeasured (a partial sum would misrepresent the true depth). */
export function shapeQueueDepth(pool: ComputePool): SummaryTile {
  if (!pool.available) {
    return {
      key: "queue-depth",
      title: "Queue depth",
      primary: "—",
      caption: pool.reason ?? "pool unavailable",
      tone: "default",
      available: false,
    };
  }
  const { queued, claimed, running } = pool.depth;
  const total = queued == null || claimed == null || running == null ? null : queued + claimed + running;
  return {
    key: "queue-depth",
    title: "Queue depth",
    primary: total == null ? "—" : String(total),
    caption: `${fmtCount(queued)} queued · ${fmtCount(claimed)} claimed · ${fmtCount(running)} running`,
    tone: "default",
    available: true,
  };
}

/** Estate CI runners busy/total. */
export function shapeRunnersSummary(runners: ComputeRunners): SummaryTile {
  if (!runners.available) {
    return {
      key: "runners",
      title: "Estate runners",
      primary: "—",
      caption: runners.reason ?? "runners unavailable",
      tone: "default",
      available: false,
    };
  }
  const total = runners.runners.length;
  const busy = runners.runners.filter((r) => runnerState(r) === "busy").length;
  const offline = runners.runners.filter((r) => runnerState(r) === "offline").length;
  const idle = total - busy - offline;
  const caption = total === 0 ? "no runners reported" : offline > 0 ? `${offline} offline · ${idle} idle` : `${idle} idle`;
  return {
    key: "runners",
    title: "Estate runners",
    primary: `${busy} / ${total}`,
    caption,
    tone: "default",
    available: true,
  };
}

/** The local box's own load ratio — doctrine-toned the same way every
 * machine card's load ratio is (the mapping applies consistently, not just
 * on the grid). */
export function shapeLocalLoad(local: ComputeLocal): SummaryTile {
  if (!local.available) {
    return {
      key: "local-load",
      title: "This machine",
      primary: "—",
      caption: local.reason ?? "local load unavailable",
      tone: "default",
      available: false,
    };
  }
  return {
    key: "local-load",
    title: "This machine",
    primary: formatLoadRatio(local.load_ratio),
    caption: local.hostname ?? "—",
    tone: loadRatioTone(local.load_ratio),
    available: true,
  };
}

/** The full 4-tile summary row, in the fixed order the page renders them. */
export function deriveSummaryTiles(estate: ComputeEstate): SummaryTile[] {
  return [shapeFreeSlots(estate.pool), shapeQueueDepth(estate.pool), shapeRunnersSummary(estate.runners), shapeLocalLoad(estate.local)];
}
