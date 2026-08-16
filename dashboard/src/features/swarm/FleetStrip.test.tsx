import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { deriveBoardRuns, mergeFleet } from "./columns";
import { FleetStrip } from "./FleetStrip";
import type { SwarmBoardTask, SwarmFleet, SwarmRunStatus } from "./types";

function card(
  runId: string,
  index: number,
  status: LiveBoardTask["status"],
): SwarmBoardTask {
  return {
    id: `task-${index}`,
    title: `Task ${index}`,
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status,
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    run_id: null,
    run_state: null,
    run_agent: null,
    run_progress: null,
    run_error: null,
    paused_run: null,
    paused_session: null,
    swarm_run_id: runId,
  };
}

function renderRun(runStatus: SwarmRunStatus, taskStatus: LiveBoardTask["status"]) {
  const runId = "swr_3d9cdc18";
  const tasks = Array.from({ length: 12 }, (_, index) => card(runId, index, taskStatus));
  const fleet: SwarmFleet = {
    runs: [{ id: runId, status: runStatus }],
  };
  const runs = mergeFleet(deriveBoardRuns(tasks), fleet);
  const view = render(
    <FleetStrip
      runs={runs}
      utilization={null}
      loading={false}
      onOpenRun={vi.fn()}
    />,
  );
  const runCard = screen.getByText("3d9cdc18").closest(".ds-card");
  expect(runCard).not.toBeNull();
  return { ...view, runCard: runCard as HTMLElement };
}

describe("FleetStrip terminal-run truth", () => {
  it("renders an all-cancelled run as cancelled and zero percent, never completed", () => {
    const { runCard } = renderRun("cancelled", "cancelled");
    const cardView = within(runCard);

    expect(cardView.getByText("cancelled")).toBeInTheDocument();
    expect(cardView.queryByText(/Completed:\s*12/i)).not.toBeInTheDocument();
    expect(cardView.getByText("Cancelled: 12")).toBeInTheDocument();
    expect(cardView.getByText("0/12")).toBeInTheDocument();

    const progress = cardView.getByRole("progressbar", { name: "Run progress" });
    expect(progress).toHaveAttribute("aria-valuemin", "0");
    expect(progress).toHaveAttribute("aria-valuemax", "100");
    expect(progress).toHaveAttribute("aria-valuenow", "0");
    expect(progress.firstElementChild).toHaveStyle({ width: "0%" });
  });

  it("still renders a genuinely completed run as completed at 100 percent", () => {
    const { runCard } = renderRun("completed", "done");
    const cardView = within(runCard);

    expect(cardView.getByText("completed")).toBeInTheDocument();
    expect(cardView.getByText("Completed: 12")).toBeInTheDocument();
    expect(cardView.getByText("12/12")).toBeInTheDocument();

    const progress = cardView.getByRole("progressbar", { name: "Run progress" });
    expect(progress).toHaveAttribute("aria-valuenow", "100");
    expect(progress.firstElementChild).toHaveStyle({ width: "100%" });
  });
});
