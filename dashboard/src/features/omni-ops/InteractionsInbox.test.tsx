import { render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { OMNIAGENTOS_INTERACTION_STATUSES } from "./types";
import type { GrokInteraction } from "./types";

const interactionsMock = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    grokOpsApi: {
      ...actual.grokOpsApi,
      interactions: (filters?: unknown) => interactionsMock(filters),
    },
  };
});

import { InteractionsInbox } from "./InteractionsInbox";

/** The exact row shape `GET /api/grok/interactions` returns — captured by
 * executing `InteractionsStore.create(...)` then `list_pending(...)` against a
 * real SQLite store, not hand-written. `status` is `active`: `list_pending`
 * hard-codes `WHERE status = 'active'`, so it is the ONLY status this endpoint
 * can ever emit. */
function pendingInteraction(overrides: Partial<GrokInteraction> = {}): GrokInteraction {
  return {
    id: "ixn_f02be89e7f4a4ce2b24f56eab7d2d48a",
    work_ref_type: "task",
    work_ref_id: "tsk_1",
    direction: "agent_to_user",
    kind: "question",
    body: "Ship the migration to prod now?",
    blocking_policy: "wait",
    status: "active",
    session_id: "ses_1",
    author: "agent",
    parent_id: null,
    expires_at: null,
    created_at: "2026-08-09T06:00:00Z",
    delivered_at: null,
    metadata: {},
    ...overrides,
  };
}

describe("InteractionsInbox — the operator must be able to answer what blocks an agent", () => {
  it("offers Reply for an undelivered blocking question, the only shape this endpoint returns", async () => {
    interactionsMock.mockResolvedValue([pendingInteraction()]);

    render(<InteractionsInbox />);

    await waitFor(() => expect(screen.getByText("Ship the migration to prod now?")).toBeTruthy());
    // The backend accepts this exact row: `answer` updates
    // `WHERE id = ? AND status IN ('active', 'delivered')`.
    expect(screen.queryByRole("button", { name: "Reply" })).not.toBeNull();
  });

  it("offers Reply once the agent has picked the interaction up", async () => {
    interactionsMock.mockResolvedValue([
      pendingInteraction({ status: "delivered", delivered_at: "2026-08-09T06:01:00Z" }),
    ]);

    render(<InteractionsInbox />);

    await waitFor(() => expect(screen.getByText("Ship the migration to prod now?")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Reply" })).not.toBeNull();
  });

  it("does not offer Reply for a status the store would refuse to answer", async () => {
    interactionsMock.mockResolvedValue([pendingInteraction({ status: "answered" })]);

    render(<InteractionsInbox />);

    await waitFor(() => expect(screen.getByText("Ship the migration to prod now?")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Reply" })).toBeNull();
  });
});

describe("OMNIAGENTOS_INTERACTION_STATUSES is the schema's union, not a hand-copied list", () => {
  /** Globs the call sites instead of listing them: if a migration ever widens
   * the `agent_interactions.status` CHECK, this fails until the client union —
   * and therefore every allowlist built on it — is widened with it. A
   * hand-written list of five has the same failure mode as the denylist that
   * caused this bug. */
  it("matches the CHECK constraint in the migration that defines the column", () => {
    const migration = readFileSync(
      path.resolve(__dirname, "../../../../omniagentos/db/migrations/058_execution_contract.sql"),
      "utf8",
    );
    const check = /status\s+TEXT NOT NULL DEFAULT 'active'\s*\n?\s*CHECK \(status IN \(([^)]*)\)\)/.exec(
      migration,
    );
    expect(check).not.toBeNull();
    const fromSchema = (check as RegExpExecArray)[1]
      .split(",")
      .map((s) => s.trim().replace(/^'|'$/g, ""));
    expect([...OMNIAGENTOS_INTERACTION_STATUSES].sort()).toEqual([...fromSchema].sort());
  });
});

// D-2 (LiveSim LS-004) audit find: `interactions` is never cleared on a
// failed refresh, so "Pending interactions: 0" / "Blocking: 0" only means
// "confirmed empty" when `error` is unset -- this inbox carries the same
// invisible-backlog stakes as Approvals.
describe("InteractionsInbox stats vs. an unknown fetch (D-2)", () => {
  it("shows an unknown state, never a confident 0, when interactions have never loaded and the fetch failed", async () => {
    interactionsMock.mockRejectedValue(new Error("network unreachable"));

    render(<InteractionsInbox />);

    await waitFor(() => {
      expect(screen.getByText("Interactions unavailable")).toBeInTheDocument();
    });
    expect(screen.getAllByText("—", { selector: ".ds-stat__value" })).toHaveLength(2);
  });

  it("still shows a real 0 pending/blocking and 'No pending interactions' when the fetch genuinely succeeded empty", async () => {
    interactionsMock.mockResolvedValue([]);

    render(<InteractionsInbox />);

    await waitFor(() => {
      expect(screen.getByText("No pending interactions")).toBeInTheDocument();
    });
    expect(screen.getAllByText("0", { selector: ".ds-stat__value" })).toHaveLength(2);
  });

  it("keeps the real blocking count when a later refresh errors after a successful load (stale beats hidden)", async () => {
    interactionsMock.mockResolvedValue([
      pendingInteraction({ blocking_policy: "block_until_answered" }),
    ]);

    render(<InteractionsInbox />);

    await waitFor(() => {
      expect(screen.getByText("Ship the migration to prod now?")).toBeInTheDocument();
    });
    expect(screen.getAllByText("1", { selector: ".ds-stat__value" })).toHaveLength(2);

    interactionsMock.mockRejectedValue(new Error("network unreachable"));
    screen.getByRole("button", { name: "Refresh" }).click();

    await waitFor(() => {
      expect(screen.getByText("Interactions unavailable")).toBeInTheDocument();
    });
    expect(screen.queryAllByText("—", { selector: ".ds-stat__value" })).toHaveLength(0);
  });
});
