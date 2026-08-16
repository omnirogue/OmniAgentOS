import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// D-2 (LiveSim LS-004) audit find: `useLiveBoard`'s `hasLoaded` distinguishes
// "never loaded, current fetch failed" from every other combination -- this
// mutable fixture drives that without re-mocking the module per test.
const liveBoardState = vi.hoisted(() => ({
  tasks: [] as unknown[],
  loading: false,
  error: null as string | null,
  hasLoaded: true,
}));

vi.mock("@/features/collab/fixtures", () => ({ USE_FIXTURES: false }));

vi.mock("@/features/collab/hooks", () => ({
  useAgents: () => ({ agents: [] }),
  useLiveBoard: () => ({
    tasks: liveBoardState.tasks,
    loading: liveBoardState.loading,
    error: liveBoardState.error,
    hasLoaded: liveBoardState.hasLoaded,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/features/collab/client", () => ({
  collabApi: {
    fetchCategories: vi.fn().mockResolvedValue([]),
    pauseTask: vi.fn(),
    retryTask: vi.fn(),
    archiveTask: vi.fn(),
  },
}));

vi.mock("@/features/swarm/hooks", () => ({
  useSwarmOverview: () => ({ overview: null }),
}));

vi.mock("@/features/swarm/client", () => ({
  cancelSwarmRun: vi.fn(),
}));

import { TaskPulse } from "./TaskPulse";

describe("Cockpit Live work summary vs. an unknown fetch (D-2)", () => {
  beforeEach(() => {
    liveBoardState.tasks = [];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;
  });

  it("says the fetch could not be checked, never the calm 'nothing running' copy, when the board has never loaded and the fetch failed", () => {
    liveBoardState.error = "Failed to load the board";
    liveBoardState.hasLoaded = false;

    render(<TaskPulse />);

    expect(screen.getByText("Could not check what's running.")).toBeInTheDocument();
    expect(
      screen.queryByText("What you dispatch shows up here and moves as agents work it."),
    ).not.toBeInTheDocument();
  });

  it("still shows the calm 'nothing running' copy when the board genuinely loaded with no live work", () => {
    render(<TaskPulse />);

    expect(
      screen.getByText("What you dispatch shows up here and moves as agents work it."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Could not check what's running.")).not.toBeInTheDocument();
  });
});
