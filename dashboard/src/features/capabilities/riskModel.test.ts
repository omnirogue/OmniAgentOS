import { describe, expect, it } from "vitest";
import type { CapabilitiesCatalog } from "./types";
import { ACTION_CLASS_ORDER, computeGrantSummary, isAutoClass } from "./riskModel";

const everyActionClassCatalog: CapabilitiesCatalog = {
  groups: { test: { label: "Test", danger: false } },
  connectors: [
    {
      id: "test",
      label: "Test connector",
      group: "test",
      env_names: [],
      capabilities: ACTION_CLASS_ORDER.map((action_class) => ({
        id: `test.${action_class}`,
        label: action_class,
        action_class,
        callable_now: true,
        always_human: action_class === "consequential" || action_class === "irreversible",
      })),
    },
  ],
};

describe("isAutoClass", () => {
  it("allows only non-human action classes to run automatically", () => {
    expect(isAutoClass("read_only")).toBe(true);
    expect(isAutoClass("sandboxed_creation")).toBe(true);
    expect(isAutoClass("internal_reversible")).toBe(true);
    expect(isAutoClass("external_reversible")).toBe(false);
    expect(isAutoClass("consequential")).toBe(false);
    expect(isAutoClass("irreversible")).toBe(false);
  });
});

describe("computeGrantSummary", () => {
  it("places every action class in exactly one blast-radius decision bucket", () => {
    const granted = everyActionClassCatalog.connectors[0].capabilities.map(({ id }) => id);
    const summary = computeGrantSummary(everyActionClassCatalog, granted);
    const automatic = everyActionClassCatalog.connectors[0].capabilities
      .filter(({ action_class }) => isAutoClass(action_class))
      .map(({ id }) => id);
    const buckets = [automatic, summary.needs_approval, summary.always_human];
    const bucketedIds = buckets.flat();

    expect(Object.values(summary.by_action_class)).toEqual([1, 1, 1, 1, 1, 1]);
    expect(new Set(bucketedIds)).toHaveLength(bucketedIds.length);
    expect(bucketedIds).toHaveLength(summary.total);
    expect(bucketedIds.sort()).toEqual(granted.sort());
  });
});
