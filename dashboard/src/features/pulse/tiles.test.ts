import { describe, expect, it } from "vitest";
import type { CapabilitySnapshot, SeriesResponse } from "./types";
import {
  deltaFromSeries,
  formatValue,
  latestValue,
  shapeCapabilityTile,
  shapeImprovementsTile,
  shapeLoopsTile,
  shapeMemoryTile,
  shapeReliabilityTile,
  shapeSkillsTile,
  trendFromDelta,
} from "./tiles";

function series(points: Array<{ date: string; value: number }>): SeriesResponse {
  return { metric: "skills.total", points };
}

describe("latestValue", () => {
  it("returns the last point's value", () => {
    expect(latestValue(series([
      { date: "2026-01-01", value: 1 },
      { date: "2026-01-02", value: 42 },
    ]))).toBe(42);
  });

  it("returns 0 on null / empty series", () => {
    expect(latestValue(null)).toBe(0);
    expect(latestValue({ metric: "x", points: [] })).toBe(0);
  });
});

describe("deltaFromSeries", () => {
  it("returns latest minus first on series >=2 points", () => {
    expect(deltaFromSeries(series([
      { date: "2026-01-01", value: 10 },
      { date: "2026-01-02", value: 15 },
      { date: "2026-01-03", value: 25 },
    ]))).toBe(15);
  });

  it("returns 0 on fewer than 2 points", () => {
    expect(deltaFromSeries(series([{ date: "2026-01-01", value: 10 }]))).toBe(0);
    expect(deltaFromSeries(null)).toBe(0);
  });

  it("returns a negative delta when trending down", () => {
    expect(deltaFromSeries(series([
      { date: "2026-01-01", value: 50 },
      { date: "2026-01-02", value: 40 },
    ]))).toBe(-10);
  });
});

describe("formatValue", () => {
  it("formats absolute counts as integers", () => {
    expect(formatValue("skills.total", 42)).toBe("42");
    expect(formatValue("loops.fires", 7)).toBe("7");
  });

  it("formats rates as percentages", () => {
    expect(formatValue("loops.acceptance", 0.75)).toBe("75.0%");
    expect(formatValue("reliability.score", 0.965)).toBe("96.5%");
  });

  it("rounds non-integer counts to one decimal", () => {
    expect(formatValue("skills.total", 12.5)).toBe("12.5");
  });
});

describe("trendFromDelta", () => {
  it("classifies positive deltas as up", () => {
    expect(trendFromDelta(1)).toBe("up");
    expect(trendFromDelta(0.1)).toBe("up");
  });
  it("classifies negative deltas as down", () => {
    expect(trendFromDelta(-1)).toBe("down");
    expect(trendFromDelta(-0.01)).toBe("down");
  });
  it("classifies zero as neutral", () => {
    expect(trendFromDelta(0)).toBe("neutral");
  });
});

describe("shapeSkillsTile", () => {
  it("uses total as the headline and versions as caption", () => {
    const total = series([
      { date: "2026-01-01", value: 40 },
      { date: "2026-01-02", value: 45 },
    ]);
    const versions: SeriesResponse = {
      metric: "skills.versions",
      points: [{ date: "2026-01-01", value: 3 }],
    };
    const tile = shapeSkillsTile(total, versions);
    expect(tile.title).toBe("Skills");
    expect(tile.primary).toBe("45");
    expect(tile.caption).toBe("3 versions this week");
    expect(tile.delta?.value).toBe(5);
    expect(tile.delta?.trend).toBe("up");
    expect(tile.deepLink).toBe("/skills");
  });

  it("handles null series gracefully", () => {
    const tile = shapeSkillsTile(null, null);
    expect(tile.primary).toBe("0");
    expect(tile.sparkPoints).toEqual([]);
  });
});

describe("shapeImprovementsTile", () => {
  it("uses apply tone and improvements deep link", () => {
    const s = series([
      { date: "2026-01-01", value: 5 },
      { date: "2026-01-02", value: 9 },
    ]);
    const tile = shapeImprovementsTile(s);
    expect(tile.title).toBe("Self-improvement");
    expect(tile.primary).toBe("9");
    expect(tile.tone).toBe("promote");
    expect(tile.deepLink).toBe("/improvements");
  });
});

describe("shapeLoopsTile", () => {
  it("combines fires count with acceptance % in caption", () => {
    const fires = series([
      { date: "2026-01-01", value: 5 },
      { date: "2026-01-02", value: 8 },
    ]);
    const acc: SeriesResponse = {
      metric: "loops.acceptance",
      points: [{ date: "2026-01-02", value: 0.75 }],
    };
    const tile = shapeLoopsTile(fires, acc);
    expect(tile.title).toBe("Loops");
    expect(tile.primary).toBe("8");
    expect(tile.caption).toContain("75.0%");
    expect(tile.deepLink).toBe("/loops");
  });
});

describe("shapeReliabilityTile", () => {
  it("picks OK tone at >= 0.95", () => {
    const s = series([{ date: "2026-01-02", value: 0.97 }]);
    expect(shapeReliabilityTile(s).tone).toBe("ok");
  });
  it("picks WARN tone between 0.8 and 0.95", () => {
    const s = series([{ date: "2026-01-02", value: 0.85 }]);
    expect(shapeReliabilityTile(s).tone).toBe("warn");
  });
  it("picks DANGER tone below 0.8", () => {
    const s = series([{ date: "2026-01-02", value: 0.6 }]);
    expect(shapeReliabilityTile(s).tone).toBe("danger");
  });
  it("formats value as percentage", () => {
    const s = series([{ date: "2026-01-02", value: 0.9234 }]);
    expect(shapeReliabilityTile(s).primary).toBe("92.3%");
  });
});

describe("shapeMemoryTile", () => {
  it("deep-links to /knowledge", () => {
    const tile = shapeMemoryTile(null);
    expect(tile.deepLink).toBe("/knowledge");
    expect(tile.title).toBe("Memory & Knowledge");
  });
});

describe("shapeCapabilityTile", () => {
  it("renders the ELO leader with ranked/tournament counts", () => {
    const snapshot: CapabilitySnapshot = {
      topElo: 1384.4,
      topConfig: "Panel · Claude+Codex+Grok",
      ranked: 9,
      subjects: 3,
      tournamentsCompleted: 12,
    };
    const tile = shapeCapabilityTile(snapshot);
    expect(tile.title).toBe("Capability");
    expect(tile.primary).toBe("1384");
    expect(tile.caption).toContain("Panel · Claude+Codex+Grok");
    expect(tile.caption).toContain("9 ranked across 3 subjects");
    expect(tile.caption).toContain("12 tournaments run");
    expect(tile.deepLink).toBe("/lab");
    // No ELO time series exists — no sparkline, no fabricated delta.
    expect(tile.sparkPoints).toEqual([]);
    expect(tile.delta).toBeUndefined();
  });

  it("handles the empty leaderboard honestly", () => {
    const tile = shapeCapabilityTile({
      topElo: null,
      topConfig: null,
      ranked: 0,
      subjects: 0,
      tournamentsCompleted: 0,
    });
    expect(tile.primary).toBe("—");
    expect(tile.caption).toBe("no configs ranked yet");
    expect(tile.deepLink).toBe("/lab");
  });

  it("tolerates a null snapshot", () => {
    const tile = shapeCapabilityTile(null);
    expect(tile.primary).toBe("—");
    expect(tile.deepLink).toBe("/lab");
  });
});
