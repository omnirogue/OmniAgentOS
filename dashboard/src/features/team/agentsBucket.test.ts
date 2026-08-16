import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { bucketKeyForStatus, buildAgentsBucket } from "./agentsBucket";
import type { TeamQueueBuckets } from "./types";

const TODAY = "2026-08-10";

function task(overrides: Partial<LiveBoardTask> = {}): LiveBoardTask {
  return {
    id: "t1",
    title: "Task",
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "open",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-10T00:00:00Z",
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

function emptyBucket(employeeId: string): TeamQueueBuckets {
  return {
    employee_id: employeeId,
    ready: [],
    active: [],
    blocked: [],
    review: [],
    done_today: [],
    counts: { ready: 0, active: 0, blocked: 0, review: 0, done_today: 0 },
    ready_below_5: false,
  };
}

describe("bucketKeyForStatus", () => {
  it("maps open to ready", () => {
    expect(bucketKeyForStatus("open", "2026-08-10T00:00:00Z", TODAY)).toBe("ready");
  });

  it("maps claimed and in_progress to active", () => {
    expect(bucketKeyForStatus("claimed", "2026-08-10T00:00:00Z", TODAY)).toBe("active");
    expect(bucketKeyForStatus("in_progress", "2026-08-10T00:00:00Z", TODAY)).toBe("active");
  });

  it("maps blocked to blocked", () => {
    expect(bucketKeyForStatus("blocked", "2026-08-10T00:00:00Z", TODAY)).toBe("blocked");
  });

  it("maps awaiting_approval to review", () => {
    expect(bucketKeyForStatus("awaiting_approval", "2026-08-10T00:00:00Z", TODAY)).toBe("review");
  });

  it("maps done updated today to done_today", () => {
    expect(bucketKeyForStatus("done", "2026-08-10T09:00:00Z", TODAY)).toBe("done_today");
  });

  it("does not bucket done from an earlier day", () => {
    expect(bucketKeyForStatus("done", "2026-08-09T09:00:00Z", TODAY)).toBeNull();
  });

  it("does not bucket pending or cancelled", () => {
    expect(bucketKeyForStatus("pending", "2026-08-10T00:00:00Z", TODAY)).toBeNull();
    expect(bucketKeyForStatus("cancelled", "2026-08-10T00:00:00Z", TODAY)).toBeNull();
  });
});

describe("buildAgentsBucket", () => {
  it("returns null when there is nothing owned by no one and no other-employee buckets", () => {
    expect(buildAgentsBucket([], [], TODAY)).toBeNull();
  });

  it("merges other-employee server buckets (e.g. emp_frank, not one of the three sectioned people)", () => {
    const andy = emptyBucket("emp_frank");
    andy.ready.push({ id: "a1", title: "Frank's card", ref: null, status: "open", size: "M" });
    const merged = buildAgentsBucket([andy], [], TODAY);
    expect(merged?.ready.map((c) => c.id)).toEqual(["a1"]);
    expect(merged?.counts.ready).toBe(1);
  });

  it("buckets genuinely unowned live cards by status, mirroring the server mapping", () => {
    const liveTasks = [
      task({ id: "open1", status: "open", owner_employee_id: null }),
      task({ id: "claimed1", status: "claimed", owner_employee_id: null }),
      task({ id: "blocked1", status: "blocked", owner_employee_id: null }),
      task({ id: "review1", status: "awaiting_approval", owner_employee_id: null }),
      task({ id: "done1", status: "done", owner_employee_id: null, updated_at: "2026-08-10T12:00:00Z" }),
      task({ id: "done_old", status: "done", owner_employee_id: null, updated_at: "2026-08-01T12:00:00Z" }),
    ];
    const merged = buildAgentsBucket([], liveTasks, TODAY);
    expect(merged?.ready.map((c) => c.id)).toEqual(["open1"]);
    expect(merged?.active.map((c) => c.id)).toEqual(["claimed1"]);
    expect(merged?.blocked.map((c) => c.id)).toEqual(["blocked1"]);
    expect(merged?.review.map((c) => c.id)).toEqual(["review1"]);
    expect(merged?.done_today.map((c) => c.id)).toEqual(["done1"]);
  });

  it("skips owned cards — they already live in a person's bucket", () => {
    const liveTasks = [task({ id: "owned1", status: "open", owner_employee_id: "emp_owner" })];
    expect(buildAgentsBucket([], liveTasks, TODAY)).toBeNull();
  });

  it("skips archived cards", () => {
    const liveTasks = [task({ id: "archived1", status: "open", owner_employee_id: null, archived: true })];
    expect(buildAgentsBucket([], liveTasks, TODAY)).toBeNull();
  });

  it("excludes pool cards from Agents & unowned", () => {
    const pooled = task({ id: "pooled", owner_employee_id: null });
    expect(buildAgentsBucket([], [pooled], TODAY, new Set(["pooled"]))).toBeNull();
  });
});
