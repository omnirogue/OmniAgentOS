import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The endpoint is live and has a stable `people` list contract. Failures still
 * render nothing so the optional strip never blocks the Team page.
 */

const { scoreboardMock } = vi.hoisted(() => ({ scoreboardMock: vi.fn() }));

vi.mock("./client", () => ({
  teamApi: { scoreboard: scoreboardMock },
  TeamApiError: class TeamApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

// No live EventSource in jsdom — the strip's live-refresh wiring is exercised
// separately (agentsBucket.test.ts covers the bucket logic it feeds); this
// suite is purely about the defensive render contract.
vi.mock("@/lib/useEvents", () => ({ useEvents: () => ({ events: [], lastEvent: null, connected: false, error: false }) }));
vi.mock("@/lib/pollWhenVisible", () => ({ startVisibilityPoll: () => () => {} }));

import { ScoreboardStrip } from "./ScoreboardStrip";

afterEach(() => {
  scoreboardMock.mockReset();
});

describe("ScoreboardStrip — defensive render", () => {
  it("renders nothing while loading", () => {
    scoreboardMock.mockReturnValue(new Promise(() => {})); // never settles
    const { container } = render(<ScoreboardStrip />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the endpoint 404s (not yet built)", async () => {
    scoreboardMock.mockRejectedValue(new Error("404: not found"));
    const { container } = render(<ScoreboardStrip />);
    await waitFor(() => expect(container.querySelector("section")).toBeNull());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the real people-list payload plus the team tile", async () => {
    scoreboardMock.mockResolvedValue({
      people: [
        {
          employee_id: "emp_owner",
          score: 8,
          baseline_points: 2,
          production_x: 4.2,
          pct_to_10x: 42,
        },
        {
          employee_id: "emp_alice",
          score: 0,
          baseline_points: 0,
          production_x: null,
          pct_to_10x: null,
        },
      ],
      team: {
        score: 8,
        baseline_points: 2,
        production_x: 3.1,
        pct_to_10x: 31,
      },
      period: { start: "2026-08-04", end: "2026-08-10" },
      score_version: "v1",
    });
    render(<ScoreboardStrip />);

    await waitFor(() => expect(screen.getByText("4.2×")).toBeInTheDocument());
    expect(screen.getByText("42% to 10×")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("3.1×")).toBeInTheDocument();
    expect(screen.getByText("31% to 10×")).toBeInTheDocument();
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });
});
