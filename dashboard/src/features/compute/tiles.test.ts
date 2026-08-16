import { describe, expect, it } from "vitest";
import type { CiRunner, ComputeEstate, ComputeLocal, ComputeMachine, ComputePool, ComputeRunners } from "./types";
import {
  deriveMachineCards,
  deriveSummaryTiles,
  fmtCount,
  formatLoadRatio,
  isDraining,
  isStale,
  lastSeenCaption,
  loadRatioTone,
  machineCardFromLocal,
  machineCardFromPool,
  runnerBadgeTone,
  runnerState,
  shapeFreeSlots,
  shapeLocalLoad,
  shapeQueueDepth,
  shapeRunnersSummary,
  STALE_AGE_S,
} from "./tiles";

function machine(overrides: Partial<ComputeMachine> = {}): ComputeMachine {
  return {
    machine_id: "m1",
    hostname: "m1.local",
    os: "linux",
    labels: ["linux"],
    max_concurrent: 4,
    in_flight: 1,
    drain: 0,
    last_seen_at: "2026-08-14T00:00:00Z",
    last_seen_age_s: 10,
    ncpu: 8,
    perf_cores: 4,
    mem_gb: 32,
    load1: 2,
    load5: 1.8,
    mem_free_gb: 12,
    load_ratio: 0.4,
    done_1h: 3,
    done_6h: 20,
    ...overrides,
  };
}

/** The `available: true` branch of the discriminated `ComputePool` union
 * (CROSS-1) — overrides only ever touch this branch's own fields; use
 * `poolUnavailable` to build the `false` branch, which structurally has NO
 * `machines`/`depth`/`capacity`/`refusals_24h` to override. */
type PoolAvailable = Extract<ComputePool, { available: true }>;
type RunnersAvailable = Extract<ComputeRunners, { available: true }>;

function pool(overrides: Partial<PoolAvailable> = {}): ComputePool {
  return {
    available: true,
    reason: null,
    machines: [machine()],
    depth: { queued: 2, claimed: 1, running: 3, done: 100, parked: 0, cancelled: 1 },
    capacity: { total_cores: 32, total_slots: 12, free_slots: 6, in_flight: 6 },
    refusals_24h: {},
    ...overrides,
  };
}

/** The `available: false` branch — matches the backend's `_absent(reason)`
 * wire shape exactly: ONLY `available`/`reason`, nothing else (CROSS-1). */
function poolUnavailable(reason = "pool db unreachable"): ComputePool {
  return { available: false, reason };
}

function runners(overrides: Partial<RunnersAvailable> = {}): ComputeRunners {
  return {
    available: true,
    reason: null,
    runners: [
      { name: "r1", labels: ["self-hosted"], status: "online", busy: true },
      { name: "r2", labels: ["self-hosted"], status: "online", busy: false },
      { name: "r3", labels: ["self-hosted"], status: "offline", busy: false },
    ],
    ...overrides,
  };
}

function runnersUnavailable(reason = "runner API down"): ComputeRunners {
  return { available: false, reason };
}

/** The `available: true` branch of the now-discriminated `ComputeLocal`
 * union (CROSS-1 remainder) — use `localUnavailable` for the `false`
 * branch, which structurally has no metric fields to override. */
type LocalAvailable = Extract<ComputeLocal, { available: true }>;

function local(overrides: Partial<LocalAvailable> = {}): ComputeLocal {
  return {
    available: true,
    reason: null,
    hostname: "local-box",
    load1: 1.2,
    load5: 1.0,
    ncpu: 10,
    load_ratio: 0.42,
    ...overrides,
  };
}

/** The `available: false` branch — matches the backend's `_safe_read()`
 * absent shape exactly: ONLY `available`/`reason` (CROSS-1 remainder). */
function localUnavailable(reason = "temporarily unavailable"): ComputeLocal {
  return { available: false, reason };
}

function estate(overrides: Partial<ComputeEstate> = {}): ComputeEstate {
  return {
    generated_at: "2026-08-14T00:00:00Z",
    pool: pool(),
    runners: runners(),
    local: local(),
    ...overrides,
  };
}

describe("loadRatioTone", () => {
  it("is neutral/default for a null or undefined ratio — unmeasured, never coerced", () => {
    expect(loadRatioTone(null)).toBe("default");
    expect(loadRatioTone(undefined)).toBe("default");
  });

  it("is ok strictly below 0.6", () => {
    expect(loadRatioTone(0)).toBe("ok");
    expect(loadRatioTone(0.59)).toBe("ok");
    expect(loadRatioTone(0.5999)).toBe("ok");
  });

  it("is warn at the exact 0.6 boundary (0.6 is NOT < 0.6)", () => {
    expect(loadRatioTone(0.6)).toBe("warn");
  });

  it("is warn through the inclusive 0.6–0.8 band", () => {
    expect(loadRatioTone(0.7)).toBe("warn");
  });

  it("is warn at the exact 0.8 boundary (0.8 is NOT > 0.8)", () => {
    expect(loadRatioTone(0.8)).toBe("warn");
  });

  it("is danger strictly above 0.8, including ratios past 1.0 (overloaded)", () => {
    expect(loadRatioTone(0.8001)).toBe("danger");
    expect(loadRatioTone(1.25)).toBe("danger");
  });
});

describe("formatLoadRatio", () => {
  it("renders '—' for null/undefined", () => {
    expect(formatLoadRatio(null)).toBe("—");
    expect(formatLoadRatio(undefined)).toBe("—");
  });

  it("renders a formatted percent for a measured ratio", () => {
    expect(formatLoadRatio(0.42)).toBe("42.0%");
    expect(formatLoadRatio(1.25)).toBe("125.0%");
  });
});

describe("isStale", () => {
  it("is false at and below the 120s boundary", () => {
    expect(isStale(0)).toBe(false);
    expect(isStale(119)).toBe(false);
    expect(isStale(STALE_AGE_S)).toBe(false);
  });

  it("is true strictly past the 120s boundary", () => {
    expect(isStale(121)).toBe(true);
    expect(isStale(540)).toBe(true);
  });

  it("is false for null/undefined — unmeasured is not the same as stale", () => {
    expect(isStale(null)).toBe(false);
    expect(isStale(undefined)).toBe(false);
  });
});

describe("isDraining", () => {
  it("is true only when drain > 0", () => {
    expect(isDraining({ drain: 1 })).toBe(true);
    expect(isDraining({ drain: 0 })).toBe(false);
    expect(isDraining({ drain: null })).toBe(false);
    expect(isDraining({ drain: undefined })).toBe(false);
  });
});

describe("fmtCount", () => {
  it("renders '—' for null/undefined, else the plain integer string", () => {
    expect(fmtCount(null)).toBe("—");
    expect(fmtCount(undefined)).toBe("—");
    expect(fmtCount(0)).toBe("0");
    expect(fmtCount(42)).toBe("42");
  });
});

describe("machineCardFromPool / machineCardFromLocal", () => {
  it("maps a pool machine's fields straight through, deriving loadTone and stale/draining", () => {
    const m = machine({ load_ratio: 0.9, last_seen_age_s: 200, drain: 1 });
    const card = machineCardFromPool(m, 0);
    expect(card.key).toBe("m1");
    expect(card.name).toBe("m1.local");
    expect(card.loadTone).toBe("danger");
    expect(card.stale).toBe(true);
    expect(card.draining).toBe(true);
    expect(card.isLocal).toBe(false);
    expect(card.available).toBe(true);
    expect(card.reason).toBeNull();
  });

  it("falls back to machine_id for the name when hostname is empty", () => {
    const card = machineCardFromPool(machine({ hostname: "" }), 0);
    expect(card.name).toBe("m1");
  });

  it("uses hostname as the key when machine_id is null (CROSS-1)", () => {
    const card = machineCardFromPool(machine({ machine_id: null, hostname: "m1.local" }), 0);
    expect(card.key).toBe("m1.local");
    expect(card.name).toBe("m1.local");
  });

  it("uses machine_id as the name when hostname is null (CROSS-1)", () => {
    const card = machineCardFromPool(machine({ machine_id: "m1", hostname: null }), 0);
    expect(card.key).toBe("m1");
    expect(card.name).toBe("m1");
  });

  it("falls back to a positional synthetic key/name when BOTH identity fields are null — never null (CROSS-1)", () => {
    const card = machineCardFromPool(machine({ machine_id: null, hostname: null }), 2);
    expect(card.key).toBe("unknown-machine-2");
    expect(card.key).not.toBeNull();
    expect(card.name).toBe("Unknown machine");
  });

  it("gives two identity-less machines distinct keys via their position (CROSS-1)", () => {
    const a = machineCardFromPool(machine({ machine_id: null, hostname: null }), 0);
    const b = machineCardFromPool(machine({ machine_id: null, hostname: null }), 1);
    expect(a.key).not.toBe(b.key);
  });

  it("tolerates a null os/labels without throwing (CROSS-1)", () => {
    const card = machineCardFromPool(machine({ os: null, labels: null }), 0);
    expect(card.os).toBeNull();
    expect(card.labels).toEqual([]);
  });

  it("shapes the local box honestly — no os/mem/in-flight/last-seen/throughput fields", () => {
    const card = machineCardFromLocal(local({ load_ratio: 0.65, ncpu: 12 }));
    expect(card.key).toBe("local");
    expect(card.isLocal).toBe(true);
    expect(card.os).toBeNull();
    expect(card.memFreeGb).toBeNull();
    expect(card.inFlight).toBeNull();
    expect(card.stale).toBe(false);
    expect(card.draining).toBe(false);
    expect(card.loadTone).toBe("warn");
    // Sourced from the wire contract's `ncpu` field (not `cores`).
    expect(card.cores).toBe(12);
    expect(card.available).toBe(true);
    expect(card.reason).toBeNull();
  });

  it("names the local card 'This machine' when hostname is null", () => {
    const card = machineCardFromLocal(local({ hostname: null }));
    expect(card.name).toBe("This machine");
  });

  it("renders a neutral refusal card — not a live-looking unmeasured machine — when local.available is false (UI-3)", () => {
    const card = machineCardFromLocal(localUnavailable("host metrics collector not running"));
    expect(card.available).toBe(false);
    expect(card.reason).toBe("host metrics collector not running");
    // The `available: false` variant of ComputeLocal has no metric fields
    // at all (CROSS-1 remainder) — the type system itself now guarantees
    // no number can leak through; the card still carries no numbers either.
    expect(card.loadRatio).toBeNull();
    expect(card.loadTone).toBe("default");
    expect(card.cores).toBeNull();
    expect(card.stale).toBe(false);
    expect(card.draining).toBe(false);
    expect(card.isLocal).toBe(true);
    expect(card.key).toBe("local");
    expect(card.name).toBe("This machine");
  });
});

describe("deriveMachineCards", () => {
  it("returns pool machines followed by the local card when the pool is available", () => {
    const est = estate({ pool: pool({ machines: [machine({ machine_id: "a" }), machine({ machine_id: "b" })] }) });
    const cards = deriveMachineCards(est);
    expect(cards.map((c) => c.key)).toEqual(["a", "b", "local"]);
  });

  it("still renders the local card when the pool is unavailable — local is its own envelope", () => {
    const est = estate({ pool: poolUnavailable("pool offline") });
    const cards = deriveMachineCards(est);
    expect(cards.map((c) => c.key)).toEqual(["local"]);
  });

  it("gives identity-less pool machines distinct fallback keys by their real array position (CROSS-1)", () => {
    const est = estate({
      pool: pool({
        machines: [machine({ machine_id: null, hostname: null }), machine({ machine_id: "b" }), machine({ machine_id: null, hostname: null })],
      }),
    });
    const cards = deriveMachineCards(est);
    expect(cards.map((c) => c.key)).toEqual(["unknown-machine-0", "b", "unknown-machine-2", "local"]);
  });
});

describe("lastSeenCaption", () => {
  it("renders 'live' for the local card regardless of lastSeenAt", () => {
    expect(lastSeenCaption({ isLocal: true, lastSeenAt: null })).toBe("live");
    expect(lastSeenCaption({ isLocal: true, lastSeenAt: "2026-08-14T00:00:00Z" })).toBe("live");
  });

  it("renders '—' for a pool machine with no last-seen timestamp", () => {
    expect(lastSeenCaption({ isLocal: false, lastSeenAt: null })).toBe("—");
  });

  it("renders a relative time for a real timestamp", () => {
    const now = new Date("2026-08-14T00:05:00Z");
    expect(lastSeenCaption({ isLocal: false, lastSeenAt: "2026-08-14T00:00:00Z" }, now)).toBe("5m ago");
  });
});

describe("runnerState", () => {
  it("is busy when status is online and busy is true", () => {
    expect(runnerState({ status: "online", busy: true })).toBe("busy");
  });

  it("is idle when status is online and busy is false", () => {
    expect(runnerState({ status: "online", busy: false })).toBe("idle");
  });

  it("is offline whenever status is not online, regardless of the busy flag", () => {
    expect(runnerState({ status: "offline", busy: false })).toBe("offline");
    expect(runnerState({ status: "offline", busy: true })).toBe("offline");
  });

  it("matches status case-insensitively", () => {
    expect(runnerState({ status: "Online", busy: true })).toBe("busy");
  });
});

describe("runnerBadgeTone", () => {
  it("maps busy to the 'running' (in-progress) Badge tone — never danger", () => {
    expect(runnerBadgeTone({ status: "online", busy: true })).toBe("running");
  });

  it("maps idle to neutral", () => {
    expect(runnerBadgeTone({ status: "online", busy: false })).toBe("neutral");
  });

  it("maps offline to danger", () => {
    expect(runnerBadgeTone({ status: "offline", busy: false })).toBe("danger");
  });
});

describe("shapeFreeSlots", () => {
  it("renders '—' with the pool's reason when the pool is unavailable", () => {
    const tile = shapeFreeSlots(poolUnavailable("pool db unreachable"));
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("pool db unreachable");
    expect(tile.available).toBe(false);
    expect(tile.tone).toBe("default");
  });

  it("renders free/total with an in-flight caption when available", () => {
    const tile = shapeFreeSlots(pool());
    expect(tile.primary).toBe("6 / 12");
    expect(tile.caption).toBe("6 in flight");
    expect(tile.available).toBe(true);
  });

  it("renders '—' honestly when free_slots or total_slots is unmeasured (never a fabricated fraction)", () => {
    const tile = shapeFreeSlots(pool({ capacity: { total_cores: 32, total_slots: null, free_slots: 6, in_flight: 6 } }));
    expect(tile.primary).toBe("—");
  });
});

describe("shapeQueueDepth", () => {
  it("renders '—' with the pool's reason when the pool is unavailable", () => {
    const tile = shapeQueueDepth(poolUnavailable("pool db unreachable"));
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("pool db unreachable");
  });

  it("sums queued+claimed+running when all three are measured", () => {
    const tile = shapeQueueDepth(pool());
    expect(tile.primary).toBe("6");
    expect(tile.caption).toBe("2 queued · 1 claimed · 3 running");
  });

  it("renders '—' for the total when any leg is null, but still reports the honest per-leg caption", () => {
    const tile = shapeQueueDepth(
      pool({ depth: { queued: 2, claimed: null, running: 3, done: 0, parked: 0, cancelled: 0 } }),
    );
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("2 queued · — claimed · 3 running");
  });
});

describe("shapeRunnersSummary", () => {
  it("renders '—' with the reason when runners are unavailable", () => {
    const tile = shapeRunnersSummary(runnersUnavailable("runner API down"));
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("runner API down");
    expect(tile.available).toBe(false);
  });

  it("counts busy/total and calls out offline runners in the caption", () => {
    const tile = shapeRunnersSummary(runners());
    expect(tile.primary).toBe("1 / 3");
    expect(tile.caption).toBe("1 offline · 1 idle");
  });

  it("reports 'no runners reported' for a genuinely empty, available runner list", () => {
    const tile = shapeRunnersSummary(runners({ runners: [] }));
    expect(tile.primary).toBe("0 / 0");
    expect(tile.caption).toBe("no runners reported");
  });

  it("omits the offline clause and just states idle count when nothing is offline", () => {
    const tile = shapeRunnersSummary(
      runners({ runners: [{ name: "r1", labels: [], status: "online", busy: true } satisfies CiRunner] }),
    );
    expect(tile.caption).toBe("0 idle");
  });
});

describe("shapeLocalLoad", () => {
  it("renders '—' when the local envelope is unavailable (CROSS-1 remainder: local can be down too)", () => {
    const tile = shapeLocalLoad(localUnavailable());
    expect(tile.primary).toBe("—");
    expect(tile.available).toBe(false);
    expect(tile.tone).toBe("default");
  });

  it("surfaces the backend's own reason when the local envelope is unavailable — same honesty as pool/runners", () => {
    const tile = shapeLocalLoad(localUnavailable("host metrics collector not running"));
    expect(tile.caption).toBe("host metrics collector not running");
  });

  it("applies the same doctrine load tone as the machine cards", () => {
    expect(shapeLocalLoad(local({ load_ratio: 0.9 })).tone).toBe("danger");
    expect(shapeLocalLoad(local({ load_ratio: 0.3 })).tone).toBe("ok");
    expect(shapeLocalLoad(local({ load_ratio: 0.6 })).tone).toBe("warn");
  });

  it("uses the hostname as the caption, honestly '—' when absent", () => {
    expect(shapeLocalLoad(local({ hostname: "box-1" })).caption).toBe("box-1");
    expect(shapeLocalLoad(local({ hostname: null })).caption).toBe("—");
  });
});

describe("deriveSummaryTiles", () => {
  it("returns the 4 tiles in the fixed order: free slots, queue depth, runners, local load", () => {
    const tiles = deriveSummaryTiles(estate());
    expect(tiles.map((t) => t.key)).toEqual(["free-slots", "queue-depth", "runners", "local-load"]);
  });
});
