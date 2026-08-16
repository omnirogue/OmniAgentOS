import { describe, expect, it } from "vitest";
import {
  EMPTY_FILTERS,
  countByStatus,
  filterCapabilities,
  sortBrokenFirst,
  sortCapabilities,
  statusRank,
} from "./logic";
import type { CapabilityHealth } from "./types";

function makeCapability(overrides: Partial<CapabilityHealth>): CapabilityHealth {
  return {
    id: "cap-1",
    name: "cap-1",
    company: "estate",
    kind: "external-service",
    what_it_does: "does a thing",
    status: "OK",
    last_checked: "2026-08-15T10:00:00Z",
    last_good: "2026-08-15T10:00:00Z",
    metric: null,
    owner: "estate-ops",
    evidence: null,
    ...overrides,
  };
}

describe("statusRank / broken-first ordering", () => {
  it("ranks DOWN before OK", () => {
    expect(statusRank("DOWN")).toBeLessThan(statusRank("OK"));
  });

  it("ranks every status in the exact declared order: DOWN, DEGRADED, STALE, CANNOT_EVALUATE, UNVERIFIED, OK", () => {
    const order = ["DOWN", "DEGRADED", "STALE", "CANNOT_EVALUATE", "UNVERIFIED", "OK"] as const;
    for (let i = 0; i < order.length - 1; i += 1) {
      expect(statusRank(order[i])).toBeLessThan(statusRank(order[i + 1]));
    }
  });
});

describe("sortBrokenFirst", () => {
  it("puts a DOWN row above an OK row with no interaction, from an OK-first input", () => {
    const input = [
      makeCapability({ id: "healthy", status: "OK" }),
      makeCapability({ id: "outage", status: "DOWN" }),
    ];
    const sorted = sortBrokenFirst(input);
    expect(sorted.map((c) => c.id)).toEqual(["outage", "healthy"]);
  });

  it("places every non-OK status ahead of every OK status", () => {
    const input = [
      makeCapability({ id: "a", status: "OK" }),
      makeCapability({ id: "b", status: "UNVERIFIED" }),
      makeCapability({ id: "c", status: "OK" }),
      makeCapability({ id: "d", status: "STALE" }),
      makeCapability({ id: "e", status: "DOWN" }),
      makeCapability({ id: "f", status: "CANNOT_EVALUATE" }),
      makeCapability({ id: "g", status: "DEGRADED" }),
    ];
    const sorted = sortBrokenFirst(input);
    expect(sorted.map((c) => c.id)).toEqual(["e", "g", "d", "f", "b", "a", "c"]);
  });

  it("is stable/deterministic within a status tier (falls back to id)", () => {
    const input = [
      makeCapability({ id: "zeta", status: "DOWN" }),
      makeCapability({ id: "alpha", status: "DOWN" }),
    ];
    expect(sortBrokenFirst(input).map((c) => c.id)).toEqual(["alpha", "zeta"]);
  });
});

describe("sortCapabilities (user-picked column sort)", () => {
  it("sorting by status ascending matches the broken-first order, not alphabetical", () => {
    const input = [
      makeCapability({ id: "a", status: "OK" }),
      makeCapability({ id: "b", status: "DOWN" }),
    ];
    const sorted = sortCapabilities(input, "status", "asc");
    expect(sorted.map((c) => c.id)).toEqual(["b", "a"]);
  });

  it("sorts by last_checked", () => {
    const input = [
      makeCapability({ id: "older", last_checked: "2026-08-01T00:00:00Z" }),
      makeCapability({ id: "newer", last_checked: "2026-08-15T00:00:00Z" }),
    ];
    expect(sortCapabilities(input, "last_checked", "asc").map((c) => c.id)).toEqual(["older", "newer"]);
    expect(sortCapabilities(input, "last_checked", "desc").map((c) => c.id)).toEqual(["newer", "older"]);
  });
});

describe("filterCapabilities", () => {
  const fixture: CapabilityHealth[] = [
    makeCapability({ id: "a", company: "estate", kind: "external-service", status: "OK" }),
    makeCapability({ id: "b", company: "globex", kind: "external-service", status: "DOWN" }),
    makeCapability({ id: "c", company: "estate", kind: "llm-loop", status: "DOWN" }),
    makeCapability({ id: "d", company: "globex", kind: "llm-loop", status: "OK" }),
  ];

  it("no filters returns everything", () => {
    expect(filterCapabilities(fixture, EMPTY_FILTERS)).toHaveLength(4);
  });

  it("filters by company alone", () => {
    const result = filterCapabilities(fixture, { ...EMPTY_FILTERS, company: "estate" });
    expect(result.map((c) => c.id).sort()).toEqual(["a", "c"]);
  });

  it("filters by kind alone", () => {
    const result = filterCapabilities(fixture, { ...EMPTY_FILTERS, kind: "llm-loop" });
    expect(result.map((c) => c.id).sort()).toEqual(["c", "d"]);
  });

  it("filters by status alone", () => {
    const result = filterCapabilities(fixture, { ...EMPTY_FILTERS, status: "DOWN" });
    expect(result.map((c) => c.id).sort()).toEqual(["b", "c"]);
  });

  it("composes company AND kind AND status together", () => {
    const result = filterCapabilities(fixture, { company: "estate", kind: "llm-loop", status: "DOWN" });
    expect(result.map((c) => c.id)).toEqual(["c"]);
  });

  it("composed filters that match nothing return an empty list, not a fallback", () => {
    const result = filterCapabilities(fixture, { company: "estate", kind: "llm-loop", status: "OK" });
    expect(result).toEqual([]);
  });
});

describe("countByStatus", () => {
  it("counts every declared status, including zero counts, and treats UNVERIFIED as first-class", () => {
    const fixture: CapabilityHealth[] = [
      makeCapability({ id: "a", status: "OK" }),
      makeCapability({ id: "b", status: "UNVERIFIED" }),
      makeCapability({ id: "c", status: "UNVERIFIED" }),
    ];
    const counts = countByStatus(fixture);
    expect(counts.UNVERIFIED).toBe(2);
    expect(counts.OK).toBe(1);
    expect(counts.DOWN).toBe(0);
    expect(counts.DEGRADED).toBe(0);
    expect(counts.STALE).toBe(0);
    expect(counts.CANNOT_EVALUATE).toBe(0);
  });
});
