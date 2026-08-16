import { fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Approval } from "../../lib/contracts";
import { ApprovalsTab } from "./ApprovalsTab";
import { useApprovalsFeed, type ApprovalsFeed } from "./useApprovalsFeed";

const approvalsMock = vi.fn();

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { ...actual.api, approvals: (state: string) => approvalsMock(state) } };
});

vi.mock("../../lib/useEventChannel", () => ({
  useEventChannel: () => ({ lastEvent: null, connected: true, error: false }),
}));

vi.mock("../../lib/pollWhenVisible", () => ({ startVisibilityPoll: () => () => {} }));

vi.mock("../projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../projects")>();
  return { ...actual, useProjectContext: () => ({ activeProjectId: "" }) };
});

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "appr_1",
    run_id: null,
    session_id: "sess_1",
    task_id: null,
    step_seq: null,
    action_class: "external_reversible",
    proposed_action: "post_tweet",
    risk: "medium",
    evidence: "looks fine",
    state: "pending",
    decided_by: null,
    decision_note: null,
    decided_at: null,
    expires_at: null,
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function feed(overrides: Partial<ApprovalsFeed> = {}): ApprovalsFeed {
  return {
    pending: [approval()],
    decided: [],
    loading: false,
    error: null,
    connected: true,
    streamError: false,
    refresh: vi.fn(),
    decide: vi.fn().mockResolvedValue(approval({ state: "approved" })),
    ...overrides,
  };
}

describe("ApprovalsTab — decision failure is surfaced, not swallowed", () => {
  it("shows an inline error and keeps the dialog + note when submitDecision rejects", async () => {
    const decide = vi.fn().mockRejectedValue(new Error("network blip"));
    render(<ApprovalsTab feed={feed({ decide })} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByText("Approve action")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "checked it, looks fine" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    // Error text is visible…
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("network blip"));
    // …the dialog is still open…
    expect(screen.getByText("Approve action")).toBeInTheDocument();
    // …and the operator's note was not cleared.
    expect(screen.getByLabelText("Note")).toHaveValue("checked it, looks fine");
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it("falls back to a generic message when the rejection isn't an Error instance", async () => {
    const decide = vi.fn().mockRejectedValue("boom");
    render(<ApprovalsTab feed={feed({ decide })} />);

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Could not save this decision."),
    );
    expect(screen.getByText("Reject action")).toBeInTheDocument();
  });

  it("clears the inline error and closes on a successful decision", async () => {
    const decide = vi.fn().mockResolvedValue(approval({ state: "approved" }));
    render(<ApprovalsTab feed={feed({ decide })} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(screen.queryByText("Approve action")).not.toBeInTheDocument());
  });

  it("clears a stale error when the dialog is reopened for a fresh attempt", async () => {
    const decide = vi.fn().mockRejectedValueOnce(new Error("first try failed"));
    render(<ApprovalsTab feed={feed({ decide })} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(screen.getByText("first try failed")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.queryByText("first try failed")).not.toBeInTheDocument();
  });
});

describe("ApprovalsTab — decided approvals", () => {
  it("shows expired approvals with a distinct treatment while preserving approved and rejected treatments", () => {
    const expired = approval({
      id: "appr_expired",
      state: "expired",
      proposed_action: "expired_action",
      decision_note: "No response before the deadline.",
      decided_at: "2026-08-04T00:00:00Z",
    });
    const approved = approval({
      id: "appr_approved",
      state: "approved",
      proposed_action: "approved_action",
      decision_note: "Approved after review.",
      decided_at: "2026-08-03T00:00:00Z",
    });
    const rejected = approval({
      id: "appr_rejected",
      state: "rejected",
      proposed_action: "rejected_action",
      decision_note: "Rejected after review.",
      decided_at: "2026-08-02T00:00:00Z",
    });

    render(<ApprovalsTab feed={feed({ decided: [expired, approved, rejected] })} />);

    expect(screen.getAllByText("expired_action").length).toBeGreaterThan(0);
    expect(screen.getByText("No response before the deadline.")).toBeInTheDocument();
    expect(screen.getAllByText("approved_action").length).toBeGreaterThan(0);
    expect(screen.getAllByText("rejected_action").length).toBeGreaterThan(0);

    const expiredBadge = screen.getByText("expired");
    const approvedBadge = screen.getByText("approved");
    const rejectedBadge = screen.getByText("rejected");
    expect(expiredBadge).toHaveClass("ds-badge--warn");
    expect(expiredBadge).not.toHaveClass("ds-badge--completed");
    expect(expiredBadge).not.toHaveClass("ds-badge--failed");
    expect(approvedBadge).toHaveClass("ds-badge--completed");
    expect(rejectedBadge).toHaveClass("ds-badge--failed");
  });

  it("renders the decided empty state when there are no recent decisions", () => {
    render(<ApprovalsTab feed={feed({ decided: [] })} />);

    expect(screen.getByText("No recent decisions.")).toBeInTheDocument();
  });
});

describe("useApprovalsFeed — decided approvals", () => {
  it("loads expired approvals and includes them in the folded decided rows", async () => {
    const expired = approval({ id: "appr_expired", state: "expired", decided_at: "2026-08-04T00:00:00Z" });
    approvalsMock.mockImplementation((state: string) => Promise.resolve(state === "expired" ? [expired] : []));

    const { result } = renderHook(() => useApprovalsFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(approvalsMock).toHaveBeenCalledWith("expired");
    expect(result.current.decided).toEqual(expect.arrayContaining([expired]));
  });

  it("keeps human decisions visible when expiry sweeps exceed the expired-row cap", async () => {
    const approved = approval({ id: "appr_approved", state: "approved", decided_at: "2026-07-01T00:00:00Z" });
    const expired = Array.from({ length: 25 }, (_, index) =>
      approval({
        id: `appr_expired_${index}`,
        state: "expired",
        decided_at: `2026-08-${String(25 - index).padStart(2, "0")}T00:00:00Z`,
      }),
    );
    approvalsMock.mockImplementation((state: string) => Promise.resolve(state === "approved" ? [approved] : state === "expired" ? expired : []));

    const { result } = renderHook(() => useApprovalsFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.decided).toEqual(expect.arrayContaining([approved]));
    expect(result.current.decided).toHaveLength(21);
  });

  it("uses expiry time for expired approvals without a decision time", async () => {
    const approved = approval({ id: "appr_approved", state: "approved", decided_at: "2026-08-02T12:00:00Z" });
    const expired = approval({
      id: "appr_expired",
      state: "expired",
      decided_at: null,
      expires_at: "2026-08-02T12:28:00Z",
      created_at: "2026-07-30T12:00:00Z",
    });
    approvalsMock.mockImplementation((state: string) => Promise.resolve(state === "approved" ? [approved] : state === "expired" ? [expired] : []));

    const { result } = renderHook(() => useApprovalsFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.decided.map((row) => row.id)).toEqual([expired.id, approved.id]);
  });
});

// D-2 (LiveSim LS-004): a fetch failure must never render as a confident
// "0 pending" + an empty table -- that is indistinguishable from a genuinely
// clear queue, and on an approvals surface that gap hides a real backlog.
describe("ApprovalsTab — unknown count vs. a real zero (D-2)", () => {
  it("shows an unknown state, not '0 pending' or an empty table, when the list has never loaded and the fetch failed", () => {
    render(
      <ApprovalsTab
        feed={feed({ pending: [], decided: [], error: "network unreachable" })}
      />,
    );

    // The count reads as unknown, never a confident zero.
    expect(screen.getByText("Pending (—)")).toBeInTheDocument();
    expect(screen.queryByText("Pending (0)")).not.toBeInTheDocument();
    // Neither the "confirmed empty" copy nor an empty table renders — the
    // ErrorState banner above is the single source of truth.
    expect(screen.queryByText("No approvals pending.")).not.toBeInTheDocument();
    expect(screen.queryByText("No recent decisions.")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Pending approvals" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Recently decided approvals" })).not.toBeInTheDocument();
  });

  it("still shows a real 0 pending and 'No approvals pending' when the fetch genuinely succeeded empty", () => {
    render(<ApprovalsTab feed={feed({ pending: [], decided: [], error: null })} />);

    expect(screen.getByText("Pending (0)")).toBeInTheDocument();
    expect(screen.getByText("No approvals pending.")).toBeInTheDocument();
    expect(screen.getByText("No recent decisions.")).toBeInTheDocument();
  });

  it("keeps showing known pending rows and the real count when a later refresh errors (stale beats hidden)", () => {
    render(
      <ApprovalsTab
        feed={feed({ pending: [approval()], decided: [], error: "network unreachable" })}
      />,
    );

    expect(screen.getByText("Pending (1)")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Pending approvals" })).toBeInTheDocument();
  });
});
