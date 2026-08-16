import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import type { TeamEvidence } from "./types";

const { unattributedMock, liveBoardMock, reattributeMock } = vi.hoisted(() => ({
  unattributedMock: vi.fn(),
  liveBoardMock: vi.fn(),
  reattributeMock: vi.fn(),
}));

vi.mock("./client", () => ({
  teamApi: {
    unattributedEvidence: unattributedMock,
    reattributeEvidence: reattributeMock,
  },
  TeamApiError: class TeamApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

vi.mock("@/features/collab/client", () => ({
  collabApi: { liveBoard: liveBoardMock },
}));

import { UnattributedInbox } from "./UnattributedInbox";

function evidence(overrides: Partial<TeamEvidence> = {}): TeamEvidence {
  return {
    id: "tev_1",
    task_id: null,
    kind: "commit",
    ref: "abc123",
    repo: "OmniAgentOS",
    actor: "bob",
    title: "Fix the thing",
    attribution: "deterministic",
    confidence: 1,
    quality_gate: "pass",
    meta: {},
    created_at: "2026-08-10T00:00:00Z",
    ...overrides,
  };
}

function boardTask(overrides: Partial<LiveBoardTask> = {}): LiveBoardTask {
  return {
    id: "t1",
    title: "Ship the thing",
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "open",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    run_id: null,
    run_state: null,
    run_agent: null,
    run_progress: null,
    run_error: null,
    paused_run: null,
    paused_session: null,
    ref: "TEAM-1",
    ...overrides,
  };
}

afterEach(() => {
  unattributedMock.mockReset();
  liveBoardMock.mockReset();
  reattributeMock.mockReset();
});

describe("UnattributedInbox — reattribution action", () => {
  it("attaching evidence to a chosen task PATCHes with actor 'operator' and refreshes", async () => {
    const user = userEvent.setup();
    unattributedMock.mockResolvedValueOnce([evidence()]).mockResolvedValueOnce([]);
    liveBoardMock.mockResolvedValue([boardTask()]);
    reattributeMock.mockResolvedValue({ ...evidence(), task_id: "t1" });

    render(<UnattributedInbox />);

    await waitFor(() => expect(screen.getByText("Fix the thing")).toBeInTheDocument());

    // Open the task-picker and choose the only option.
    const picker = screen.getByRole("button", { name: /attach fix the thing to a task/i });
    await user.click(picker);
    await user.click(await screen.findByRole("option", { name: /TEAM-1/i }));

    await user.click(screen.getByRole("button", { name: "Attach" }));

    await waitFor(() =>
      expect(reattributeMock).toHaveBeenCalledWith("tev_1", "t1", "operator"),
    );
    // The list refetches after a successful attach — the second (empty)
    // resolved value replaces the attached row.
    await waitFor(() => expect(unattributedMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Fix the thing")).not.toBeInTheDocument());
  });

  it("shows the server's detail string when reattribution is refused", async () => {
    const user = userEvent.setup();
    unattributedMock.mockResolvedValue([evidence()]);
    liveBoardMock.mockResolvedValue([boardTask()]);
    reattributeMock.mockRejectedValue(new Error("400: task does not exist"));

    render(<UnattributedInbox />);
    await waitFor(() => expect(screen.getByText("Fix the thing")).toBeInTheDocument());

    const picker = screen.getByRole("button", { name: /attach fix the thing to a task/i });
    await user.click(picker);
    await user.click(await screen.findByRole("option", { name: /TEAM-1/i }));
    await user.click(screen.getByRole("button", { name: "Attach" }));

    await waitFor(() => expect(screen.getByText("400: task does not exist")).toBeInTheDocument());
  });
});
