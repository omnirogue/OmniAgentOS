import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { columnCounts, columnFor, VISION_COLUMNS } from "@/features/swarm/columns";
import type { SwarmRunGroup } from "@/features/swarm/runAggregate";
import { SwarmRunCard } from "@/features/swarm/SwarmRunCard";
import type { SwarmRunDetail } from "@/features/swarm/types";
import { BoardKanban } from "./BoardKanban";
import boardStyles from "./board.module.css";

const clientMocks = vi.hoisted(() => ({
  fetchSwarmRun: vi.fn(),
}));

vi.mock("@/features/swarm/client", () => ({
  fetchSwarmRun: clientMocks.fetchSwarmRun,
  cancelSwarmRun: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

function task(overrides: Partial<LiveBoardTask> = {}): LiveBoardTask {
  return {
    id: "task-1",
    title: "Task",
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "pending",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-08-01T10:00:00.000Z",
    updated_at: "2026-08-01T10:00:00.000Z",
    run_id: null,
    run_state: null,
    run_agent: null,
    run_progress: null,
    run_error: null,
    paused_run: null,
    paused_session: null,
    ...overrides,
  };
}

function runGroup(live: boolean): SwarmRunGroup {
  const member = task({
    id: live ? "running-member" : "done-member",
    title: live ? "Running member" : "Done member",
    status: live ? "in_progress" : "done",
    // A terminal aggregate must win even when an old member retained a stale
    // live run_state value in the board payload.
    run_state: "running",
  });
  return {
    runId: live ? "swr_running" : "swr_terminal",
    root: null,
    members: [member],
    title: live ? "Running run" : "Terminal run",
    done: live ? 0 : 1,
    total: 1,
    preview: [],
    running: live ? 1 : 0,
    blocked: 0,
    column: live ? "running" : "completed",
  };
}

function swarmTask(runId: string, overrides: Partial<LiveBoardTask>): LiveBoardTask {
  return { ...task(overrides), swarm_run_id: runId } as LiveBoardTask;
}

describe("board column logic", () => {
  it("maps phases and counts filtered tasks, including completions today", () => {
    const now = new Date("2026-08-03T12:00:00.000Z");
    const tasks = [
      task({ id: "ready", status: "open" }),
      task({ id: "testing", status: "in_progress" }),
      task({ id: "done-today", status: "done", updated_at: "2026-08-03T10:00:00.000Z" }),
      task({ id: "done-before", status: "done", updated_at: "2026-08-02T10:00:00.000Z" }),
    ];
    const overlay = new Map([["testing", "testing"]]);

    expect(columnFor(tasks[1], overlay)).toBe("testing");
    expect(columnCounts(tasks, overlay, now)).toMatchObject({
      ready: { total: 1, completedToday: 0 },
      testing: { total: 1, completedToday: 0 },
      completed: { total: 2, completedToday: 1 },
    });
  });

  it("shows chip totals equal to rendered cards when swarm runs are aggregated", () => {
    clientMocks.fetchSwarmRun.mockResolvedValue(null);
    const tasks = [
      swarmTask("swr_live", {
        id: "run-root",
        title: "Swarm: Live goal",
        status: "in_progress",
      }),
      swarmTask("swr_live", {
        id: "run-active-member",
        title: "Active member",
        status: "in_progress",
      }),
      swarmTask("swr_live", {
        id: "run-done-member",
        title: "Done member",
        status: "done",
      }),
      task({ id: "solo-running", title: "Solo running", status: "in_progress" }),
      task({ id: "solo-ready", title: "Solo ready", status: "open" }),
      task({ id: "solo-done", title: "Solo done", status: "done" }),
    ];

    render(<BoardKanban tasks={tasks} aggregateSwarms />);

    for (const column of VISION_COLUMNS) {
      const region = screen.getByRole("region", { name: column.label });
      const renderedCards =
        within(region).queryAllByRole("button", { name: /^Open swarm run / }).length
        + within(region).queryAllByRole("link", { name: /^Open details for / }).length;
      const totalChip = region.querySelector(`.${boardStyles.kcount}`);
      expect(totalChip, `${column.label} count chip`).toHaveTextContent(String(renderedCards));
    }

    expect(screen.getByRole("region", { name: "Running" }).querySelector(`.${boardStyles.kcount}`)).toHaveTextContent("2");
    expect(screen.getByRole("region", { name: "Completed" }).querySelector(`.${boardStyles.kcount}`)).toHaveTextContent("1");
  });

  it("lets the server's Needs Response band move a card into Needs you", () => {
    // The card's own status still says in_progress — the SERVER owns the strict
    // predicate, so its band wins the column.
    const parked = task({ id: "parked", title: "Parked card", status: "in_progress" });

    render(<BoardKanban tasks={[parked]} needsResponseIds={new Set(["parked"])} />);

    const needsYou = screen.getByRole("region", { name: "Needs you" });
    expect(within(needsYou).getByRole("link", { name: "Open details for Parked card" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Running" })).queryByRole("link", {
        name: "Open details for Parked card",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("per-column render cap", () => {
  const many = (count: number) =>
    Array.from({ length: count }, (_, index) =>
      task({ id: `ready-${index}`, title: `Ready ${index}`, status: "open" }),
    );

  it("renders only the cap, and says how many more there are", () => {
    render(<BoardKanban tasks={many(10)} renderCap={4} />);

    const ready = screen.getByRole("region", { name: "Ready" });
    expect(within(ready).queryAllByRole("link", { name: /^Open details for / })).toHaveLength(4);
    // The head count is the TRUTH about the column, not what is mounted.
    expect(ready.querySelector(`.${boardStyles.kcount}`)).toHaveTextContent("10");
    expect(within(ready).getByRole("button", { name: "Show 4 more" })).toBeInTheDocument();
  });

  it("reveals one more page per click and drops the control at the end", () => {
    render(<BoardKanban tasks={many(6)} renderCap={4} />);

    const ready = screen.getByRole("region", { name: "Ready" });
    // Two left, so the control offers exactly two — never a number it cannot honour.
    fireEvent.click(within(ready).getByRole("button", { name: "Show 2 more" }));

    expect(within(ready).queryAllByRole("link", { name: /^Open details for / })).toHaveLength(6);
    expect(within(ready).queryByRole("button", { name: /^Show / })).not.toBeInTheDocument();
  });

  it("does not cap a column that fits", () => {
    render(<BoardKanban tasks={many(3)} renderCap={4} />);

    const ready = screen.getByRole("region", { name: "Ready" });
    expect(within(ready).queryAllByRole("link", { name: /^Open details for / })).toHaveLength(3);
    expect(within(ready).queryByRole("button", { name: /^Show / })).not.toBeInTheDocument();
  });
});

describe("blocked cards say why they stopped", () => {
  it("renders the server's blocked_reason as one line, full text in the title", () => {
    const blocked = task({
      id: "blocked-1",
      title: "Stuck card",
      status: "blocked",
      blocked_reason: {
        reason: "context_exhausted",
        detail: "the model ran out of context on attempt 4",
        source: "longhaul",
        at: "2026-08-03T10:00:00.000Z",
      },
    });

    render(<BoardKanban tasks={[blocked]} />);

    const hint = screen.getByText(/context exhausted — the model ran out of context on attempt 4/);
    expect(hint).toBeInTheDocument();
    expect(hint.closest("p")).toHaveAttribute(
      "title",
      "context exhausted — the model ran out of context on attempt 4 (longhaul)",
    );
  });

  it("falls back to run_error when the control plane ships no blocked_reason", () => {
    const blocked = task({
      id: "blocked-2",
      title: "Old stuck card",
      status: "blocked",
      run_error: "worktree is dirty",
    });

    render(<BoardKanban tasks={[blocked]} />);

    expect(screen.getByText("worktree is dirty")).toBeInTheDocument();
  });

  it("never renders a reason on a card that is not blocked", () => {
    // The server contract says blocked_reason only names a FAILURE, but a card
    // that moved on must not keep showing one.
    const moved = task({
      id: "moved",
      title: "Moved on",
      status: "in_progress",
      blocked_reason: { reason: "timeout", detail: "old failure", source: "swarm", at: null },
    });

    render(<BoardKanban tasks={[moved]} />);

    expect(screen.queryByText(/old failure/)).not.toBeInTheDocument();
  });
});

describe("per-card actions", () => {
  const handlers = () => ({
    onOpenFiles: vi.fn(),
    onArchive: vi.fn(),
    onPause: vi.fn(),
    onOpenTerminal: vi.fn(),
  });

  it("shows at most three chips and keeps the rest behind More", () => {
    const props = handlers();
    // pause + terminal + files + archive = four operations on one card.
    const live = task({ id: "live", title: "Live card", status: "in_progress", run_id: "run-1" });

    render(<BoardKanban tasks={[live]} {...props} />);

    expect(screen.getByRole("button", { name: "Pause Live card" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open terminal for Live card" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More actions for Live card" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Open files for Live card" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "More actions for Live card" }));

    // Nothing was lost — every operation is still reachable.
    expect(screen.getByRole("menuitem", { name: "Archive (stops work)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open files for Live card" }));
    expect(props.onOpenFiles).toHaveBeenCalledWith("live");
    // Acting closes the menu.
    expect(screen.queryByRole("menuitem", { name: "Archive (stops work)" })).not.toBeInTheDocument();
  });

  it("renders every action inline when three or fewer apply", () => {
    const props = handlers();
    const done = task({ id: "done", title: "Done card", status: "done" });

    render(<BoardKanban tasks={[done]} {...props} />);

    expect(screen.getByRole("button", { name: "View deliverables for Done card" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^More actions/ })).not.toBeInTheDocument();
  });
});

describe("SwarmRunCard detail loading", () => {
  beforeEach(() => {
    clientMocks.fetchSwarmRun.mockReset();
    clientMocks.fetchSwarmRun.mockResolvedValue(null);
  });

  it("does not fetch terminal run detail on mount", () => {
    render(<SwarmRunCard group={runGroup(false)} onOpen={vi.fn()} />);

    expect(clientMocks.fetchSwarmRun).not.toHaveBeenCalled();
  });

  it("fetches running run detail on mount", async () => {
    clientMocks.fetchSwarmRun.mockResolvedValue({
      run: { id: "swr_running", status: "running" },
      tasks: [],
      attempts: {
        "running-member": [{
          id: "attempt-1",
          board_task_id: "running-member",
          provider: "openai",
          model: "gpt-5",
          session_id: "session-1",
        }],
      },
    });
    render(<SwarmRunCard group={runGroup(true)} onOpen={vi.fn()} />);

    await waitFor(() => expect(clientMocks.fetchSwarmRun).toHaveBeenCalledTimes(1));
    expect(clientMocks.fetchSwarmRun).toHaveBeenCalledWith("swr_running");
    expect(await screen.findByText("openai/gpt-5")).toBeInTheDocument();
  });

  it("opens a terminal run synchronously, then applies lazy detail", async () => {
    let resolveDetail: (detail: SwarmRunDetail | null) => void = () => undefined;
    const detailPromise = new Promise<SwarmRunDetail | null>((resolve) => {
      resolveDetail = resolve;
    });
    const detail: SwarmRunDetail = {
      run: { id: "swr_terminal", status: "completed" },
      tasks: [],
      attempts: {},
    };
    clientMocks.fetchSwarmRun.mockReturnValue(detailPromise);
    const onOpen = vi.fn();
    const onOpenUpdate = vi.fn();
    render(
      <SwarmRunCard
        group={runGroup(false)}
        onOpen={onOpen}
        onOpenUpdate={onOpenUpdate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand swarm run Terminal run" }));

    expect(onOpen).toHaveBeenCalledWith(runGroup(false), null);
    expect(clientMocks.fetchSwarmRun).toHaveBeenCalledWith("swr_terminal");
    expect(onOpenUpdate).not.toHaveBeenCalled();

    resolveDetail(detail);
    await waitFor(() => expect(onOpenUpdate).toHaveBeenCalledWith(runGroup(false), detail));
  });

  it("reports unavailable terminal detail after a lazy fetch fails", async () => {
    const onOpen = vi.fn();
    const onOpenUnavailable = vi.fn();
    clientMocks.fetchSwarmRun.mockResolvedValue(null);
    render(
      <SwarmRunCard
        group={runGroup(false)}
        onOpen={onOpen}
        onOpenUnavailable={onOpenUnavailable}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand swarm run Terminal run" }));

    expect(onOpen).toHaveBeenCalledWith(runGroup(false), null);
    await waitFor(() => expect(onOpenUnavailable).toHaveBeenCalledWith(runGroup(false)));
  });
});
