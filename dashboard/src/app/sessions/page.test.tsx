import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "../../lib/contracts";

// D-2 (LiveSim LS-004) audit find: `useSessions` never clears `sessions` on a
// failed refresh (see features/sessions/hooks.ts), so an empty array only
// means "confirmed empty" when `error` is unset. This mutable fixture lets
// each test drive that combination without re-mocking the module.
const sessionsState = vi.hoisted(() => ({
  sessions: [] as Session[],
  loading: false,
  error: null as string | null,
  unauthorized: false,
  connected: true,
}));

vi.mock("../../features/sessions/fixtures", () => ({
  USE_FIXTURES: false,
  decideFixtureApproval: vi.fn(),
  killFixtureSession: vi.fn(),
}));

vi.mock("../../features/sessions/hooks", () => ({
  useSessions: () => ({
    sessions: sessionsState.sessions,
    approvals: [],
    loading: sessionsState.loading,
    error: sessionsState.error,
    unauthorized: sessionsState.unauthorized,
    refresh: vi.fn(),
    connected: sessionsState.connected,
  }),
}));

import SessionsPage from "./page";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "ses_1",
    source: "bridge",
    project_dir: "/workspace/omniagentos",
    provider: "claude",
    state: "running",
    model: "claude-opus-4-1",
    title: "Implement dashboard observability",
    cost_usd: 1.42,
    last_activity_at: "2026-08-01T00:01:00Z",
    created_at: "2026-08-01T00:00:00Z",
    approvals_requested: 1,
    approvals_granted: 1,
    approvals_denied: 0,
    ...overrides,
  };
}

describe("Sessions count vs. an unknown fetch (D-2)", () => {
  beforeEach(() => {
    sessionsState.sessions = [];
    sessionsState.loading = false;
    sessionsState.error = null;
    sessionsState.unauthorized = false;
    sessionsState.connected = true;
  });

  it("shows an unknown count, never 'All sessions (0)' or an empty table, when sessions have never loaded and the fetch failed", () => {
    sessionsState.error = "Could not reach the sessions API";

    render(<SessionsPage />);

    expect(screen.getByText("Running sessions (\u2014)")).toBeInTheDocument();
    expect(screen.queryByText("Running sessions (0)")).not.toBeInTheDocument();
    expect(screen.queryByText("No sessions have been recorded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Claude Code sessions" })).not.toBeInTheDocument();
  });

  it("still shows a real 'All sessions (0)' and the empty-state copy when the fetch genuinely succeeded empty", () => {
    render(<SessionsPage />);

    expect(screen.getByText("Running sessions (0)")).toBeInTheDocument();
    expect(screen.getByText("No sessions have been recorded.")).toBeInTheDocument();
  });

  it("keeps the real count and table when a later refresh errors after a successful load (stale beats hidden)", () => {
    sessionsState.sessions = [session()];
    sessionsState.error = "Could not reach the sessions API";

    render(<SessionsPage />);

    expect(screen.getByText("Running sessions (1)")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Claude Code sessions" })).toBeInTheDocument();
    expect(screen.queryByText("All sessions (—)")).not.toBeInTheDocument();
  });
});


describe("Running-only default view", () => {
  beforeEach(() => {
    sessionsState.sessions = [];
    sessionsState.loading = false;
    sessionsState.error = null;
    sessionsState.unauthorized = false;
    sessionsState.connected = true;
  });

  it("hides terminal sessions by default and reveals them via the toggle", () => {
    sessionsState.sessions = [
      session(),
      session({ id: "ses_2", state: "completed", title: "Old finished run" }),
      session({ id: "ses_3", state: "failed", title: "Old failed run" }),
    ];
    render(<SessionsPage />);
    expect(screen.getByText("Running sessions (1)")).toBeInTheDocument();
    expect(screen.queryByText("Old finished run")).not.toBeInTheDocument();
    expect(screen.queryByText("Old failed run")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Show finished (2)"));
    expect(screen.getByText("All sessions (3)")).toBeInTheDocument();
    expect(screen.getByText("Old finished run")).toBeInTheDocument();
    expect(screen.getByText("Old failed run")).toBeInTheDocument();
  });

  it("keeps a needs-input session visible even in a terminal state", () => {
    sessionsState.sessions = [
      session({ id: "ses_4", state: "completed", attention_state: "needs_input", attention_reason: "permission: Bash" }),
    ];
    render(<SessionsPage />);
    expect(screen.getByText("Running sessions (1)")).toBeInTheDocument();
  });

  it("shows the hidden-count empty state when everything is finished", () => {
    sessionsState.sessions = [
      session({ id: "ses_5", state: "completed", title: "Done run" }),
    ];
    render(<SessionsPage />);
    expect(screen.getByText("No running sessions. 1 finished session hidden.")).toBeInTheDocument();
  });
});
