import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Agent, LiveBoardTask } from "@/features/collab/types";
import { SETTLED_TASK_STATUSES } from "@/features/swarm/columns";
import { runGranularityStaleHideSet, taskStaleness } from "@/features/board/staleness";
import { InsightsStrip, StalenessToggle } from "./InsightsStrip";

const { fetchWithTimeout } = vi.hoisted(() => ({ fetchWithTimeout: vi.fn() }));

vi.mock("@/lib/fetchTimeout", () => ({ fetchWithTimeout }));

function response(body: unknown, ok = true, status = ok ? 200 : 503) {
  return { ok, status, json: vi.fn().mockResolvedValue(body) } as unknown as Response;
}

const agents: Agent[] = [
  { id: "busy", name: "Busy", lineage: "root", model: null, expertise: [], trust_level: "trusted", status: "busy", created_at: "2026-08-03T00:00:00Z", updated_at: "2026-08-03T00:00:00Z" },
  { id: "idle", name: "Idle", lineage: "root", model: null, expertise: [], trust_level: "trusted", status: "idle", created_at: "2026-08-03T00:00:00Z", updated_at: "2026-08-03T00:00:00Z" },
];

const tasks = [
  { id: "open", status: "open" },
  { id: "done", status: "done" },
  { id: "cancelled", status: "cancelled" },
] as LiveBoardTask[];

afterEach(() => {
  fetchWithTimeout.mockReset();
});

describe("InsightsStrip", () => {
  it("renders provider, today, and card values when both endpoints succeed", async () => {
    fetchWithTimeout.mockImplementation((url: string) => Promise.resolve(
      url === "/api/swarm/providers"
        ? response([{ active_sessions: 3 }])
        : response({ started_today: 8, completed_today: 5 }),
    ));

    render(<InsightsStrip tasks={tasks} agents={agents} />);

    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
    expect(screen.getByText("1 busy · 1 idle")).toBeInTheDocument();
    expect(screen.getByText("Started 8 · Completed 5 (UTC)")).toBeInTheDocument();
    expect(screen.getByText("Open 1")).toBeInTheDocument();
    expect(screen.getByText("Done 2")).toBeInTheDocument();
  });

  it.each([
    ["/api/swarm/providers", "Live sessions"],
    ["/api/dashboard/today", "Attempts today"],
  ])("renders unavailable when %s fails", async (failedPath, label) => {
    fetchWithTimeout.mockImplementation((url: string) => Promise.resolve(
      url === failedPath
        ? response({ message: "unavailable" }, false)
        : url === "/api/swarm/providers"
          ? response([{ active_sessions: 1 }])
          : response({ started_today: 2, completed_today: 1 }),
    ));

    render(<InsightsStrip tasks={tasks} agents={agents} />);

    await waitFor(() => {
      const card = screen.getByText(label).closest(".ds-card");
      expect(card).not.toBeNull();
      expect(card).toHaveTextContent("unavailable");
    });
  });

  it("toggles stale-card visibility", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<StalenessToggle hiddenCount={3} showStale={false} onToggle={onToggle} />);
    const button = screen.getByRole("button", { name: "3 stale hidden" });

    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledOnce();
    expect(button).toHaveAttribute("aria-pressed", "false");

    rerender(<StalenessToggle hiddenCount={3} showStale onToggle={onToggle} />);
    expect(screen.getByRole("button", { name: "3 stale shown" })).toHaveAttribute("aria-pressed", "true");
  });

  it("omits the staleness toggle when no cards are hidden", () => {
    render(<StalenessToggle hiddenCount={0} showStale={false} onToggle={vi.fn()} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders live sessions as unavailable when the agents registry fails", async () => {
    fetchWithTimeout.mockImplementation((url: string) => Promise.resolve(
      url === "/api/swarm/providers"
        ? response([{ active_sessions: 3 }])
        : response({ started_today: 8, completed_today: 5 }),
    ));

    render(<InsightsStrip tasks={tasks} agents={agents} agentsError="Failed to load agents" />);

    await waitFor(() => {
      const card = screen.getByText("Live sessions").closest(".ds-card");
      expect(card).toHaveTextContent("unavailable");
      expect(card).not.toHaveTextContent("1 busy");
      expect(card).not.toHaveTextContent("1 idle");
    });
  });

  it("P0-1: run-granularity stale hiding preserves live swarm membership", () => {
    const liveRun = Array.from({ length: 10 }, (_, index) => ({
      id: `live-${index}`,
      status: index < 8 ? "done" : "in_progress",
      swarm_run_id: "live-run",
    })) as unknown as LiveBoardTask[];
    const staleRun = Array.from({ length: 10 }, (_, index) => ({
      id: `stale-${index}`,
      status: "done",
      swarm_run_id: "stale-run",
    })) as unknown as LiveBoardTask[];

    const hidden = runGranularityStaleHideSet(
      [...liveRun, ...staleRun],
      (task) => task.status === "done",
    );

    expect(liveRun.filter((task) => !hidden.has(task.id))).toHaveLength(10);
    expect(staleRun.filter((task) => !hidden.has(task.id))).toHaveLength(0);
  });

  it("R2: tests the production hide predicate with real staleness", () => {
    const fixedNow = new Date("2026-08-10T12:00:00.000Z");
    // 9+ days old = stale
    const staleDate = new Date("2026-07-31T12:00:00.000Z");
    // 6 days old = not stale
    const freshDate = new Date("2026-08-04T12:00:00.000Z");

    const liveRun = Array.from({ length: 5 }, (_, i) => ({
      id: `live-settled-${i}`,
      status: "done" as const,
      updated_at: i < 3 ? staleDate.toISOString() : freshDate.toISOString(),
      swarm_run_id: "live-run",
    })) as unknown as LiveBoardTask[];

    const staleRun = Array.from({ length: 5 }, (_, i) => ({
      id: `stale-${i}`,
      status: "done" as const,
      updated_at: staleDate.toISOString(),
      swarm_run_id: "stale-run",
    })) as unknown as LiveBoardTask[];

    const blockedCard = {
      id: "blocked-card",
      status: "blocked" as const,
      updated_at: staleDate.toISOString(),
      swarm_run_id: null,
    } as unknown as LiveBoardTask;

    const predicate = (task: LiveBoardTask) =>
      SETTLED_TASK_STATUSES.has(task.status) && taskStaleness(task.updated_at, fixedNow).stale;

    const hidden = runGranularityStaleHideSet(
      [...liveRun, ...staleRun, blockedCard],
      predicate,
    );

    // Live run has mixed stale/fresh settled members — don't hide the run
    expect(liveRun.filter((task) => !hidden.has(task.id))).toHaveLength(5);
    // Stale run has all settled+stale members — hide the run
    expect(staleRun.filter((task) => !hidden.has(task.id))).toHaveLength(0);
    // Blocked card never hides (settled but NOT settled+stale)
    expect(hidden.has("blocked-card")).toBe(false);
  });
});
