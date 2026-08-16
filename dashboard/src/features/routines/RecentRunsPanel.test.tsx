import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RecentRunItem } from "./types";
import { RecentRunsPanel } from "./RecentRunsPanel";

/**
 * ISSUE-8 (Sol review, seam 2): `cost_usd` is `null` when a run's true cost
 * was never reported (omniagentos/scheduler/store.py, nullable since
 * migration 120) — genuinely unknown, never a manufactured "$0.00".
 */

function run(overrides: Partial<RecentRunItem> = {}): RecentRunItem {
  return {
    routine_id: "rtn_1",
    routine_name: "nightly-lint-fix",
    run_id: "run_1",
    gate_passed: true,
    accepted: true,
    cost_usd: 1.25,
    finished_at: "2026-01-01T09:00:00Z",
    ...overrides,
  };
}

describe("RecentRunsPanel", () => {
  it('renders "unknown" — never $0.00 — for a null cost_usd', () => {
    render(
      <RecentRunsPanel
        runs={[run({ cost_usd: null })]}
        loading={false}
        error={null}
        refresh={vi.fn()}
      />,
    );

    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it("renders a known cost as a dollar figure", () => {
    render(
      <RecentRunsPanel
        runs={[run({ cost_usd: 1.25 })]}
        loading={false}
        error={null}
        refresh={vi.fn()}
      />,
    );

    expect(screen.getByText("$1.25")).toBeInTheDocument();
  });

  it("still renders $0.00 for a genuinely, exactly zero known cost", () => {
    render(
      <RecentRunsPanel
        runs={[run({ cost_usd: 0 })]}
        loading={false}
        error={null}
        refresh={vi.fn()}
      />,
    );

    expect(screen.getByText("$0.00")).toBeInTheDocument();
  });
});
