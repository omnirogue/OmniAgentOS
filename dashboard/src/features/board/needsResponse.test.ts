import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import {
  isNeedsResponse,
  listAttentionRank,
  selectNeedsResponse,
} from "./needsResponse";

function card(overrides: Partial<LiveBoardTask> = {}): LiveBoardTask {
  return {
    id: "btk_1",
    title: "Task",
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "open",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-07-25T00:00:00Z",
    updated_at: "2026-07-25T00:00:00Z",
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

describe("isNeedsResponse — strict predicate", () => {
  it("includes awaiting_approval status", () => {
    expect(isNeedsResponse(card({ status: "awaiting_approval" }))).toBe(true);
  });

  it("includes a bound pending_approval even if status lags", () => {
    expect(
      isNeedsResponse(
        card({
          status: "in_progress",
          pending_approval: {
            id: "apr_1",
            command: "cat > ~/Desktop/bird-clock.html",
            action_class: "sandboxed_creation",
          },
        }),
      ),
    ).toBe(true);
  });

  it("includes work.state awaiting_approval", () => {
    expect(
      isNeedsResponse(
        card({
          status: "in_progress",
          work: {
            kind: "session",
            state: "awaiting_approval",
            agent: null,
            steps_done: 0,
            steps_total: 0,
            current_step: null,
            files_count: null,
            cost_usd: null,
            last_activity_at: null,
            error: null,
          },
        }),
      ),
    ).toBe(true);
  });

  it("excludes blocked (FAILED) cards — they are backlog, not Needs Response", () => {
    expect(isNeedsResponse(card({ status: "blocked" }))).toBe(false);
  });

  it("rejects a stale pending_approval on blocked/done/cancelled", () => {
    // Otherwise a failed session that never voided its approval inflates the band.
    const stale = {
      id: "apr_stale",
      command: "rm -rf /tmp/x",
      action_class: "irreversible",
    };
    expect(isNeedsResponse(card({ status: "blocked", pending_approval: stale }))).toBe(false);
    expect(isNeedsResponse(card({ status: "done", pending_approval: stale }))).toBe(false);
    expect(isNeedsResponse(card({ status: "cancelled", pending_approval: stale }))).toBe(false);
    expect(isNeedsResponse(card({ status: "pending", pending_approval: stale }))).toBe(false);
  });

  it("excludes plain open / in_progress / done", () => {
    expect(isNeedsResponse(card({ status: "open" }))).toBe(false);
    expect(isNeedsResponse(card({ status: "in_progress" }))).toBe(false);
    expect(isNeedsResponse(card({ status: "done" }))).toBe(false);
  });
});

describe("summarizeBoard aligns needsResponse with the queue predicate", () => {
  it("counts a card with pending_approval even when status still says in_progress", async () => {
    const { summarizeBoard } = await import("./summary");
    const parked = card({
      id: "lag",
      status: "in_progress",
      pending_approval: {
        id: "apr_lag",
        command: "echo lag",
        action_class: "read_only",
      },
    });
    const summary = summarizeBoard([parked]);
    expect(summary.needsResponse).toBe(1);
    expect(summary.running).toBe(0);
  });
});

describe("selectNeedsResponse — ranks by consequence, returns only true needs", () => {
  it("returns 1, not 104: only the real parking, not blocked backlog", () => {
    const birdClock = card({
      id: "btk_bird",
      title: "make a bird clock on my desktop",
      status: "awaiting_approval",
      pending_approval: {
        id: "apr_bird",
        command: "cat > ~/Desktop/outputs/bird-clock.html << 'EOF'",
        action_class: "sandboxed_creation",
        created_at: "2026-07-25T10:00:00Z",
      },
    });
    // 49 blocked cards + open noise — none of these belong in Needs Response.
    const backlog = Array.from({ length: 49 }, (_, i) =>
      card({ id: `btk_blocked_${i}`, status: "blocked", title: `blocked ${i}` }),
    );
    const open = Array.from({ length: 54 }, (_, i) =>
      card({ id: `btk_open_${i}`, status: "open", title: `open ${i}` }),
    );
    const selected = selectNeedsResponse([birdClock, ...backlog, ...open]);
    expect(selected).toHaveLength(1);
    expect(selected[0].id).toBe("btk_bird");
    expect(selected[0].pending_approval?.command).toContain("bird-clock");
    expect(selected[0].pending_approval?.command).not.toBe("Bash");
  });

  it("orders riskier action_class before safer, then oldest wait", () => {
    const olderSafe = card({
      id: "btk_safe",
      status: "awaiting_approval",
      pending_approval: {
        id: "apr_safe",
        command: "ls",
        action_class: "read_only",
        created_at: "2026-07-25T08:00:00Z",
      },
    });
    const newerRisky = card({
      id: "btk_risky",
      status: "awaiting_approval",
      pending_approval: {
        id: "apr_risky",
        command: "rm -rf /tmp/x",
        action_class: "irreversible",
        created_at: "2026-07-25T12:00:00Z",
      },
    });
    const selected = selectNeedsResponse([olderSafe, newerRisky]);
    expect(selected.map((t) => t.id)).toEqual(["btk_risky", "btk_safe"]);
  });
});

describe("listAttentionRank — Needs Response rises", () => {
  it("ranks awaiting_approval above in_progress", () => {
    const parked = card({ status: "awaiting_approval" });
    const running = card({ status: "in_progress" });
    expect(listAttentionRank(parked)).toBeLessThan(listAttentionRank(running));
  });
});
