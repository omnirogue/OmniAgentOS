import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HealthTable } from "./HealthTable";
import { sortBrokenFirst } from "./logic";
import type { CapabilityHealth } from "./types";

function makeCapability(overrides: Partial<CapabilityHealth>): CapabilityHealth {
  return {
    id: "cap",
    name: "cap",
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

function rowOrder(): string[] {
  const rows = screen.getAllByRole("row").slice(1); // drop the header row
  return rows.map((row) => within(row).getAllByRole("cell")[0]!.textContent ?? "");
}

describe("HealthTable — broken-first ordering with zero interaction", () => {
  it("renders a DOWN capability above an OK capability without any click, given rows already broken-first sorted by the caller", () => {
    const capabilities = sortBrokenFirst([
      makeCapability({ id: "healthy-service", name: "healthy-service", status: "OK" }),
      makeCapability({ id: "broken-service", name: "broken-service", status: "DOWN" }),
    ]);
    render(<HealthTable capabilities={capabilities} onSelect={vi.fn()} />);

    const order = rowOrder();
    expect(order.indexOf("broken-service")).toBeLessThan(order.indexOf("healthy-service"));
  });

  it("renders the full broken-first tier order top to bottom with no interaction", () => {
    const capabilities = sortBrokenFirst([
      makeCapability({ id: "a", name: "a", status: "OK" }),
      makeCapability({ id: "b", name: "b", status: "UNVERIFIED" }),
      makeCapability({ id: "c", name: "c", status: "DOWN" }),
      makeCapability({ id: "d", name: "d", status: "STALE" }),
      makeCapability({ id: "e", name: "e", status: "CANNOT_EVALUATE" }),
      makeCapability({ id: "f", name: "f", status: "DEGRADED" }),
    ]);
    render(<HealthTable capabilities={capabilities} onSelect={vi.fn()} />);
    expect(rowOrder()).toEqual(["c", "f", "d", "e", "b", "a"]);
  });

  it("UNVERIFIED and CANNOT_EVALUATE rows render a visible status badge that is not the OK/green badge", () => {
    const capabilities = [
      makeCapability({ id: "unwatched", name: "unwatched", status: "UNVERIFIED" }),
      makeCapability({ id: "cant-tell", name: "cant-tell", status: "CANNOT_EVALUATE" }),
      makeCapability({ id: "healthy", name: "healthy", status: "OK" }),
    ];
    render(<HealthTable capabilities={capabilities} onSelect={vi.fn()} />);

    expect(screen.getByText("Unverified").className).not.toContain("ds-badge--ok");
    expect(screen.getByText("Cannot evaluate").className).not.toContain("ds-badge--ok");
    expect(screen.getByText("OK").className).toContain("ds-badge--ok");
  });

  it("renders an empty state message when no rows match", () => {
    render(<HealthTable capabilities={[]} onSelect={vi.fn()} />);
    expect(screen.getByText("No capabilities match the current filters.")).toBeInTheDocument();
  });
});
