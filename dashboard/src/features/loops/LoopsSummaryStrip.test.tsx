import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SystemJobsSnapshot } from "@/features/routines/systemJobs";
import { summarizeHealth } from "./filterSort";
import { LoopsSummaryStrip } from "./LoopsSummaryStrip";

function snapshot(overrides: Partial<SystemJobsSnapshot> = {}): SystemJobsSnapshot {
  return {
    generated_at: "2026-08-15T10:00:00Z",
    launchctl: { available: true, reason: "" },
    counts: { total: 1, loaded: 1, failing: 0, stale: 0 },
    jobs: [],
    ...overrides,
  };
}

describe("LoopsSummaryStrip remote_probe absence", () => {
  it("treats a MISSING remote_probe the same as available:false — never renders as fine", () => {
    // The live API predates the backend package that populates this field:
    // absence is the common case today, so it must degrade, not go silent.
    const snap = snapshot(); // no remote_probe key at all
    render(<LoopsSummaryStrip counts={summarizeHealth(snap.jobs)} snapshot={snap} onRetry={vi.fn()} />);
    expect(screen.getByText(/Remote probe unavailable/)).toBeInTheDocument();
  });

  it("does not show the remote-probe note when explicitly available", () => {
    const snap = snapshot({ remote_probe: { available: true, reason: "", probed_at: "2026-08-15T10:00:00Z" } });
    render(<LoopsSummaryStrip counts={summarizeHealth(snap.jobs)} snapshot={snap} onRetry={vi.fn()} />);
    expect(screen.queryByText(/Remote probe unavailable/)).not.toBeInTheDocument();
  });

  it("wires the degraded note's Retry button to onRetry", () => {
    const onRetry = vi.fn();
    const snap = snapshot({ launchctl: { available: false, reason: "sandboxed" } });
    render(<LoopsSummaryStrip counts={summarizeHealth(snap.jobs)} snapshot={snap} onRetry={onRetry} />);
    const retryButtons = screen.getAllByRole("button", { name: "Retry" });
    expect(retryButtons.length).toBeGreaterThan(0);
    fireEvent.click(retryButtons[0]!);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
