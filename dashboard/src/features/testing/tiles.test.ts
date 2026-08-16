import { describe, expect, it } from "vitest";
import type { TestObsOverview, TestObsSeriesLine, TestObsSeriesResponse, WeakspotItem, WeakspotSources } from "./types";
import {
  allSourcesAvailable,
  deltaFromPoints,
  findSeriesLine,
  groupWeakspotsByCategory,
  latestPoint,
  northstarTone,
  seriesIsEmpty,
  seriesValues,
  shapeStat,
  toLineSeries,
  unavailableSources,
  weakspotTone,
} from "./tiles";

function overview(overrides: Partial<TestObsOverview> = {}): TestObsOverview {
  return {
    generated_at: "2026-08-14T00:00:00Z",
    northstar: {
      available: true,
      run_id: "nscert-t1-20260813T101005Z",
      verdict_ts: "2026-08-13T10:10:05Z",
      distance: 23.7,
      gate_pass_rate: 88.2,
      checks_pass: 52,
      checks_fail: 1,
      checks_not_evaluable: 166,
      checks_void: 0,
      delta_distance: 1.4,
    },
    memory: { available: false, reason: "no durable memcert runs recorded yet" },
    diagnostics: {
      available: true,
      features_total: 14,
      features_failing: 2,
      features_stale: 3,
      last_run_ts: "2026-08-14T01:00:00Z",
      aborted_recent: 2,
    },
    suite: {
      available: true,
      runs_7d: 41,
      pass_rate_7d: 93.1,
      latest_lane: { name: "fast-lane", tests: 11510, failures: 3, errors: 0, skipped: 12, ts: "2026-08-14T01:20:00Z" },
    },
    ...overrides,
  };
}

function line(metric: string, points: Array<{ date: string; value: number }> = []): TestObsSeriesLine {
  return { metric, label: metric, unit: "%", points };
}

describe("findSeriesLine", () => {
  it("returns the matching line by metric name", () => {
    const lines = [line("nsc.distance", [{ date: "2026-08-01", value: 1 }]), line("nsc.void_ratio")];
    expect(findSeriesLine(lines, "nsc.void_ratio")?.metric).toBe("nsc.void_ratio");
  });

  it("returns null when the metric is absent or lines is undefined", () => {
    expect(findSeriesLine([line("a")], "b")).toBeNull();
    expect(findSeriesLine(undefined, "a")).toBeNull();
  });
});

describe("seriesValues / latestPoint / deltaFromPoints — empty-points cases", () => {
  it("returns [] / null / null for a null line", () => {
    expect(seriesValues(null)).toEqual([]);
    expect(latestPoint(null)).toBeNull();
    expect(deltaFromPoints(null)).toBeNull();
  });

  it("returns [] / null / null for a line with zero points", () => {
    const l = line("nsc.distance", []);
    expect(seriesValues(l)).toEqual([]);
    expect(latestPoint(l)).toBeNull();
    expect(deltaFromPoints(l)).toBeNull();
  });

  it("returns null delta for exactly one point (never a fabricated 0)", () => {
    const l = line("nsc.distance", [{ date: "2026-08-01", value: 5 }]);
    expect(deltaFromPoints(l)).toBeNull();
    expect(latestPoint(l)).toBe(5);
  });

  it("computes latest and oldest-to-newest delta over multiple points", () => {
    const l = line("nsc.distance", [
      { date: "2026-08-01", value: 20 },
      { date: "2026-08-02", value: 22 },
      { date: "2026-08-03", value: 23.7 },
    ]);
    expect(seriesValues(l)).toEqual([20, 22, 23.7]);
    expect(latestPoint(l)).toBe(23.7);
    expect(deltaFromPoints(l)).toBeCloseTo(3.7);
  });
});

describe("shapeStat — northstar", () => {
  it("shapes the available case with pass/fail counts and a spark from nsc.distance", () => {
    const lines = [line("nsc.distance", [{ date: "2026-08-13", value: 22.3 }, { date: "2026-08-14", value: 23.7 }])];
    const tile = shapeStat("northstar", overview(), lines);
    expect(tile.primary).toBe("23.7%");
    expect(tile.caption).toContain("88.2% gate pass");
    expect(tile.caption).toContain("52 pass / 1 fail");
    expect(tile.sparkPoints).toEqual([22.3, 23.7]);
    expect(tile.tone).toBe("danger"); // checks_fail > 0
    expect(tile.available).toBe(true);
  });

  it("picks ok tone when there are zero failing checks and mechanics don't dominate", () => {
    const tile = shapeStat(
      "northstar",
      overview({
        northstar: {
          available: true,
          run_id: "nscert-t1-20260813T101005Z",
          verdict_ts: "2026-08-13T10:10:05Z",
          distance: 23.7,
          gate_pass_rate: 100,
          checks_pass: 53,
          checks_fail: 0,
          checks_not_evaluable: 5,
          checks_void: 0,
          delta_distance: 1.4,
        },
      }),
      [],
    );
    expect(tile.tone).toBe("ok");
  });

  it("picks warn tone when checks_fail is 0 but mechanics refusals outnumber passes (grok T2-N1)", () => {
    const tile = shapeStat(
      "northstar",
      overview({
        northstar: {
          available: true,
          run_id: "nscert-t1-20260813T101005Z",
          verdict_ts: "2026-08-13T10:10:05Z",
          distance: 23.7,
          gate_pass_rate: 100,
          checks_pass: 53,
          checks_fail: 0,
          checks_not_evaluable: 166,
          checks_void: 0,
          delta_distance: 1.4,
        },
      }),
      [],
    );
    expect(tile.tone).toBe("warn");
  });

  it("renders neutral/unavailable tone and '—' fail count when checks_fail is null (grok T2-M1/N1)", () => {
    const tile = shapeStat(
      "northstar",
      overview({
        northstar: {
          available: true,
          run_id: "nscert-t1-20260813T101005Z",
          verdict_ts: "2026-08-13T10:10:05Z",
          distance: null,
          gate_pass_rate: null,
          checks_pass: 0,
          checks_fail: null,
          checks_not_evaluable: 0,
          checks_void: 0,
          delta_distance: null,
        },
      }),
      [],
    );
    expect(tile.tone).toBe("default");
    expect(tile.primary).toBe("—"); // distance null -> never coerced to 0%
    expect(tile.caption).toBe("— gate pass · 0 pass / — fail");
    expect(tile.available).toBe(true); // the category IS available, individual measures just aren't yet
  });

  it("renders the neutral absent state honestly", () => {
    const tile = shapeStat("northstar", overview({ northstar: { available: false, reason: "no cert run recorded yet" } }), []);
    expect(tile.available).toBe(false);
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("no cert run recorded yet");
    expect(tile.tone).toBe("default");
  });
});

describe("northstarTone", () => {
  it("is neutral/default when checks_fail is null — unknown, not zero", () => {
    expect(northstarTone(null, 10, 2)).toBe("default");
  });
  it("is danger whenever checks_fail > 0, regardless of the mechanics split", () => {
    expect(northstarTone(1, 100, 0)).toBe("danger");
  });
  it("is warn when checks_fail is 0 but not_evaluable outnumbers pass", () => {
    expect(northstarTone(0, 10, 11)).toBe("warn");
  });
  it("is ok when checks_fail is 0 and not_evaluable does not outnumber pass", () => {
    expect(northstarTone(0, 10, 10)).toBe("ok");
    expect(northstarTone(0, 10, 5)).toBe("ok");
  });
});

describe("shapeStat — memory available:false", () => {
  it("renders a neutral '—' with the backend's reason, never 0 or success-styled", () => {
    const tile = shapeStat("memory", overview(), []);
    expect(tile.available).toBe(false);
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("no durable memcert runs recorded yet");
    expect(tile.tone).toBe("default");
    expect(tile.sparkPoints).toEqual([]);
  });

  it("shapes the available case (once memcert persists durable runs)", () => {
    const withMemory = overview({
      memory: { available: true, runs: 1, pass_rate: 95, last_run_ts: "2026-08-14T00:00:00Z", tests: 40, failures: 2, errors: 0, skipped: 1 },
    });
    const tile = shapeStat("memory", withMemory, []);
    expect(tile.available).toBe(true);
    expect(tile.primary).toBe("95.0%");
    expect(tile.caption).toBe("40 tests · 2 failing");
    expect(tile.tone).toBe("danger");
  });

  it("danger tone on errors alone, even with zero failures (grok T2-N2)", () => {
    const withMemory = overview({
      memory: { available: true, runs: 1, pass_rate: 90, last_run_ts: "2026-08-14T00:00:00Z", tests: 40, failures: 0, errors: 3, skipped: 0 },
    });
    const tile = shapeStat("memory", withMemory, []);
    expect(tile.tone).toBe("danger");
  });

  it("ok tone when both failures and errors are zero", () => {
    const withMemory = overview({
      memory: { available: true, runs: 1, pass_rate: 100, last_run_ts: "2026-08-14T00:00:00Z", tests: 40, failures: 0, errors: 0, skipped: 0 },
    });
    const tile = shapeStat("memory", withMemory, []);
    expect(tile.tone).toBe("ok");
  });

  it("renders '—' and neutral tone honestly when pass_rate is null, even with zero failures/errors (grok T2-M1, fixed T2-R2-N2)", () => {
    // Fixed per grok T2-R2-N2: a null pass_rate means the rate is unmeasured
    // (e.g. zero tests), not that it passed — danger/ok is a verdict about a
    // MEASURED rate, and there isn't one here. This test previously asserted
    // "ok" for this exact case, which is success-styling an unknown (Rule 2).
    const withMemory = overview({
      memory: { available: true, runs: 0, pass_rate: null, last_run_ts: "2026-08-14T00:00:00Z", tests: 0, failures: 0, errors: 0, skipped: 0 },
    });
    const tile = shapeStat("memory", withMemory, []);
    expect(tile.primary).toBe("—");
    expect(tile.tone).toBe("default");
  });

  it("danger/ok tone logic only applies once pass_rate is a measured number, not when it's null", () => {
    const nullRateWithFailures = overview({
      memory: { available: true, runs: 1, pass_rate: null, last_run_ts: "2026-08-14T00:00:00Z", tests: 10, failures: 3, errors: 0, skipped: 0 },
    });
    // Even with real failures present, a null rate still renders neutral —
    // the tone is about the RATE being unmeasured, not about failures being
    // absent (those are reported separately, honestly, in the caption).
    expect(shapeStat("memory", nullRateWithFailures, []).tone).toBe("default");
  });
});

describe("shapeStat — diagnostics and suite", () => {
  it("diagnostics: failing count leads, danger tone when features are failing", () => {
    const tile = shapeStat("diagnostics", overview(), []);
    expect(tile.primary).toBe("2 failing");
    expect(tile.caption).toBe("14 features · 3 stale · 2 aborted");
    expect(tile.tone).toBe("danger");
  });

  it("diagnostics: warn tone when nothing is failing but something is stale", () => {
    const tile = shapeStat(
      "diagnostics",
      overview({ diagnostics: { available: true, features_total: 10, features_failing: 0, features_stale: 1, last_run_ts: "x", aborted_recent: 0 } }),
      [],
    );
    expect(tile.tone).toBe("warn");
  });

  it("suite: renders latest-lane detail in the caption", () => {
    const tile = shapeStat("suite", overview(), []);
    expect(tile.primary).toBe("93.1%");
    expect(tile.caption).toBe("41 runs (7d) · fast-lane: 3 failing / 11510 tests");
    expect(tile.tone).toBe("warn"); // 93.1 is < 95
  });

  it("suite: handles a null latest_lane honestly — zero runs means an undefined rate, not a fabricated 0% (grok T2-M1)", () => {
    // Fixed per grok T2-M1: 0 runs in the window means there is nothing to
    // divide by, so the backend sends pass_rate_7d: null here, not 0 — this
    // test previously locked in a coerced `pass_rate_7d: 0`, which is
    // exactly the "unknown read as favorable/zero" anti-pattern Rule 2 bans.
    // Also the runs_7d:0 counterpart to the null-runs_7d test below: the
    // store DOES have history here (runs_7d is a real 0, not null), so it
    // renders the real count "0 runs (7d)", never "—".
    const tile = shapeStat("suite", overview({ suite: { available: true, runs_7d: 0, pass_rate_7d: null, latest_lane: null } }), []);
    expect(tile.caption).toBe("0 runs (7d)");
    expect(tile.primary).toBe("—");
    expect(tile.tone).toBe("default");
  });

  it("suite: runs_7d null renders '—' and neutral tone — no merge-gate receipt store exists at all (backend contract update)", () => {
    // null !== 0 here: null means the store itself is unmeasured (never
    // coerced to a fabricated "0 runs"); a real 0 means the store exists
    // and has history, it just saw nothing in the window (see the test
    // above). Same treatment as pass_rate_7d's own null-honesty guard.
    const tile = shapeStat("suite", overview({ suite: { available: true, runs_7d: null, pass_rate_7d: null, latest_lane: null } }), []);
    expect(tile.caption).toBe("— runs (7d)");
    expect(tile.primary).toBe("—");
    expect(tile.tone).toBe("default");
  });

  it("suite: runs_7d null still forces neutral tone even if pass_rate_7d were somehow a number", () => {
    // Defensive: the receipt store being entirely unmeasured must win over
    // any stray rate value the backend might still attach.
    const tile = shapeStat("suite", overview({ suite: { available: true, runs_7d: null, pass_rate_7d: 97, latest_lane: null } }), []);
    expect(tile.caption).toBe("— runs (7d)");
    expect(tile.tone).toBe("default");
  });

  it("suite: a real, measured 0% pass rate (runs happened, all failed) still renders as 0%, tone danger", () => {
    const tile = shapeStat("suite", overview({ suite: { available: true, runs_7d: 5, pass_rate_7d: 0, latest_lane: null } }), []);
    expect(tile.caption).toBe("5 runs (7d)");
    expect(tile.primary).toBe("0.0%");
    expect(tile.tone).toBe("danger");
  });

  it("suite unavailable renders the neutral absent state", () => {
    const tile = shapeStat("suite", overview({ suite: { available: false, reason: "no gate-evidence receipts in window" } }), []);
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("no gate-evidence receipts in window");
    expect(tile.tone).toBe("default");
  });
});

describe("seriesIsEmpty", () => {
  it("is true for null, unavailable, and all-empty-lines responses", () => {
    expect(seriesIsEmpty(null)).toBe(true);
    expect(seriesIsEmpty({ category: "memory", days: 30, available: false, reason: "x" })).toBe(true);
    const res: TestObsSeriesResponse = { category: "northstar", days: 30, available: true, series: [line("nsc.distance", [])] };
    expect(seriesIsEmpty(res)).toBe(true);
  });

  it("is false when at least one line has points", () => {
    const res: TestObsSeriesResponse = {
      category: "northstar",
      days: 30,
      available: true,
      series: [line("nsc.distance", []), line("nsc.void_ratio", [{ date: "2026-08-14", value: 0.5 }])],
    };
    expect(seriesIsEmpty(res)).toBe(false);
  });
});

describe("toLineSeries", () => {
  it("maps date/value points to x/y and uses the label as the series name", () => {
    const lines = [line("nsc.distance", [{ date: "2026-08-14", value: 23.7 }])];
    lines[0]!.label = "Distance";
    const out = toLineSeries(lines);
    expect(out).toEqual([{ name: "Distance", points: [{ x: "2026-08-14", y: 23.7 }] }]);
  });

  it("returns [] for [] and preserves empty points arrays per line", () => {
    expect(toLineSeries([])).toEqual([]);
    const out = toLineSeries([line("nsc.distance", [])]);
    expect(out).toEqual([{ name: "nsc.distance", points: [] }]);
  });
});

describe("weakspotTone", () => {
  it("maps FAIL and ERR to danger — actual failures/errors (backend contract: closed 8-value union)", () => {
    expect(weakspotTone("FAIL")).toBe("danger");
    expect(weakspotTone("ERR")).toBe("danger");
  });

  it("maps ABORT/MISS/EMPTY/STALE to warn — coverage/mechanics gaps, not a graded failure", () => {
    expect(weakspotTone("ABORT")).toBe("warn");
    expect(weakspotTone("MISS")).toBe("warn");
    expect(weakspotTone("EMPTY")).toBe("warn");
    expect(weakspotTone("STALE")).toBe("warn");
  });

  it("keeps NOT_EVALUABLE's existing warn tone and VOID's existing neutral tone", () => {
    expect(weakspotTone("NOT_EVALUABLE")).toBe("warn");
    expect(weakspotTone("VOID")).toBe("neutral");
  });

  it("never renders green: none of the eight known statuses maps to an ok/success tone", () => {
    const ALL_STATUSES = ["FAIL", "ERR", "ABORT", "MISS", "EMPTY", "STALE", "VOID", "NOT_EVALUABLE"];
    for (const status of ALL_STATUSES) {
      expect(["danger", "warn", "neutral"]).toContain(weakspotTone(status));
    }
  });

  it("falls back to neutral for an unrecognized status — never green, never a crash", () => {
    expect(weakspotTone("SOMETHING_UNKNOWN")).toBe("neutral");
    expect(weakspotTone("")).toBe("neutral");
  });
});

function wsItem(overrides: Partial<WeakspotItem> = {}): WeakspotItem {
  return {
    category: "northstar",
    id: "NSC-1",
    label: "NSC-1",
    status: "FAIL",
    detail: "",
    since: "2026-08-13",
    ...overrides,
  };
}

function sources(overrides: Partial<WeakspotSources> = {}): WeakspotSources {
  return {
    northstar: { available: true, reason: null },
    memory: { available: true, reason: null },
    diagnostics: { available: true, reason: null },
    ...overrides,
  };
}

describe("allSourcesAvailable", () => {
  it("is true only when every source is available", () => {
    expect(allSourcesAvailable(sources())).toBe(true);
  });

  it("is false when any single source is unavailable", () => {
    expect(allSourcesAvailable(sources({ memory: { available: false, reason: "no durable memcert runs recorded yet" } }))).toBe(false);
  });

  it("is false for null/undefined sources (grok T2-M2 — never default to a false all-clear)", () => {
    expect(allSourcesAvailable(null)).toBe(false);
    expect(allSourcesAvailable(undefined)).toBe(false);
  });

  it("does NOT all-clear on an empty object — a missing expected key counts as unavailable (grok T2-R2-N1)", () => {
    // Iterating Object.keys({}) would find nothing to check and vacuously
    // report "every source available" — favourable absence. Must be false.
    expect(allSourcesAvailable({} as WeakspotSources)).toBe(false);
  });

  it("ignores an unexpected extra key — it must not count toward the verdict (grok T2-R2-N1)", () => {
    const withExtraSuiteKey = {
      ...sources(),
      suite: { available: false, reason: "should never be consulted" },
    } as WeakspotSources;
    expect(allSourcesAvailable(withExtraSuiteKey)).toBe(true);
  });
});

describe("unavailableSources", () => {
  it("returns [] when every source is available", () => {
    expect(unavailableSources(sources())).toEqual([]);
  });

  it("lists only the unavailable sources, with their reasons", () => {
    const result = unavailableSources(
      sources({
        memory: { available: false, reason: "no durable memcert runs recorded yet" },
        diagnostics: { available: false, reason: "feature-health ledger unreadable" },
      }),
    );
    expect(result).toEqual([
      { category: "memory", reason: "no durable memcert runs recorded yet" },
      { category: "diagnostics", reason: "feature-health ledger unreadable" },
    ]);
  });

  it("reports every expected key for null/undefined sources — consistent with allSourcesAvailable(null) === false", () => {
    const all = [
      { category: "northstar", reason: "not reported" },
      { category: "memory", reason: "not reported" },
      { category: "diagnostics", reason: "not reported" },
    ];
    expect(unavailableSources(null)).toEqual(all);
    expect(unavailableSources(undefined)).toEqual(all);
  });

  it("reports every expected key missing from an empty object, with reason 'not reported' (grok T2-R2-N1)", () => {
    expect(unavailableSources({} as WeakspotSources)).toEqual([
      { category: "northstar", reason: "not reported" },
      { category: "memory", reason: "not reported" },
      { category: "diagnostics", reason: "not reported" },
    ]);
  });

  it("ignores an unexpected extra key entirely — never lists it (grok T2-R2-N1)", () => {
    const withExtraSuiteKey = {
      ...sources(),
      suite: { available: false, reason: "should never be consulted" },
    } as WeakspotSources;
    expect(unavailableSources(withExtraSuiteKey)).toEqual([]);
  });
});

describe("groupWeakspotsByCategory", () => {
  it("groups items by category key", () => {
    const groups = groupWeakspotsByCategory([
      wsItem({ id: "a", category: "northstar" }),
      wsItem({ id: "b", category: "diagnostics" }),
      wsItem({ id: "c", category: "northstar" }),
    ]);
    expect(Object.keys(groups).sort()).toEqual(["diagnostics", "northstar"]);
    expect(groups.northstar?.map((i) => i.id)).toEqual(["a", "c"]);
    expect(groups.diagnostics?.map((i) => i.id)).toEqual(["b"]);
  });

  it("returns {} for an empty list", () => {
    expect(groupWeakspotsByCategory([])).toEqual({});
  });
});
