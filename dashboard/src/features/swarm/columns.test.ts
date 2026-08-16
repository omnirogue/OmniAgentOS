import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { columnFor, swarmPhase, VISION_COLUMNS, type PhaseOverlay } from "./columns";
import type { SwarmBoardTask, SwarmJson } from "./types";

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

describe("columnFor — non-swarm cards degrade cleanly", () => {
  it("maps the plain intake/longhaul statuses onto vision columns", () => {
    expect(columnFor(card({ status: "pending" }))).toBe("backlog");
    expect(columnFor(card({ status: "open" }))).toBe("ready");
    expect(columnFor(card({ status: "claimed" }))).toBe("running");
    expect(columnFor(card({ status: "in_progress" }))).toBe("running");
    expect(columnFor(card({ status: "blocked" }))).toBe("blocked");
    expect(columnFor(card({ status: "done" }))).toBe("completed");
    expect(columnFor(card({ status: "cancelled" }))).toBe("cancelled");
  });

  it("keeps an in_progress card with NO swarm metadata in Running", () => {
    // The load-bearing property: a plain LiveBoardTask never lands in
    // Review/Testing/Integration.
    const plain: LiveBoardTask = card({ status: "in_progress" });
    expect(["review", "testing", "integration"]).not.toContain(columnFor(plain));
  });
});

describe("columnFor — swarm phase overlay", () => {
  it("routes an in_progress swarm card by its live overview phase", () => {
    const t = card({ id: "swarm-1", status: "in_progress", swarm_run_id: "swr_abc" });
    const overlay = new Map<string, string>([["swarm-1", "review"]]);
    expect(columnFor(t, overlay)).toBe("review");
    expect(columnFor(t, new Map([["swarm-1", "testing"]]))).toBe("testing");
    expect(columnFor(t, new Map([["swarm-1", "integration"]]))).toBe("integration");
  });

  it("falls back to swarm_json.swarm_phase when no overlay is present", () => {
    const meta: SwarmJson = { swarm_phase: "verifying" };
    const t = card({ id: "s2", status: "in_progress", swarm_run_id: "swr_x", swarm_json: meta });
    expect(columnFor(t)).toBe("testing");
  });

  it("tolerates swarm_json arriving as raw TEXT", () => {
    const t = card({
      id: "s3",
      status: "in_progress",
      swarm_run_id: "swr_y",
      swarm_json: JSON.stringify({ swarm_phase: "merging" }),
    });
    expect(swarmPhase(t)).toBe("merging");
    expect(columnFor(t)).toBe("integration");
  });

  it("lets the live overlay win over a stale swarm_json phase", () => {
    const t = card({
      id: "s4",
      status: "in_progress",
      swarm_run_id: "swr_z",
      swarm_json: { swarm_phase: "review" },
    });
    expect(columnFor(t, new Map([["s4", "testing"]]))).toBe("testing");
  });

  it("routes an integration task with no phase into Integration", () => {
    const t = card({ id: "s5", status: "in_progress", swarm_run_id: "swr_i", swarm_json: { integration: true } });
    expect(columnFor(t)).toBe("integration");
  });
});

describe("columnFor — server-projected swarm fields (no swarm_json envelope)", () => {
  // Board LIST payloads dropped `swarm_json` (47.8 MB of it on a live board) and
  // now project the two consumed fields flat. Same kanban behaviour, 1/25th the bytes.
  it("routes by the projected swarm_phase with no envelope at all", () => {
    const t = card({
      id: "p1",
      status: "in_progress",
      swarm_run_id: "swr_p",
      swarm_phase: "reviewing",
    });
    expect(t.swarm_json).toBeUndefined();
    expect(swarmPhase(t)).toBe("reviewing");
    expect(columnFor(t)).toBe("review");
  });

  it("routes a projected integration card into Integration", () => {
    const t = card({
      id: "p2",
      status: "in_progress",
      swarm_run_id: "swr_p",
      swarm_phase: null,
      // SQLite json_extract yields 1/0 for a JSON boolean.
      swarm_integration: 1,
    });
    expect(columnFor(t)).toBe("integration");
  });

  it("keeps a projected non-swarm card (phase null, integration false) in Running", () => {
    const t = card({
      id: "p3",
      status: "in_progress",
      swarm_phase: null,
      swarm_integration: false,
    });
    expect(swarmPhase(t)).toBeNull();
    expect(columnFor(t)).toBe("running");
  });

  it("lets the live overlay win over the projected phase", () => {
    const t = card({
      id: "p4",
      status: "in_progress",
      swarm_run_id: "swr_p",
      swarm_phase: "review",
    });
    expect(columnFor(t, new Map([["p4", "testing"]]))).toBe("testing");
  });

  it("prefers the projected phase over a stale envelope, when both arrive", () => {
    const t = card({
      id: "p5",
      status: "in_progress",
      swarm_run_id: "swr_p",
      swarm_phase: "testing",
      swarm_json: { swarm_phase: "review" },
    });
    expect(columnFor(t)).toBe("testing");
  });
});

describe("VISION_COLUMNS", () => {
  it("exposes the truthful ordered columns, with Needs-you after Running", () => {
    // Was eight. `needs_you` was added when awaiting_approval became a real board
    // status: it used to map to in_progress, so a card parked on a human decision
    // rendered as Running and was indistinguishable from work in flight.
    expect(VISION_COLUMNS.map((c) => c.id)).toEqual([
      "backlog",
      "ready",
      "running",
      "needs_you",
      "review",
      "testing",
      "integration",
      "completed",
      "cancelled",
      "blocked",
    ]);
  });

  it("routes awaiting_approval to needs_you, never to running", () => {
    expect(columnFor(card({ status: "awaiting_approval" }))).toBe("needs_you");
  });

  it("keeps awaiting_approval in needs_you even with a swarm phase overlay", () => {
    // A parked card must not be phase-overlaid into Review/Testing/Integration:
    // whatever the swarm believes it is doing, nothing advances until someone decides.
    const parked = card({ status: "awaiting_approval", swarm_run_id: "swr_x" });
    const overlay: PhaseOverlay = new Map([[parked.id, "review"]]);
    expect(columnFor(parked, overlay)).toBe("needs_you");
  });
});
