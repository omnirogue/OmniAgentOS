import { describe, expect, it } from "vitest";
import {
  bucketAcceptanceRates,
  rollingAcceptanceRates,
  sparklineToneForPoints,
} from "./sparkline";
import type { RoutineRun } from "./types";

function run(partial: Partial<RoutineRun> & { iteration: number }): RoutineRun {
  return {
    id: partial.iteration,
    routine_id: "rtn_x",
    run_id: null,
    gate_passed: null,
    accepted: null,
    cost_usd: 0,
    stop_reason: "",
    notes: "",
    started_at: null,
    finished_at: null,
    created_at: "",
    ...partial,
  };
}

describe("rollingAcceptanceRates", () => {
  it("returns one rolling-rate point per settled run, oldest-first", () => {
    const runs = [
      run({ iteration: 1, accepted: true }),
      run({ iteration: 2, accepted: false }),
      run({ iteration: 3, accepted: true }),
    ];
    const points = rollingAcceptanceRates(runs);
    expect(points).toHaveLength(3);
    // API returns newest-first by default; our fn reverses to oldest-first
    // then emits: [1/1, 1/2, 2/3]
    expect(points[0]).toBeCloseTo(1);
    expect(points[1]).toBeCloseTo(0.5);
    expect(points[2]).toBeCloseTo(2 / 3);
  });

  it("skips runs with null accepted (unsettled)", () => {
    // API order is newest-first: iter3 is the most recent run.
    const runs = [
      run({ iteration: 3, accepted: false }),
      run({ iteration: 2, accepted: true }),
      run({ iteration: 1, accepted: null }),
    ];
    const points = rollingAcceptanceRates(runs);
    expect(points).toEqual([1, 0.5]);
  });

  it("returns an empty list when every run is unsettled", () => {
    expect(
      rollingAcceptanceRates([
        run({ iteration: 1, accepted: null }),
        run({ iteration: 2, accepted: null }),
      ]),
    ).toEqual([]);
  });

  it("returns empty from an empty input", () => {
    expect(rollingAcceptanceRates([])).toEqual([]);
  });
});

describe("bucketAcceptanceRates", () => {
  it("groups by routine_id and computes per-routine rolling rates", () => {
    const runs = [
      { routine_id: "A", accepted: true },
      { routine_id: "B", accepted: false },
      { routine_id: "A", accepted: false },
      { routine_id: "A", accepted: true },
    ];
    const out = bucketAcceptanceRates(runs);
    expect(out.has("A")).toBe(true);
    expect(out.has("B")).toBe(true);
    // A: [true, false, true] → [1, 0.5, 2/3]
    expect(out.get("A")).toEqual([1, 0.5, 2 / 3]);
    // B: [false] → [0]
    expect(out.get("B")).toEqual([0]);
  });

  it("drops routines where every run is unsettled", () => {
    const runs = [
      { routine_id: "A", accepted: null },
      { routine_id: "A", accepted: null },
    ];
    expect(bucketAcceptanceRates(runs).size).toBe(0);
  });

  it("returns empty from empty input", () => {
    expect(bucketAcceptanceRates([]).size).toBe(0);
  });
});

describe("sparklineToneForPoints", () => {
  it("returns accent (neutral) when there are no points", () => {
    expect(sparklineToneForPoints([])).toBe("accent");
  });

  it("ok for rates >= 0.5, danger for rates < 0.5", () => {
    expect(sparklineToneForPoints([0.5, 0.6, 0.55])).toBe("ok");
    expect(sparklineToneForPoints([0.7, 0.4, 0.3])).toBe("danger");
    expect(sparklineToneForPoints([0.5])).toBe("ok");
    expect(sparklineToneForPoints([0.49])).toBe("danger");
  });

  it("uses the LAST point, not the first", () => {
    expect(sparklineToneForPoints([1, 1, 0.2])).toBe("danger");
    expect(sparklineToneForPoints([0.1, 0.2, 0.9])).toBe("ok");
  });
});
