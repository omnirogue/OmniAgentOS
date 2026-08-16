import { describe, expect, it } from "vitest";
import { normalizeSuggestion } from "./api";
import { RISK_CLASSES } from "./types";

describe("normalizeSuggestion restrictive fallbacks", () => {
  it.each([undefined, "", "   "])("defaults a missing or blank plan action class to irreversible (%j)", (actionClass) => {
    expect(
      normalizeSuggestion({
        proposed_plan: { action_class: actionClass },
      }).proposed_plan?.action_class,
    ).toBe("irreversible");
  });

  it("preserves irreversible risk instead of coercing it to read_only", () => {
    expect(normalizeSuggestion({ risk_class: "irreversible" }).risk_class).toBe("irreversible");
  });

  it("coerces an unrecognized risk class to irreversible", () => {
    expect(normalizeSuggestion({ risk_class: "totally-bogus-value" }).risk_class).toBe("irreversible");
  });

  // Ordering lock: mostRestrictiveRiskClass() (api.ts) assumes the LAST entry
  // in RISK_CLASSES is the most restrictive class, mirroring the same
  // most-restrictive-last convention as contracts.ActionClass /
  // capabilities/riskModel.ts's ACTION_CLASS_ORDER. If a future edit appends
  // a class after "irreversible" (or reorders the array), the fallback would
  // silently start coercing unknown values to the wrong (less restrictive)
  // class — this test pins the assumption so that edit fails loudly here.
  it("keeps irreversible as the last (most restrictive) entry in RISK_CLASSES", () => {
    expect(RISK_CLASSES[RISK_CLASSES.length - 1]).toBe("irreversible");
  });
});
