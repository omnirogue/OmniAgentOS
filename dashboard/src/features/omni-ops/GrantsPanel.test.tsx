import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { GrokGrant } from "./types";

const grants = vi.fn();

vi.mock("./api", () => ({
  grokOpsApi: {
    grants: (...args: unknown[]) => grants(...args),
  },
}));

import { GrantsPanel } from "./GrantsPanel";

function grokGrant(overrides: Partial<GrokGrant> = {}): GrokGrant {
  return {
    id: "grant_1",
    capability: "spend.approve",
    label: "Approve small spend",
    project_id: null,
    approval_id: "appr_1",
    max_actions: 10,
    actions_used: 1,
    max_spend_usd: 100,
    spend_used_usd: 12.5,
    expires_at: "2026-09-01T00:00:00Z",
    status: "active",
    metadata: {},
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

// D-2 (LiveSim LS-004) audit find: `grants` is never cleared on a failed
// refresh, so "Total grants: 0" only means "confirmed empty" when `error`
// is unset -- an authorization-spend surface must never misread "unknown"
// as "zero".
describe("GrantsPanel stats vs. an unknown fetch (D-2)", () => {
  it("shows an unknown state, never a confident 0, when grants have never loaded and the fetch failed", async () => {
    grants.mockRejectedValue(new Error("network unreachable"));

    render(<GrantsPanel />);

    await waitFor(() => {
      expect(screen.getByText("Grants unavailable")).toBeInTheDocument();
    });
    const stats = screen.getAllByText("—");
    expect(stats.length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("still shows a real 0 total/active grants and $0.00 spend when the fetch genuinely succeeded empty", async () => {
    grants.mockResolvedValue([]);

    render(<GrantsPanel />);

    await waitFor(() => {
      expect(screen.getByText("No active grants")).toBeInTheDocument();
    });
    expect(screen.getAllByText("0", { selector: ".ds-stat__value" })).toHaveLength(2);
    expect(screen.getByText("$0.00")).toBeInTheDocument();
  });

  it("keeps the real stats when a later refresh errors after a successful load (stale beats hidden)", async () => {
    grants.mockResolvedValue([grokGrant()]);

    render(<GrantsPanel />);

    await waitFor(() => {
      expect(screen.getByText("$12.50")).toBeInTheDocument();
    });

    grants.mockRejectedValue(new Error("network unreachable"));
    screen.getByRole("button", { name: "Refresh" }).click();

    await waitFor(() => {
      expect(screen.getByText("Grants unavailable")).toBeInTheDocument();
    });
    expect(screen.getByText("$12.50")).toBeInTheDocument();
  });
});
