import { describe, expect, it } from "vitest";
import { parseTeamBoard } from "./hooks";

describe("parseTeamBoard", () => {
  it.each([
    { pool: { depth: 2, low: false }, name: "missing cards" },
    { pool: { cards: null, depth: 2, low: false }, name: "null cards" },
    { pool: "pool", name: "truthy non-object" },
    { pool: null, name: "explicit null" },
  ])("degrades malformed $name pool to null", (raw) => {
    expect(parseTeamBoard(raw).pool).toBeNull();
  });

  it("accepts the upgraded pool and filters buckets by shape", () => {
    const result = parseTeamBoard({
      pool: { cards: [], depth: 51, low: true, truncated: true },
      emp_owner: { ready: [], active: [], blocked: [], review: [], done_today: [], counts: {} },
      ignored: { active: [] },
    });
    expect(result.pool).toEqual({ cards: [], depth: 51, low: true, truncated: true });
    expect(Object.keys(result.buckets)).toEqual(["emp_owner"]);
  });

  it("drops pool cards without string ids and titles", () => {
    const result = parseTeamBoard({
      pool: {
        cards: [
          { id: "kept", title: "Kept" },
          { id: "missing-title" },
          { title: "missing-id" },
          "not a card",
        ],
        depth: 4,
        low: false,
      },
    });

    expect(result.pool?.cards).toEqual([{ id: "kept", title: "Kept" }]);
  });

  it("passes through the additive per-card fields when well-typed (2026-08-13 widening)", () => {
    const result = parseTeamBoard({
      pool: {
        cards: [{
          id: "c1", title: "Card", ref: "REF-1", status: "open", size: "M",
          owner_employee_id: "emp_alice", priority: "urgent",
          company_slug: "acmeuni", company_name: "AcmeUni",
        }],
        depth: 1,
        low: false,
      },
    });
    expect(result.pool?.cards).toEqual([{
      id: "c1", title: "Card", ref: "REF-1", status: "open", size: "M",
      owner_employee_id: "emp_alice", priority: "urgent",
      company_slug: "acmeuni", company_name: "AcmeUni",
    }]);
  });

  it("drops wrong-typed additive fields so a bad payload degrades like an old server", () => {
    const result = parseTeamBoard({
      pool: {
        cards: [{
          id: "c1", title: "Card",
          owner_employee_id: null, priority: 3, company_slug: {}, company_name: undefined,
        }],
        depth: 1,
        low: false,
      },
    });
    expect(result.pool?.cards).toEqual([{ id: "c1", title: "Card" }]);
  });

  it("tolerates an un-upgraded server that omits the additive fields entirely", () => {
    const result = parseTeamBoard({
      pool: { cards: [{ id: "c1", title: "Card", ref: null, status: "open", size: "S" }], depth: 1, low: false },
    });
    expect(result.pool?.cards).toEqual([{ id: "c1", title: "Card", ref: null, status: "open", size: "S" }]);
    expect(result.pool?.cards[0]?.company_slug).toBeUndefined();
    expect(result.pool?.cards[0]?.priority).toBeUndefined();
  });

  it.each([
    { name: "missing counts", value: { ready: [], active: [], blocked: [], review: [], done_today: [] } },
    { name: "missing active", value: { ready: [], blocked: [], review: [], done_today: [], counts: {} } },
    { name: "missing blocked", value: { ready: [], active: [], review: [], done_today: [], counts: {} } },
    { name: "missing review", value: { ready: [], active: [], blocked: [], done_today: [], counts: {} } },
    { name: "missing done_today", value: { ready: [], active: [], blocked: [], review: [], counts: {} } },
  ])("drops buckets with $name", ({ value }) => {
    expect(parseTeamBoard({ emp_owner: value }).buckets).toEqual({});
  });
});
