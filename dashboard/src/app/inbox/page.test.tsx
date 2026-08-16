import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../../design";
import type { Approval } from "../../lib/contracts";
import type { Alert, Briefing } from "../../features/steward";

const useApprovalsFeedMock = vi.fn();
vi.mock("../../features/inbox", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../features/inbox")>();
  return { ...actual, useApprovalsFeed: () => useApprovalsFeedMock() };
});

const useAlertsMock = vi.fn();
const useBriefingMock = vi.fn();
vi.mock("../../features/steward", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../features/steward")>();
  return {
    ...actual,
    useAlerts: (...args: unknown[]) => useAlertsMock(...args),
    useBriefing: () => useBriefingMock(),
  };
});

vi.mock("../../features/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../features/projects")>();
  return { ...actual, useProjectContext: () => ({ activeProject: null, activeProjectId: "" }) };
});

// Decisions tab hook is exercised in its own tests; stub it here so the Inbox
// shell test does not hit the network via the real useDecisions fetch.
const useDecisionsMock = vi.fn();
vi.mock("../../features/decisions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../features/decisions")>();
  return { ...actual, useDecisions: () => useDecisionsMock() };
});

import ApprovalsPage from "./page";

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

function alert(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 1,
    rule: "roas_floor",
    severity: "high",
    title: "ROAS dropped below floor",
    body: "ROAS is at 1.2, floor is 1.5.",
    evidence: {},
    cooldown_key: null,
    state: "open",
    created_at: "2026-08-01T00:00:00Z",
    acked_at: null,
    ...overrides,
  };
}

function briefing(overrides: Partial<Briefing> = {}): Briefing {
  return {
    id: "brf_1",
    briefing_date: "2026-08-01",
    vault_path: null,
    summary: { subject: "Daily briefing — 2026-08-01", headline: "1 open alert, 1 open suggestion", sections: [], composed_by: "deterministic" },
    deliveries: [],
    voice_artifact_id: null,
    acked_at: null,
    ...overrides,
  };
}

function mockFeeds() {
  useApprovalsFeedMock.mockReturnValue({
    pending: [approval()],
    decided: [],
    loading: false,
    error: null,
    connected: true,
    streamError: false,
    refresh: vi.fn(),
    decide: vi.fn().mockResolvedValue(approval({ state: "approved" })),
  });
  useAlertsMock.mockReturnValue({
    alerts: [alert()],
    loading: false,
    error: null,
    refresh: vi.fn(),
    ack: vi.fn().mockResolvedValue(alert({ state: "acked", acked_at: "2026-08-01T01:00:00Z" })),
  });
  useDecisionsMock.mockReturnValue({
    decisions: [],
    groups: { urgent: [], needsOwner: [], maybe: [], snoozed: [] },
    badgeCount: 0,
    loading: false,
    error: null,
    deciding: false,
    refresh: vi.fn(),
    decide: vi.fn(),
  });
  useBriefingMock.mockReturnValue({
    briefing: briefing(),
    // Newest first, mirroring the backend's ORDER BY briefing_date DESC.
    history: [briefing(), briefing({ id: "brf_0", briefing_date: "2026-07-31", acked_at: "2026-07-31T09:00:00Z" })],
    loading: false,
    error: null,
    generating: false,
    refresh: vi.fn(),
    generate: vi.fn(),
    ack: vi.fn().mockResolvedValue(briefing({ acked_at: "2026-08-01T02:00:00Z" })),
  });
}

function renderPage() {
  return render(
    <ToastProvider>
      <ApprovalsPage />
    </ToastProvider>,
  );
}

describe("Inbox (/inbox tabbed shell)", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders all four tabs with cheap pending/unread badges and defaults to Approvals", () => {
    mockFeeds();
    renderPage();

    expect(screen.getByRole("heading", { name: "Inbox" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Approvals/ })).toHaveTextContent("1");
    // Decisions tab is present, inserted after Approvals (which stays default).
    expect(screen.getByRole("tab", { name: /Decisions/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Alerts/ })).toHaveTextContent("1");
    // No Suggestions tab (F08: removed as dormant).
    expect(screen.queryByRole("tab", { name: /Suggestions/ })).not.toBeInTheDocument();
    // 1 of the 2 briefings is unacked.
    expect(screen.getByRole("tab", { name: /Briefings/ })).toHaveTextContent("1");

    // Approvals is the default tab and keeps its full decision workflow.
    // ("post_tweet" appears twice: proposed-action column + origin preview.)
    expect(screen.getAllByText("post_tweet").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("opens the decide dialog from the default Approvals tab", () => {
    mockFeeds();
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByText("Approve action")).toBeInTheDocument();
  });

  it("switches to the Alerts tab and shows the open alert with an Ack action", () => {
    mockFeeds();
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    expect(screen.getByText("ROAS dropped below floor")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ack" })).toBeInTheDocument();
  });

  it("switches to the Briefings tab and shows newest-first history, ack only on the unacked row", async () => {
    mockFeeds();
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /Briefings/ }));
    const rows = screen.getAllByText(/Daily briefing/);
    expect(rows).toHaveLength(2);
    // The newest (unacked) row exposes Ack; the older, already-acked one shows a badge instead.
    expect(screen.getByRole("button", { name: "Ack" })).toBeInTheDocument();
    expect(screen.getByText("Acked")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ack" }));
    await waitFor(() => expect(useBriefingMock().ack).toHaveBeenCalled());
  });
});
