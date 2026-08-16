import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Routine } from "@/features/routines/types";
import type { Approval } from "@/lib/contracts";

const apiGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}));

import { LoopHealthCard } from "./LoopHealthCard";

function routine(overrides: Partial<Routine> = {}): Routine {
  return {
    id: "rtn_health",
    name: "w3-health-monitor",
    description: "",
    trigger_type: "cron",
    trigger_config: { cron: "*/10 * * * *" },
    task_template: {
      input: {
        module: "omniagentos.loops",
        instance_id: "w3_health_monitor",
        instance_module: "omniagentos_loops.instances.health_monitor",
      },
    },
    gate_type: "exit_code",
    gate_config: {},
    hard_cap_type: "budget_usd",
    hard_cap_value: 5,
    notification_target: {},
    status: "active",
    auto_pause_reason: "",
    total_runs: 2,
    accepted_runs: 0,
    acceptance_rate: null,
    total_cost_usd: 0.5,
    cost_per_accepted_change: null,
    last_fired: new Date(Date.now() - 5 * 60_000).toISOString(),
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "apr_1",
    run_id: null,
    task_id: null,
    step_seq: null,
    action_class: "consequential",
    proposed_action: "w3_health_monitor/diagnose: restart service",
    risk: "loop_approval",
    evidence: "loop effect at or above the T2 approval floor",
    state: "pending",
    decided_by: null,
    decision_note: null,
    decided_at: null,
    expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function mockEndpoints(opts: {
  routines?: Routine[] | (() => Promise<Routine[]>);
  runs?: unknown[];
  approvals?: Approval[];
  routinesRejects?: boolean;
}) {
  apiGet.mockImplementation((path: string) => {
    if (path === "/api/routines") {
      if (opts.routinesRejects) return Promise.reject(new Error("503: backend unavailable"));
      return Promise.resolve(opts.routines ?? []);
    }
    if (path.startsWith("/api/routines/runs")) return Promise.resolve({ runs: opts.runs ?? [] });
    if (path.startsWith("/api/approvals")) return Promise.resolve(opts.approvals ?? []);
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

describe("LoopHealthCard", () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it("renders STALE for a routine whose last run is older than 2x its cron interval, never its last status", async () => {
    mockEndpoints({
      routines: [
        routine({ last_fired: new Date(Date.now() - 25 * 60_000).toISOString() }),
      ],
    });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByText("STALE")).toBeInTheDocument());
    expect(screen.queryByText("OK")).not.toBeInTheDocument();
  });

  it("renders 'no judged runs' for a null acceptance_rate, never 0%", async () => {
    mockEndpoints({ routines: [routine({ acceptance_rate: null })] });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByText("no judged runs")).toBeInTheDocument());
    expect(screen.queryByText(/0%/)).not.toBeInTheDocument();
  });

  // ISSUE-8: a null total_cost_usd (migration 119 — cost unknown) must render
  // as "unknown total cost", never crash on `.toFixed`, and never show $0.00.
  it("renders 'unknown total cost' for a null total_cost_usd, never $0.00, and never throws", async () => {
    mockEndpoints({ routines: [routine({ total_cost_usd: null })] });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByText("unknown total cost")).toBeInTheDocument());
    expect(screen.queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it("renders a parked loop as amber (warn), never red (danger)", async () => {
    mockEndpoints({
      routines: [routine()],
      approvals: [approval()],
    });

    render(<LoopHealthCard />);

    const badge = await screen.findByText("Needs you");
    expect(badge.className).toContain("ds-badge--warn");
    expect(badge.className).not.toContain("ds-badge--danger");
  });

  it("renders an auto_paused loop as red (danger)", async () => {
    mockEndpoints({
      routines: [routine({ status: "auto_paused", auto_pause_reason: "acceptance below floor" })],
    });

    render(<LoopHealthCard />);

    const badge = await screen.findByText("Auto-paused");
    expect(badge.className).toContain("ds-badge--danger");
  });

  it("renders an ERROR state and zero loop rows on an API failure -- never fixture content", async () => {
    mockEndpoints({ routinesRejects: true });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText(/Could not load loop health/)).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    // Never a "0 loops" / empty-state placeholder that could be mistaken for
    // a real (if empty) success — the error case is visually distinct.
    expect(screen.queryByText("No loops registered.")).not.toBeInTheDocument();
  });

  it("renders nothing (not an error, not a fixture) when there are no loop routines", async () => {
    mockEndpoints({ routines: [] });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByText("No loops registered.")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("surfaces an unmatched loop-risk approval instead of dropping it silently", async () => {
    mockEndpoints({
      routines: [routine()],
      approvals: [approval({ id: "apr_stray", proposed_action: "renamed_instance/diagnose: x" })],
    });

    render(<LoopHealthCard />);

    await waitFor(() =>
      expect(screen.getByText(/doesn't match any registered routine/)).toBeInTheDocument(),
    );
    // The stray approval must not be attributed to the one known loop.
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });

  it("filters out non-loop routines", async () => {
    mockEndpoints({
      routines: [
        routine(),
        {
          ...routine(),
          id: "rtn_ordinary",
          name: "ordinary-sweep",
          task_template: { input: { kind: "skill", skill: "ops.heartbeat" } },
        },
      ],
    });

    render(<LoopHealthCard />);

    await waitFor(() => expect(screen.getByText("w3-health-monitor")).toBeInTheDocument());
    expect(screen.queryByText("ordinary-sweep")).not.toBeInTheDocument();
  });
});
