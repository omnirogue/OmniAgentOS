import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { attemptModels, flattenAttempts, groupSwarmRuns, liveSessionCount } from "./runAggregate";
import type { SwarmBoardTask, SwarmRunAttempt } from "./types";

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

function attempt(overrides: Partial<SwarmRunAttempt> = {}): SwarmRunAttempt {
  return { id: "swa_1", board_task_id: "t1", ...overrides };
}

/**
 * `GET /api/swarm/{id}` serializes `attempts` as `{board_task_id: attempt[]}`.
 * Treating it as a flat array threw `attempts.filter is not a function` during
 * SwarmRunCard's render, which unmounted the tree and turned /board into Next's
 * "Application error: a client-side exception has occurred".
 */
describe("attempts arrive as a map keyed by board task id", () => {
  it("flattens the map shape the API actually returns", () => {
    const attempts = {
      t1: [attempt({ id: "a1" }), attempt({ id: "a2" })],
      t2: [attempt({ id: "a3", board_task_id: "t2" })],
    };
    expect(flattenAttempts(attempts).map((a) => a.id)).toEqual(["a1", "a2", "a3"]);
  });

  it("still accepts a flat array, and never throws on junk", () => {
    expect(flattenAttempts([attempt({ id: "a1" })]).map((a) => a.id)).toEqual(["a1"]);
    expect(flattenAttempts(null)).toEqual([]);
    expect(flattenAttempts(undefined)).toEqual([]);
    // A bucket that is not an array (a partial/older payload) is skipped, not fatal.
    expect(flattenAttempts({ t1: null as unknown as SwarmRunAttempt[] })).toEqual([]);
  });

  it("counts live sessions across the map without throwing", () => {
    const attempts = {
      t1: [
        attempt({ id: "a1", session_id: "ses_1", end_reason: null }),
        attempt({ id: "a2", session_id: "ses_2", end_reason: "completed" }),
      ],
      t2: [attempt({ id: "a3", board_task_id: "t2", session_id: "ses_3", end_reason: null })],
    };
    expect(() => liveSessionCount(attempts)).not.toThrow();
    expect(liveSessionCount(attempts)).toBe(2);
    expect(liveSessionCount(null)).toBe(0);
  });

  it("lists models across the map, live ones first", () => {
    const attempts = {
      t1: [attempt({ id: "a1", model: "gpt-5.6-sol", end_reason: "review_denied" })],
      t2: [attempt({ id: "a2", board_task_id: "t2", model: "claude-opus-5", end_reason: null })],
    };
    expect(attemptModels(attempts)).toEqual(["claude-opus-5", "gpt-5.6-sol"]);
    expect(attemptModels(null)).toEqual([]);
  });
});

describe("groupSwarmRuns", () => {
  it("keeps one group per run, root excluded from members", () => {
    const tasks = [
      card({ id: "root", title: "Swarm: build a thing", status: "in_progress", swarm_run_id: "swr_1" }),
      card({ id: "m1", title: "part one", status: "done", swarm_run_id: "swr_1" }),
      card({ id: "m2", title: "part two", status: "in_progress", swarm_run_id: "swr_1" }),
      card({ id: "solo", title: "unrelated", status: "open" }),
    ];
    const { groups, solo } = groupSwarmRuns(tasks, new Map());
    expect(groups).toHaveLength(1);
    expect(solo.map((task) => task.id)).toEqual(["solo"]);
    expect(groups[0].title).toBe("build a thing");
    expect(groups[0].members.map((task) => task.id)).toEqual(["m1", "m2"]);
    expect(groups[0].done).toBe(1);
    expect(groups[0].total).toBe(2);
    expect(groups[0].column).toBe("running");
  });
});

/**
 * A FAILED swarm run must never read as "not started".
 *
 * `SwarmDal.close_out_run_cards` (omniagentos/swarm/dal.py) settles a terminal
 * run's still-live cards: `run failed -> cards blocked`, and it skips cards that
 * are ALREADY terminal. So when a run fails after its member cards finished, the
 * only card that moves is the root `Swarm: <goal>` card — root `blocked`, members
 * still `done`, `group.blocked === 0`. `columnFor` maps a lone card with
 * `status: "blocked"` onto the Blocked column; the aggregated run card is the ONLY
 * representation of that run on the board (one card per run, by directive), so it
 * has to agree.
 */
describe("a failed run's root card decides the column", () => {
  it("files a run whose root is blocked under Blocked, not Backlog", () => {
    // run failed after every member finished: close_out_run_cards flipped only
    // the root, because done/blocked/cancelled cards are left alone.
    const tasks = [
      card({ id: "root", title: "Swarm: ship it", status: "blocked", swarm_run_id: "swr_2" }),
      card({ id: "m1", title: "part one", status: "done", swarm_run_id: "swr_2" }),
      card({ id: "m2", title: "part two", status: "done", swarm_run_id: "swr_2" }),
    ];
    const { groups } = groupSwarmRuns(tasks, new Map());
    expect(groups[0].blocked).toBe(0);
    expect(groups[0].column).toBe("blocked");
  });

  it("files a run that failed before any member card existed under Blocked", () => {
    // planning/admission died: the root card exists, no members were ever created.
    const tasks = [
      card({ id: "root", title: "Swarm: ship it", status: "blocked", swarm_run_id: "swr_3" }),
    ];
    const { groups } = groupSwarmRuns(tasks, new Map());
    expect(groups[0].total).toBe(0);
    expect(groups[0].column).toBe("blocked");
  });

  it("still lets live member work outrank a stale blocked root", () => {
    const tasks = [
      card({ id: "root", title: "Swarm: ship it", status: "blocked", swarm_run_id: "swr_4" }),
      card({ id: "m1", title: "part one", status: "in_progress", swarm_run_id: "swr_4" }),
    ];
    const { groups } = groupSwarmRuns(tasks, new Map());
    expect(groups[0].column).toBe("running");
  });
});
