import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";
import { TeamBoard } from "./TeamBoard";
import type { TeamBoardResponse, TeamQueueBuckets } from "./types";

const { boardMock, liveBoardMock, projectTreeMock } = vi.hoisted(() => ({
  boardMock: vi.fn(),
  liveBoardMock: vi.fn(),
  projectTreeMock: vi.fn(),
}));

vi.mock("./hooks", () => ({ useTeamBoard: boardMock }));
vi.mock("@/features/collab/hooks", () => ({ useLiveBoard: liveBoardMock }));
vi.mock("@/features/projects/hierarchyHooks", () => ({ useProjectTree: projectTreeMock }));

projectTreeMock.mockReturnValue({ nodes: [], loading: false, error: null, source: "live", refresh: vi.fn() });

function bucket(overrides: Partial<TeamQueueBuckets> = {}): TeamQueueBuckets {
  return {
    employee_id: "emp_owner",
    ready: [],
    active: [],
    blocked: [],
    review: [],
    done_today: [],
    counts: { ready: 0, active: 0, blocked: 0, review: 0, done_today: 0 },
    ready_below_5: false,
    ...overrides,
  };
}

function renderBoard(board: TeamBoardResponse, tasks: LiveBoardTask[] = []) {
  liveBoardMock.mockReturnValue({ tasks });
  boardMock.mockReturnValue({ board, loading: false, error: null, hasLoaded: true, refresh: vi.fn() });
  return render(<TeamBoard onOpenTask={vi.fn()} />);
}

describe("TeamBoard pool and active warnings", () => {
  it("renders pool cards and its depth", () => {
    renderBoard({ buckets: {},
      pool: {
        cards: [{ id: "pool-1", title: "Pool task", ref: "POOL-1", status: "open", size: "M" }],
        depth: 12,
        low: false,
      },
    });

    expect(screen.getByRole("heading", { name: "Pool" })).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("POOL-1")).toBeInTheDocument();
    expect(screen.getByText("Pool task")).toBeInTheDocument();
    expect(screen.getByText("M")).toBeInTheDocument();
  });

  it("shows the low warning badge from the pool contract", () => {
    renderBoard({ buckets: {}, pool: { cards: [], depth: 9, low: true } });
    expect(screen.getByText("Low pool")).toBeInTheDocument();
  });

  it("enriches pool cards with priority and verified state", () => {
    const enrichedTask = {
      id: "pool-1", title: "Pool task", description: "", required_expertise: [], discipline: null,
      // "done" (not "open"): the tri-state badge (migration 132) only ever
      // renders on a DONE card — a verified_at on an open card cannot happen
      // server-side (verify_task requires status='done'), so a realistic
      // fixture needs the matching status for the badge to appear at all.
      priority: "high", status: "done", claimed_by: null, claim_version: 0, result_ref: null,
      created_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-10T00:00:00Z", run_id: null,
      run_state: null, run_agent: null, run_progress: null, run_error: null, paused_run: null,
      paused_session: null, verified_at: "2026-08-10T00:00:00Z", verified_by: "emp_owner",
    } as LiveBoardTask;
    renderBoard({ buckets: {}, pool: { cards: [{ id: "pool-1", title: "Pool task", ref: "POOL-1", status: "open", size: "M" }], depth: 1, low: false } }, [enrichedTask]);
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.getByTitle("Verified by the operator")).toBeInTheDocument();
  });

  it("renders the failed-verification badge with the reason in its tooltip", () => {
    const failedTask = {
      id: "pool-3", title: "Failed card", description: "", required_expertise: [], discipline: null,
      priority: "normal", status: "done", claimed_by: null, claim_version: 0, result_ref: null,
      created_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-10T00:00:00Z", run_id: null,
      run_state: null, run_agent: null, run_progress: null, run_error: null, paused_run: null,
      paused_session: null, verified_at: null, verified_by: null,
      // Migration 132 columns — not on `LiveBoardTask` (see TeamBoard.tsx's
      // `EnrichedTeamTask` local cast); present here purely as extra fields
      // on the fixture object, exactly the shape a real `GET /api/board` row
      // sends once `_BOARD_LIST_COLUMNS` includes them.
      verification_failed_at: "2026-08-10T01:00:00Z",
      verification_failed_by: "emp_alice",
      verification_failed_reason: "no tests",
    } as LiveBoardTask;
    renderBoard(
      {
        buckets: {},
        pool: { cards: [{ id: "pool-3", title: "Failed card", ref: "POOL-3", status: "open", size: "M" }], depth: 1, low: false },
      },
      [failedTask],
    );
    expect(screen.getByText("✗")).toBeInTheDocument();
    expect(screen.getByTitle("Verification failed: no tests")).toBeInTheDocument();
  });

  it("renders NO verification badge when enrichment is missing (indeterminate, not unverified)", () => {
    // The pool card names no enrichment for "pool-4" at all (empty tasks
    // list) — this must NOT collapse to the ○ unverified mark; it must show
    // nothing, because "we don't know yet" and "the server confirmed
    // unverified" are different answers.
    renderBoard(
      {
        buckets: {},
        pool: { cards: [{ id: "pool-4", title: "Unenriched card", ref: "POOL-4", status: "open", size: "M" }], depth: 1, low: false },
      },
      [],
    );
    expect(screen.getByText("Unenriched card")).toBeInTheDocument();
    expect(screen.queryByText("✓")).not.toBeInTheDocument();
    expect(screen.queryByText("✗")).not.toBeInTheDocument();
    expect(screen.queryByText("○")).not.toBeInTheDocument();
  });

  it("does not render a Pool column for the old payload", () => {
    renderBoard({ buckets: { emp_owner: bucket() }, pool: null });
    expect(screen.queryByRole("heading", { name: "Pool" })).not.toBeInTheDocument();
  });

  it("marks a person whose active queue is below five", () => {
    renderBoard({ buckets: { emp_owner: bucket({ active_below_5: true, counts: { ready: 0, active: 2, blocked: 0, review: 0, done_today: 0 } }) }, pool: null });
    expect(screen.getByText("below target: 2/5 active")).toBeInTheDocument();
    expect(screen.getByText("below target: 2/5 active").className).toContain("ds-badge");
  });

  it("omits the active warning when the optional flag is absent", () => {
    renderBoard({ buckets: { emp_owner: bucket() }, pool: null });
    expect(screen.queryByText(/below target:/)).not.toBeInTheDocument();
  });

  it("shows the team-board fallback when a card throws during render", () => {
    const throwingCard = {
      id: "broken-card",
      title: "Broken card",
      ref: null,
      get status(): string {
        throw new Error("malformed card status");
      },
      size: "M",
    } as TeamBoardResponse["pool"] extends { cards: (infer Card)[] } ? Card : never;

    renderBoard({ buckets: {}, pool: { cards: [throwingCard], depth: 1, low: false } });

    expect(screen.getByText("The team board failed to render")).toBeInTheDocument();
    expect(screen.getByText("malformed card status")).toBeInTheDocument();
  });
});

function liveTask(overrides: Partial<LiveBoardTask> = {}): LiveBoardTask {
  return {
    id: "t-1", title: "Task", description: "", required_expertise: [], discipline: null,
    priority: "normal", status: "open", claimed_by: null, claim_version: 0, result_ref: null,
    created_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-10T00:00:00Z", run_id: null,
    run_state: null, run_agent: null, run_progress: null, run_error: null, paused_run: null,
    paused_session: null,
    ...overrides,
  } as LiveBoardTask;
}

async function selectOption(triggerLabel: string, optionLabel: string) {
  fireEvent.click(screen.getByRole("button", { name: triggerLabel }));
  const listbox = await screen.findByRole("listbox");
  fireEvent.click(within(listbox).getByText(optionLabel));
}

describe("TeamBoard priority fallback", () => {
  it("renders the priority chip from card.priority when there is no enrichment yet", () => {
    renderBoard({
      buckets: { emp_owner: bucket({ ready: [{ id: "c-1", title: "Fallback priority", ref: null, status: "open", size: "M", priority: "urgent" }] }) },
      pool: null,
    });
    expect(screen.getByText("urgent")).toBeInTheDocument();
  });

  it("suppresses the chip for a 'normal' priority to avoid noise", () => {
    renderBoard({
      buckets: { emp_owner: bucket({ ready: [{ id: "c-1", title: "Normal priority", ref: null, status: "open", size: "M", priority: "normal" }] }) },
      pool: null,
    });
    expect(screen.queryByText("normal")).not.toBeInTheDocument();
  });

  it("prefers card.priority over an enriched task's priority", () => {
    renderBoard(
      {
        buckets: { emp_owner: bucket({ ready: [{ id: "c-1", title: "Both", ref: null, status: "open", size: "M", priority: "high" }] }) },
        pool: null,
      },
      [liveTask({ id: "c-1", priority: "urgent" })],
    );
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.queryByText("urgent")).not.toBeInTheDocument();
  });
});

describe("TeamBoard company filter", () => {
  it("hides enriched cards that do not match the selected company but keeps unenriched cards", async () => {
    renderBoard(
      {
        buckets: {
          emp_owner: bucket({
            ready: [
              { id: "acme-card", title: "Acme task", ref: null, status: "open", size: "M" },
              { id: "other-card", title: "Other task", ref: null, status: "open", size: "M" },
              { id: "unenriched-card", title: "Unenriched task", ref: null, status: "open", size: "M" },
            ],
          }),
        },
        pool: null,
      },
      [
        liveTask({ id: "acme-card", org: { organization_context: { company_slug: "acme" } } }),
        liveTask({ id: "other-card", org: { organization_context: { company_slug: "beta" } } }),
      ],
    );

    expect(screen.getByText("Acme task")).toBeInTheDocument();
    expect(screen.getByText("Other task")).toBeInTheDocument();
    expect(screen.getByText("Unenriched task")).toBeInTheDocument();

    await selectOption("Company", "acme");

    expect(screen.getByText("Acme task")).toBeInTheDocument();
    expect(screen.queryByText("Other task")).not.toBeInTheDocument();
    expect(screen.getByText("Unenriched task")).toBeInTheDocument();
  });

  it("shows everything again once the filter is reset to All companies", async () => {
    renderBoard(
      {
        buckets: {
          emp_owner: bucket({
            ready: [
              { id: "acme-card", title: "Acme task", ref: null, status: "open", size: "M" },
              { id: "other-card", title: "Other task", ref: null, status: "open", size: "M" },
            ],
          }),
        },
        pool: null,
      },
      [
        liveTask({ id: "acme-card", org: { organization_context: { company_slug: "acme" } } }),
        liveTask({ id: "other-card", org: { organization_context: { company_slug: "beta" } } }),
      ],
    );

    await selectOption("Company", "acme");
    expect(screen.queryByText("Other task")).not.toBeInTheDocument();

    await selectOption("Company", "All companies");
    expect(screen.getByText("Acme task")).toBeInTheDocument();
    expect(screen.getByText("Other task")).toBeInTheDocument();
  });
});

describe("TeamBoard company chip on the card face (2026-08-13 widening)", () => {
  it("renders the company name from the server-sent card fields", () => {
    renderBoard({
      buckets: {},
      pool: {
        cards: [{ id: "p-1", title: "Pool task", ref: null, status: "open", size: "M", company_slug: "acmeuni", company_name: "AcmeUni" }],
        depth: 1,
        low: false,
      },
    });
    expect(screen.getByTitle("Company")).toHaveTextContent("AcmeUni");
  });

  it("falls back to the slug when the server sends no company_name", () => {
    renderBoard({
      buckets: {},
      pool: {
        cards: [{ id: "p-1", title: "Pool task", ref: null, status: "open", size: "M", company_slug: "globex" }],
        depth: 1,
        low: false,
      },
    });
    expect(screen.getByTitle("Company")).toHaveTextContent("globex");
  });

  it("renders no company chip for a card an un-upgraded server sent", () => {
    renderBoard({
      buckets: {},
      pool: {
        cards: [{ id: "p-1", title: "Pool task", ref: null, status: "open", size: "M" }],
        depth: 1,
        low: false,
      },
    });
    expect(screen.queryByTitle("Company")).not.toBeInTheDocument();
  });
});

describe("TeamBoard owner name on Pool and Agents & unowned cards", () => {
  it("names the owner on a pool card face", () => {
    renderBoard({
      buckets: {},
      pool: {
        cards: [{ id: "p-1", title: "Pool task", ref: null, status: "open", size: "M", owner_employee_id: "emp_alice" }],
        depth: 1,
        low: false,
      },
    });
    expect(screen.getByTitle("Owner: Alice")).toHaveTextContent("Alice");
  });

  it("does NOT name the owner on a person section's own cards (the heading already does)", () => {
    renderBoard({
      buckets: {
        emp_owner: bucket({
          ready: [{ id: "c-1", title: "the operator's card", ref: null, status: "open", size: "M", owner_employee_id: "emp_owner" }],
        }),
      },
      pool: null,
    });
    expect(screen.getByText("the operator's card")).toBeInTheDocument();
    expect(screen.queryByTitle("Owner: the operator")).not.toBeInTheDocument();
  });

  it("names the owner on an Agents & unowned card from another employee's server bucket", () => {
    renderBoard({
      buckets: {
        emp_frank: bucket({
          employee_id: "emp_frank",
          ready: [{ id: "frank-1", title: "Frank's card", ref: null, status: "open", size: "M", owner_employee_id: "emp_frank" }],
          counts: { ready: 1, active: 0, blocked: 0, review: 0, done_today: 0 },
        }),
      },
      pool: null,
    });
    fireEvent.click(screen.getByRole("button", { name: /Agents & unowned/ }));
    expect(screen.getByTitle("Owner: Frank")).toHaveTextContent("Frank");
  });
});
