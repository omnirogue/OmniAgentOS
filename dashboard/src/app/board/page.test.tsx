import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const boardConnection = vi.hoisted(() => ({ connected: false }));
// D-2 (LiveSim LS-004): a mutable liveBoard fixture so tests can drive the
// hasLoaded/error combinations the metaCount span must distinguish, without
// re-mocking the module per test.
const liveBoardState = vi.hoisted(() => ({
  tasks: [] as unknown[],
  loading: false,
  error: null as string | null,
  hasLoaded: true,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/features/collab/fixtures", () => ({ USE_FIXTURES: false }));

vi.mock("@/features/collab/hooks", () => ({
  useAgents: () => ({ agents: [] }),
  useLiveBoard: () => ({
    tasks: liveBoardState.tasks,
    loading: liveBoardState.loading,
    error: liveBoardState.error,
    hasLoaded: liveBoardState.hasLoaded,
    reconnecting: false,
    connected: boardConnection.connected,
    refresh: vi.fn(),
    archiveTask: vi.fn(),
    archiveTasks: vi.fn(),
  }),
}));

vi.mock("@/features/collab/client", () => ({
  collabApi: {
    fetchCategories: vi.fn().mockResolvedValue([]),
    liveBoard: vi.fn().mockResolvedValue([]),
    pauseTask: vi.fn(),
    retryTask: vi.fn(),
    fetchLedgerClaims: vi.fn().mockResolvedValue([]),
  },
  revealBoardWorkspace: vi.fn(),
}));

vi.mock("@/features/projects/hierarchyHooks", () => ({
  useProjectTree: () => ({ nodes: [] }),
}));

vi.mock("@/features/swarm/hooks", () => ({
  useSwarmOverview: () => ({ overview: null }),
  useSwarmFleet: () => ({ fleet: null }),
}));

import BoardPage from "./page";

describe("Board live-event status", () => {
  beforeEach(() => {
    boardConnection.connected = false;
    liveBoardState.tasks = [];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;
  });

  it("never claims Live before the event stream has connected", () => {
    render(<BoardPage />);

    expect(screen.getByText("Live events reconnecting…")).toBeInTheDocument();
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
  });

  it("reports connected only while the event stream is connected", () => {
    boardConnection.connected = true;
    render(<BoardPage />);

    expect(screen.getByText("Live events connected")).toBeInTheDocument();
  });
});

describe("Board toolbar", () => {
  beforeEach(() => {
    boardConnection.connected = true;
    liveBoardState.tasks = [];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;
  });

  // Measured-dead chrome. Each of these was a control nobody could act on:
  // the view toggles duplicated /orgdims, Lane duplicated the Swarm badge,
  // Company filtered an envelope the card no longer renders, and Bulk
  // reclassify was a maintenance job wearing a toolbar button.
  it.each(["Matrix", "Portfolio", "Kanban", "Bulk reclassify"])(
    "does not offer the %s control",
    (label) => {
      render(<BoardPage />);
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    },
  );

  it.each(["Lane", "Company"])("does not offer the %s filter", (label) => {
    render(<BoardPage />);
    expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
  });

  it("keeps the controls the board still needs", () => {
    render(<BoardPage />);

    expect(screen.getByLabelText("Search titles")).toBeInTheDocument();
    expect(screen.getByLabelText("Discipline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show archived" })).toBeInTheDocument();
  });
});

// D-2 (LiveSim LS-004): the board must never print "0 of 0 cards" when the
// board has never loaded and the fetch failed -- that reads as a confirmed-
// empty board, indistinguishable from a genuinely empty one.
describe("Board card count vs. an unknown fetch (D-2)", () => {
  beforeEach(() => {
    boardConnection.connected = true;
  });

  it("shows an unknown count, never '0 of 0', when the board has never loaded and the fetch failed", () => {
    liveBoardState.tasks = [];
    liveBoardState.loading = false;
    liveBoardState.error = "Failed to load the board";
    liveBoardState.hasLoaded = false;

    render(<BoardPage />);

    expect(
      screen.getByText("Card count unavailable — could not reach the server."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/0 of 0 card/)).not.toBeInTheDocument();
  });

  it("still shows a real '0 of 0 cards' once the board has genuinely loaded empty", () => {
    liveBoardState.tasks = [];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;

    render(<BoardPage />);

    expect(screen.getByText("0 of 0 cards on the board.")).toBeInTheDocument();
  });

  it("keeps showing the real count when a later refresh errors after a successful load (stale beats hidden)", () => {
    liveBoardState.tasks = [
      { id: "t1", title: "Task one", status: "open", updated_at: "2026-08-01T00:00:00Z" },
    ];
    liveBoardState.loading = false;
    liveBoardState.error = "Failed to load the board";
    liveBoardState.hasLoaded = true;

    render(<BoardPage />);

    expect(screen.getByText("1 of 1 card on the board.")).toBeInTheDocument();
    expect(
      screen.queryByText("Card count unavailable — could not reach the server."),
    ).not.toBeInTheDocument();
  });
});

// F09: staleness grouping (runGranularityStaleHideSet) must decide from the
// same pre-observational-filter population as the hidden-count/progress
// readouts -- never the further-narrowed observationalFilteredTasks -- so a
// hidden-but-fresh observational sibling can't make its stale sibling
// disappear as though the whole run had settled.
describe("Board staleness grouping pins its source population (F09)", () => {
  beforeEach(() => {
    boardConnection.connected = true;
  });

  it("does not hide a settled/stale swarm member when its only surviving sibling is a hidden-by-default observational card", () => {
    liveBoardState.tasks = [
      {
        id: "sib-observational",
        title: "[claude · external] youruser · ttys020",
        status: "running",
        updated_at: new Date().toISOString(),
        swarm_run_id: "swr_shared",
      },
      {
        id: "sib-settled",
        title: "Ship the thing",
        status: "done",
        updated_at: "2020-01-01T00:00:00Z",
        swarm_run_id: "swr_shared",
      },
    ];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;

    render(<BoardPage />);

    // Total population is 2; the observational sibling is hidden by default
    // (1 card renders) -- and it is NOT additionally hidden as stale, because
    // the run-level staleness decision saw the full 2-member run (including
    // the fresh observational sibling), not the already-observational-
    // filtered 1-member view.
    expect(screen.getByText("1 of 2 cards on the board.")).toBeInTheDocument();
    expect(screen.getByText("terminal cards: hidden (1)")).toBeInTheDocument();
    // hiddenCount is 0, so StalenessToggle renders null
    // (features/board/InsightsStrip.tsx) -- no toggle at all.
    expect(screen.queryByText(/stale hidden/)).not.toBeInTheDocument();
    expect(screen.queryByText(/stale shown/)).not.toBeInTheDocument();
  });

  it("still hides a swarm run once every member, observational included, is terminal and stale", () => {
    liveBoardState.tasks = [
      {
        id: "sib-observational-stale",
        title: "[claude · external] youruser · ttys021",
        status: "done",
        updated_at: "2020-01-01T00:00:00Z",
        swarm_run_id: "swr_shared_2",
      },
      {
        id: "sib-settled-2",
        title: "Ship the other thing",
        status: "done",
        updated_at: "2020-01-01T00:00:00Z",
        swarm_run_id: "swr_shared_2",
      },
    ];
    liveBoardState.loading = false;
    liveBoardState.error = null;
    liveBoardState.hasLoaded = true;

    render(<BoardPage />);

    // Both members are terminal+stale (even the observational one), so the
    // whole run legitimately hides -- staleness grouping still works, it is
    // just re-anchored to the correct population.
    expect(screen.getByText("0 of 2 cards on the board.")).toBeInTheDocument();
  });
});
