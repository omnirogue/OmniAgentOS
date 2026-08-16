import { describe, expect, it } from "vitest";
import {
  budgetFillTone,
  budgetRatio,
  compactRelative,
  formatMoney,
  parentBreadcrumb,
  stateStripeClass,
  stateToDot,
} from "./format";
import { buildForest, fuzzyMatchProject } from "./tree";
import type { PortfolioProject } from "./types";

function proj(
  partial: Partial<PortfolioProject> & Pick<PortfolioProject, "id" | "name">,
): PortfolioProject {
  return {
    parent_id: null,
    kind: "project",
    path: [partial.name],
    depth: 0,
    state: "idle",
    rollup_state: "idle",
    doing: "",
    blocked_count: 0,
    failed_count: 0,
    running_count: 0,
    budget_usd: null,
    spent_usd: 0,
    last_activity_at: null,
    ...partial,
  };
}

describe("portfolio format", () => {
  it("maps states to design-system dots (no accent-for-status)", () => {
    expect(stateToDot("blocked")).toBe("awaiting");
    expect(stateToDot("failing")).toBe("failed");
    expect(stateToDot("running")).toBe("running");
    expect(stateToDot("healthy")).toBe("ok");
    expect(stateToDot("idle")).toBe("queued");
  });

  it("maps stripe classes to semantic colours", () => {
    expect(stateStripeClass("blocked")).toBe("block");
    expect(stateStripeClass("failing")).toBe("fail");
    expect(stateStripeClass("running")).toBe("run");
    expect(stateStripeClass("healthy")).toBe("ok");
    expect(stateStripeClass("idle")).toBe("idle");
  });

  it("treats null budget as uncapped (no fill ratio)", () => {
    expect(budgetRatio(18.4, null)).toBeNull();
    expect(budgetFillTone(null)).toBeNull();
    expect(budgetRatio(40, 80)).toBe(0.5);
    expect(budgetFillTone(0.95)).toBe("danger");
  });

  it("treats null spent_usd as unknown — never $0, never ok tone", () => {
    // Decisive: unmeasured spend (the case that used to collapse to confident $0).
    const unknown = proj({
      id: "unknown-spend",
      name: "Unmeasured",
      spent_usd: null,
      budget_usd: 100,
    });
    expect(unknown.spent_usd).toBeNull();
    expect(formatMoney(unknown.spent_usd)).toBe("—");
    expect(formatMoney(unknown.spent_usd)).not.toMatch(/\$0/);
    const unknownRatio = budgetRatio(unknown.spent_usd, unknown.budget_usd);
    expect(unknownRatio).toBeNull();
    expect(budgetFillTone(unknownRatio)).toBeNull();
    expect(budgetFillTone(unknownRatio)).not.toBe("ok");

    // Control: genuine zero spend is a different, measured fact.
    const zero = proj({
      id: "zero-spend",
      name: "ZeroSpend",
      spent_usd: 0,
      budget_usd: 100,
    });
    expect(zero.spent_usd).toBe(0);
    expect(formatMoney(zero.spent_usd)).toBe("$0.00");
    const zeroRatio = budgetRatio(zero.spent_usd, zero.budget_usd);
    expect(zeroRatio).toBe(0);
    expect(budgetFillTone(zeroRatio)).toBe("ok");
  });

  // The BudgetMeter / AttentionQueue render tests that used to live here were
  // removed with those components in the dashboard prune. The invariant they
  // guarded (unknown spend is never coerced to a confident $0) is still covered
  // above by the pure formatMoney / budgetRatio / budgetFillTone assertions.
  it("parent breadcrumb drops the leaf name", () => {
    expect(parentBreadcrumb(["Brands", "Globex", "Direct to Consumer"], "Direct to Consumer")).toBe(
      "Brands / Globex",
    );
    expect(parentBreadcrumb(["Advertising"], "Advertising")).toBe("");
  });

  it("compact relative handles recent timestamps", () => {
    expect(compactRelative(new Date().toISOString())).toBe("now");
    expect(compactRelative(null)).toBe("—");
  });
});

describe("portfolio tree", () => {
  it("builds a forest from parent_id edges", () => {
    const brands = proj({ id: "b", name: "Brands", depth: 0, path: ["Brands"] });
    const cf = proj({
      id: "cf",
      name: "Globex",
      parent_id: "b",
      depth: 1,
      path: ["Brands", "Globex"],
    });
    const dtc = proj({
      id: "dtc",
      name: "Direct to Consumer",
      parent_id: "cf",
      depth: 2,
      path: ["Brands", "Globex", "Direct to Consumer"],
    });
    const ad = proj({ id: "ad", name: "Advertising", depth: 0, path: ["Advertising"] });

    const forest = buildForest([dtc, cf, brands, ad]);
    expect(forest.map((n) => n.project.name)).toEqual(["Advertising", "Brands"]);
    const brandsNode = forest.find((n) => n.project.id === "b")!;
    expect(brandsNode.children).toHaveLength(1);
    expect(brandsNode.children[0]!.project.name).toBe("Globex");
    expect(brandsNode.children[0]!.children[0]!.project.name).toBe("Direct to Consumer");
  });

  it("fuzzy matches name and path tokens", () => {
    const p = proj({
      id: "1",
      name: "Globex — Direct to Consumer",
      path: ["Brands", "Globex", "Direct to Consumer"],
    });
    expect(fuzzyMatchProject(p, "click")).toBe(true);
    expect(fuzzyMatchProject(p, "brands dtc")).toBe(false);
    expect(fuzzyMatchProject(p, "brands direct")).toBe(true);
    expect(fuzzyMatchProject(p, "")).toBe(true);
  });
});
