import { describe, expect, it } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import type { SwarmBoardTask } from "@/features/swarm/types";
import {
  AGENTS_OWNER_FILTER,
  deriveRunOptions,
  EMPTY_BOARD_FILTERS,
  filterBoardTasks,
  isObservationalCard,
  matchesOwnerFilter,
  missingRunOption,
  type BoardFilters,
} from "./filters";

function card(overrides: Partial<SwarmBoardTask> = {}): SwarmBoardTask {
  const base: LiveBoardTask = {
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

const filters = (overrides: Partial<BoardFilters> = {}): BoardFilters => ({
  ...EMPTY_BOARD_FILTERS,
  ...overrides,
});

describe("filterBoardTasks", () => {
  const tasks: SwarmBoardTask[] = [
    card({ id: "a", title: "Ship the login page", discipline: "eng", lane: "fast", project_id: "proj-1" }),
    card({ id: "b", title: "Write docs", discipline: "docs", lane: "longhaul", category_id: "cat-1", project_id: "proj-2" }),
    card({ id: "c", title: "Refactor login flow", discipline: "eng", swarm_run_id: "swr_1", project_id: "proj-1" }),
    card({ id: "d", title: "Design review", discipline: "design", swarm_run_id: "swr_2", category_id: "cat-1" }),
  ];

  it("returns everything with empty filters", () => {
    expect(filterBoardTasks(tasks, EMPTY_BOARD_FILTERS)).toHaveLength(4);
  });

  it("filters by discipline", () => {
    expect(filterBoardTasks(tasks, filters({ discipline: "eng" })).map((t) => t.id)).toEqual(["a", "c"]);
  });

  it("filters by case-insensitive title substring", () => {
    expect(filterBoardTasks(tasks, filters({ titleQuery: "LOGIN" })).map((t) => t.id)).toEqual(["a", "c"]);
  });

  it("filters by category id", () => {
    expect(filterBoardTasks(tasks, filters({ categoryId: "cat-1" })).map((t) => t.id)).toEqual(["b", "d"]);
  });

  it("filters by swarm run id", () => {
    expect(filterBoardTasks(tasks, filters({ runId: "swr_1" })).map((t) => t.id)).toEqual(["c"]);
  });

  it("composes multiple filters (AND)", () => {
    const out = filterBoardTasks(tasks, filters({ categoryId: "cat-1", discipline: "design" }));
    expect(out.map((t) => t.id)).toEqual(["d"]);
  });

  it("filters by project id — only cards on that project", () => {
    const out = filterBoardTasks(tasks, filters({ projectId: "proj-1" }));
    expect(out.map((t) => t.id)).toEqual(["a", "c"]);
  });

  it("filters by project id — different project", () => {
    const out = filterBoardTasks(tasks, filters({ projectId: "proj-2" }));
    expect(out.map((t) => t.id)).toEqual(["b"]);
  });

  it("filters by unknown project returns empty", () => {
    expect(filterBoardTasks(tasks, filters({ projectId: "proj-unknown" }))).toHaveLength(0);
  });

  it("composes projectId with other filters", () => {
    const out = filterBoardTasks(tasks, filters({ projectId: "proj-1", discipline: "eng" }));
    expect(out.map((t) => t.id)).toEqual(["a", "c"]);
  });

  it("project filter excludes cards without project_id", () => {
    const out = filterBoardTasks(tasks, filters({ projectId: "proj-1" }));
    expect(out.find((t) => t.id === "d")).toBeUndefined();
  });
});

describe("owner filter (Team Work OS, P5)", () => {
  const ownerTasks: LiveBoardTask[] = [
    card({ id: "owner1", owner_employee_id: "emp_owner" }),
    card({ id: "alice1", owner_employee_id: "emp_alice" }),
    card({ id: "agent1", owner_employee_id: null }),
    card({ id: "agent2" }), // owner_employee_id undefined — also "Agents"
  ];

  it("matchesOwnerFilter: empty owner matches everything", () => {
    expect(matchesOwnerFilter(ownerTasks[0]!, "")).toBe(true);
    expect(matchesOwnerFilter(ownerTasks[2]!, "")).toBe(true);
  });

  it("matchesOwnerFilter: a real employee id matches only that owner", () => {
    expect(matchesOwnerFilter(ownerTasks[0]!, "emp_owner")).toBe(true);
    expect(matchesOwnerFilter(ownerTasks[1]!, "emp_owner")).toBe(false);
  });

  it("matchesOwnerFilter: AGENTS_OWNER_FILTER matches null and undefined owner_employee_id", () => {
    expect(matchesOwnerFilter(ownerTasks[2]!, AGENTS_OWNER_FILTER)).toBe(true);
    expect(matchesOwnerFilter(ownerTasks[3]!, AGENTS_OWNER_FILTER)).toBe(true);
    expect(matchesOwnerFilter(ownerTasks[0]!, AGENTS_OWNER_FILTER)).toBe(false);
  });

  it("filterBoardTasks scopes to one owner", () => {
    const out = filterBoardTasks(ownerTasks, filters({ owner: "emp_owner" }));
    expect(out.map((t) => t.id)).toEqual(["owner1"]);
  });

  it("filterBoardTasks scopes to Agents (unowned)", () => {
    const out = filterBoardTasks(ownerTasks, filters({ owner: AGENTS_OWNER_FILTER }));
    expect(out.map((t) => t.id)).toEqual(["agent1", "agent2"]);
  });

  it("filterBoardTasks composes owner with other filters", () => {
    const mixed = [
      card({ id: "eng-owner", discipline: "eng", owner_employee_id: "emp_owner" }),
      card({ id: "docs-owner", discipline: "docs", owner_employee_id: "emp_owner" }),
    ];
    const out = filterBoardTasks(mixed, filters({ owner: "emp_owner", discipline: "eng" }));
    expect(out.map((t) => t.id)).toEqual(["eng-owner"]);
  });
});

describe("company filter (multi-company Work OS, 2026-08-13)", () => {
  const companyTasks: LiveBoardTask[] = [
    card({ id: "cf1", company_slug: "globex" }),
    card({ id: "acmeuni1", company_slug: "acmeuni" }),
    card({ id: "none1", company_slug: null }),
    card({ id: "legacy1" }), // company_slug absent — un-upgraded server
  ];

  it("empty companyId matches everything, including legacy cards", () => {
    expect(filterBoardTasks(companyTasks, filters({ companyId: "" }))).toHaveLength(4);
  });

  it("scopes to one company by slug", () => {
    const out = filterBoardTasks(companyTasks, filters({ companyId: "acmeuni" }));
    expect(out.map((t) => t.id)).toEqual(["acmeuni1"]);
  });

  it("excludes null and absent company_slug when a company is selected", () => {
    const out = filterBoardTasks(companyTasks, filters({ companyId: "globex" }));
    expect(out.map((t) => t.id)).toEqual(["cf1"]);
  });

  it("unknown company slug returns empty", () => {
    expect(filterBoardTasks(companyTasks, filters({ companyId: "hooli" }))).toHaveLength(0);
  });

  it("composes companyId with the owner filter", () => {
    const mixed = [
      card({ id: "cf-owner", company_slug: "globex", owner_employee_id: "emp_owner" }),
      card({ id: "cf-alice", company_slug: "globex", owner_employee_id: "emp_alice" }),
      card({ id: "acmeuni-owner", company_slug: "acmeuni", owner_employee_id: "emp_owner" }),
    ];
    const out = filterBoardTasks(mixed, filters({ companyId: "globex", owner: "emp_owner" }));
    expect(out.map((t) => t.id)).toEqual(["cf-owner"]);
  });
});

describe("deriveRunOptions", () => {
  it("returns distinct sorted runs, labelled by fleet goal when known", () => {
    const tasks: SwarmBoardTask[] = [
      card({ id: "1", swarm_run_id: "swr_bbb" }),
      card({ id: "2", swarm_run_id: "swr_aaa" }),
      card({ id: "3", swarm_run_id: "swr_aaa" }),
      card({ id: "4" }),
    ];
    const goals = new Map<string, string | null>([["swr_aaa", "Refactor auth"]]);
    const opts = deriveRunOptions(tasks, goals);
    expect(opts).toEqual([
      { id: "swr_aaa", label: "Refactor auth" },
      { id: "swr_bbb", label: "Run bbb" },
    ]);
  });

  it("returns no options when the board has no swarm cards", () => {
    expect(deriveRunOptions([card({ id: "x" })])).toEqual([]);
  });
});

describe("missingRunOption", () => {
  it("labels a stale ?run= id as not on board, with the short id", () => {
    expect(missingRunOption("swr_850835eba377431f854a")).toEqual({
      id: "swr_850835eba377431f854a",
      label: "Run 850835eb (not on board)",
    });
  });

  it("keeps unprefixed ids intact", () => {
    expect(missingRunOption("abcdef1234")).toEqual({
      id: "abcdef1234",
      label: "Run abcdef12 (not on board)",
    });
  });
});

describe("isObservationalCard", () => {
  it("matches a claude terminal-session sighting title", () => {
    expect(isObservationalCard("[claude · external] youruser · ttys020")).toBe(true);
  });

  it("matches a codex terminal-session sighting title", () => {
    expect(isObservationalCard("[codex · external] DockerDevOps_Sol · ttys272")).toBe(true);
  });

  it("does not match a real filed task title", () => {
    expect(isObservationalCard("Fix the merge-gate false refusal on seed_cursor")).toBe(false);
  });
});
