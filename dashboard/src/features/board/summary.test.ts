import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import type { SwarmBoardTask } from "@/features/swarm/types";
import { summarizeBoard, summaryLine } from "./summary";

function card(overrides: Partial<SwarmBoardTask> = {}): SwarmBoardTask {
  const base: LiveBoardTask = {
    id: "t1",
    title: "Task",
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "pending",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-07-23T00:00:00Z",
    updated_at: "2026-07-23T00:00:00Z",
    run_id: null,
    run_state: null,
    run_agent: null,
    run_progress: null,
    run_error: null,
    paused_run: null,
    paused_session: null,
  };
  return { ...base, ...overrides };
}

/** An 11-member swarm run is ONE thing the operator asked for, not eleven. */
describe("summarizeBoard counts requested tasks, not sub-tasks", () => {
  const tasks = [
    // Run A: live — 1 requested task, 3 sub-tasks, 1 done.
    card({ id: "a-root", title: "Swarm: ten mini demos", status: "in_progress", swarm_run_id: "swr_a" }),
    card({ id: "a1", status: "done", swarm_run_id: "swr_a" }),
    card({ id: "a2", status: "in_progress", swarm_run_id: "swr_a" }),
    card({ id: "a3", status: "open", swarm_run_id: "swr_a" }),
    // Run B: finished — 1 requested task, 2 sub-tasks, both done.
    card({ id: "b-root", title: "Swarm: write a haiku", status: "done", swarm_run_id: "swr_b" }),
    card({ id: "b1", status: "done", swarm_run_id: "swr_b" }),
    card({ id: "b2", status: "done", swarm_run_id: "swr_b" }),
    // Two plain cards + one Needs Response.
    card({ id: "solo-open", status: "open" }),
    card({ id: "solo-blocked", status: "blocked" }),
    card({ id: "solo-parked", status: "awaiting_approval" }),
  ];

  it("treats each run as one unit and each solo card as one unit", () => {
    const summary = summarizeBoard(tasks);
    expect(summary.total).toBe(5); // 2 runs + 3 solo cards, NOT 10 cards
    expect(summary.runCount).toBe(2);
    expect(summary.running).toBe(1); // run A
    expect(summary.done).toBe(1); // run B
    expect(summary.queued).toBe(1); // solo-open
    expect(summary.blocked).toBe(1); // solo-blocked
    expect(summary.needsResponse).toBe(1); // solo-parked — not folded into blocked
    expect(summary.pct).toBe(20);
  });

  it("reports sub-task volume separately, never folded into the totals", () => {
    const summary = summarizeBoard(tasks);
    // Run A: 3 members counted as sub-tasks; run B: 2; roots are not sub-tasks.
    expect(summary.subtasks).toEqual({ done: 3, total: 5 });
    // Requested-task total (2 runs + 3 solo) must stay a different number.
    expect(summary.total).toBe(5);
    expect(summary.subtasks.total).toBe(5);
    // The contract: sub-task volume is a separate rollup field, never used as total.
    expect(summary.total).toBe(summary.groups.length + summary.solo.length);
  });

  it("returns the grouped rows so a caller renders exactly what it counted", () => {
    const summary = summarizeBoard(tasks);
    expect(summary.groups.map((group) => group.runId)).toEqual(["swr_a", "swr_b"]);
    expect(summary.solo.map((task) => task.id)).toEqual([
      "solo-open",
      "solo-blocked",
      "solo-parked",
    ]);
  });

  it("folds review/testing/integration phases into running", () => {
    const overlay = new Map([["a2", "testing"]]);
    expect(summarizeBoard(tasks, overlay).running).toBe(1);
  });

  it("is empty-safe", () => {
    const summary = summarizeBoard([]);
    expect(summary.total).toBe(0);
    expect(summary.pct).toBe(0);
  });

  it("renders the shared header line with needs-you first when present", () => {
    expect(summaryLine(summarizeBoard(tasks))).toBe(
      "1 needs you · 1 active · 1 queued · 1 blocked · 1 done · 2 swarms",
    );
  });
});
