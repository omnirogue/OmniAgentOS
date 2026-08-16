/**
 * Fixture data for local development without a live backend. Enabled with
 * ``NEXT_PUBLIC_USE_COMPUTE_FIXTURES=true``, mirroring the testing feature's
 * (and before it, pulse's) convention. Fixtures are NEVER an error fallback
 * — the fixture short-circuit only fires behind this explicit env flag,
 * checked in client.ts before any network call is attempted.
 *
 * Deliberately includes:
 *   - one `available: false` envelope (`runners` — the estate's GitHub
 *     Actions runner API unreachable), exercising the neutral
 *     unavailable-with-reason state end to end in fixture-mode dev.
 *   - one null-heavy machine (`mw-quiet`) — enrolled (it has an identity)
 *     but every measured metric is unmeasured, exercising honest "—"
 *     rendering rather than a coerced zero anywhere on its card.
 *   - a draining, stale machine (`runner-old`) and an overloaded one
 *     (`mw0001`, load_ratio > 0.8) so every tone/badge path renders once.
 *   - a machine with `machine_id`/`hostname` both null (CROSS-1) — the pool
 *     server's raw JSON is relayed via an unvalidated `dict.get`, so an
 *     upstream identity omission is a real case, exercising the shaping
 *     layer's synthetic fallback key rather than ever handing React `null`.
 */

import type { ComputeEstate } from "./types";

const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_COMPUTE_FIXTURES === "true";

function agoIso(seconds: number): string {
  return new Date(Date.now() - seconds * 1000).toISOString();
}

export const FIXTURE_ESTATE: ComputeEstate = {
  generated_at: new Date().toISOString(),
  pool: {
    available: true,
    reason: null,
    machines: [
      {
        machine_id: "mac-studio-01",
        hostname: "owners-mac-studio",
        os: "darwin",
        labels: ["macos", "apple-silicon", "gpu"],
        max_concurrent: 6,
        in_flight: 2,
        drain: 0,
        last_seen_at: agoIso(15),
        last_seen_age_s: 15,
        ncpu: 20,
        perf_cores: 16,
        mem_gb: 128,
        load1: 4.2,
        load5: 3.8,
        mem_free_gb: 61.2,
        load_ratio: 0.42,
        done_1h: 11,
        done_6h: 58,
      },
      {
        machine_id: "mw0001",
        hostname: "mw0001.local",
        os: "linux",
        labels: ["linux", "cpu-only"],
        max_concurrent: 4,
        in_flight: 4,
        drain: 0,
        last_seen_at: agoIso(5),
        last_seen_age_s: 5,
        ncpu: 8,
        perf_cores: null,
        mem_gb: 32,
        load1: 7.9,
        load5: 6.4,
        mem_free_gb: 4.1,
        load_ratio: 0.91,
        done_1h: 3,
        done_6h: 20,
      },
      // Draining AND stale (last_seen_age_s > 120) — both badges at once.
      {
        machine_id: "runner-old",
        hostname: "runner-old.local",
        os: "linux",
        labels: ["linux"],
        max_concurrent: 2,
        in_flight: 0,
        drain: 1,
        last_seen_at: agoIso(9 * 60),
        last_seen_age_s: 540,
        ncpu: 4,
        perf_cores: null,
        mem_gb: 16,
        load1: 0.1,
        load5: 0.1,
        mem_free_gb: 12.0,
        load_ratio: 0.05,
        done_1h: 0,
        done_6h: 2,
      },
      // The null-heavy machine: enrolled (identity present) but nothing
      // measured yet — every card field must render "—", never a fabricated 0.
      {
        machine_id: "mw-quiet",
        hostname: "mw-quiet.local",
        os: "linux",
        labels: ["linux"],
        max_concurrent: null,
        in_flight: null,
        drain: null,
        last_seen_at: null,
        last_seen_age_s: null,
        ncpu: null,
        perf_cores: null,
        mem_gb: null,
        load1: null,
        load5: null,
        mem_free_gb: null,
        load_ratio: null,
        done_1h: null,
        done_6h: null,
      },
      // CROSS-1: the pool server's raw JSON relays machine_id/hostname via
      // an unvalidated dict.get — both can come back null. This machine has
      // neither identity field, exercising the synthetic fallback key path.
      {
        machine_id: null,
        hostname: null,
        os: "linux",
        labels: ["linux"],
        max_concurrent: 2,
        in_flight: 0,
        drain: 0,
        last_seen_at: agoIso(30),
        last_seen_age_s: 30,
        ncpu: 4,
        perf_cores: null,
        mem_gb: 8,
        load1: 0.3,
        load5: 0.2,
        mem_free_gb: 5.0,
        load_ratio: 0.08,
        done_1h: 1,
        done_6h: 4,
      },
    ],
    depth: { queued: 3, claimed: 1, running: 6, done: 512, parked: 2, cancelled: 4 },
    capacity: { total_cores: 32, total_slots: 12, free_slots: 6, in_flight: 6 },
    refusals_24h: { load_shed: 2, quota: 0 },
  },
  // The one available:false envelope — GitHub Actions runner API unreachable.
  // CROSS-1: the `false` variant carries ONLY `reason`, matching the
  // backend's `_absent()` wire shape exactly (no empty `runners: []`).
  runners: {
    available: false,
    reason: "GitHub Actions runner API unreachable — org token expired",
  },
  local: {
    available: true,
    reason: null,
    hostname: "owners-mac-studio",
    load1: 2.1,
    load5: 2.6,
    ncpu: 20,
    load_ratio: 0.31,
  },
};

/** A second fixture with `runners` reporting real rows, for exercising the
 * busy/idle/offline badge paths — not wired into `fetchComputeEstate` (that
 * always serves `FIXTURE_ESTATE` above), exported for direct use in tests. */
export const FIXTURE_ESTATE_WITH_RUNNERS: ComputeEstate = {
  ...FIXTURE_ESTATE,
  runners: {
    available: true,
    reason: null,
    runners: [
      { name: "estate-runner-01", labels: ["self-hosted", "macos"], status: "online", busy: true },
      { name: "estate-runner-02", labels: ["self-hosted", "linux"], status: "online", busy: false },
      { name: "estate-runner-03", labels: ["self-hosted", "linux"], status: "offline", busy: false },
    ],
  },
};

export { USE_FIXTURES };
