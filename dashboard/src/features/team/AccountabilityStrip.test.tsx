import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * All FOUR states must render DISTINCT DOM (Sol@high cross-review round 2,
 * 2026-08-14): "loading" (nothing — genuinely unknown yet), "unavailable"
 * (an explicit ⚠ warning — a failure), a genuinely empty successful fetch
 * (a muted "No accountability data yet" line — a confirmed answer, not a
 * guess), and a populated fetch (the real per-person cards). Collapsing any
 * two of these into the same rendering is exactly the favourable-absence bug
 * this suite exists to catch — silence must never stand in for "confirmed
 * empty" OR for "the call failed".
 */

const { accountabilityMock } = vi.hoisted(() => ({ accountabilityMock: vi.fn() }));

vi.mock("./client", () => ({
  teamApi: { accountability: accountabilityMock },
  TeamApiError: class TeamApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

vi.mock("@/lib/useEvents", () => ({ useEvents: () => ({ events: [], lastEvent: null, connected: false, error: false }) }));
vi.mock("@/lib/pollWhenVisible", () => ({ startVisibilityPoll: () => () => {} }));

import { AccountabilityStrip } from "./AccountabilityStrip";

afterEach(() => {
  accountabilityMock.mockReset();
});

describe("AccountabilityStrip — defensive render", () => {
  it("state 1/4 — loading: renders nothing (genuinely unknown yet)", () => {
    accountabilityMock.mockReturnValue(new Promise(() => {})); // never settles
    const { container } = render(<AccountabilityStrip />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("⚠ Accountability unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText("No accountability data yet")).not.toBeInTheDocument();
  });

  it("state 2/4 — unavailable: renders an explicit warning, not nothing", async () => {
    accountabilityMock.mockRejectedValue(new Error("500: internal"));
    const { container } = render(<AccountabilityStrip />);
    await waitFor(() => expect(screen.getByText("⚠ Accountability unavailable")).toBeInTheDocument());
    // Distinct from BOTH other non-card states: not empty DOM, and not the
    // "confirmed empty" line.
    expect(container).not.toBeEmptyDOMElement();
    expect(screen.queryByText("No accountability data yet")).not.toBeInTheDocument();
  });

  it("state 3/4 — confirmed empty: renders a muted line, not silence and not the warning", async () => {
    accountabilityMock.mockResolvedValue({ day: "2026-08-14", people: [] });
    const { container } = render(<AccountabilityStrip />);
    await waitFor(() => expect(screen.getByText("No accountability data yet")).toBeInTheDocument());
    // Distinct from "loading" (not empty DOM) and from "unavailable" (no
    // warning badge) — "successfully learned there is nothing to report" is
    // neither "still unknown" nor "the call failed".
    expect(container).not.toBeEmptyDOMElement();
    expect(screen.queryByText("⚠ Accountability unavailable")).not.toBeInTheDocument();
  });

  it("state 4/4 — populated: renders the real per-person payload — commitments, tri-state, blocked/evidence", async () => {
    accountabilityMock.mockResolvedValue({
      day: "2026-08-14",
      people: [
        {
          employee_id: "emp_bob",
          name: "Bob",
          commitments: [
            {
              id: "tcm_1",
              day: "2026-08-14",
              employee_id: "emp_bob",
              task_id: "btk_1",
              kind: "task",
              title: "Ship the thing",
              expected_outcome: "it ships",
              status: "delivered",
              source: "auto",
              carried_from: null,
              resolved_at: "2026-08-14T09:00:00Z",
              resolved_by: "system",
              resolution_note: "",
              created_at: "2026-08-14T06:55:00Z",
              updated_at: "2026-08-14T09:00:00Z",
            },
          ],
          improvement_of_day: null,
          counts: {},
          done_today: [
            {
              id: "btk_1",
              ref: "GH-7",
              title: "Ship the thing (card)",
              size: "M",
              completion_state: "failed_verification",
              automation_maturity: "assisted",
              automation_note: null,
              verification_failed_reason: "no tests",
              evidence: [],
            },
          ],
          blocked: [{ id: "btk_2", ref: "GH-8", title: "Stuck", blocked_reason: "waiting on review" }],
          overdue: 1,
          learning_captures: 0,
          evidence_today: 3,
          points_pace: { points: 4, floor: 2, prorated_target: 5, on_pace: false },
        },
      ],
    });
    render(<AccountabilityStrip />);

    await waitFor(() => expect(screen.getByText("Bob")).toBeInTheDocument());
    expect(screen.getByText("Ship the thing")).toBeInTheDocument();
    expect(screen.getByText("1 blocked")).toBeInTheDocument();
    expect(screen.getByText("1 overdue")).toBeInTheDocument();
    expect(screen.getByText("3 evidence today")).toBeInTheDocument();
    // Distinct from the other three states: no warning, no "confirmed empty" line.
    expect(screen.queryByText("⚠ Accountability unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText("No accountability data yet")).not.toBeInTheDocument();
  });
});
