import { describe, expect, it } from "vitest";
import { summarizeQueue } from "../parsers/parseQueue";

describe("summarizeQueue", () => {
  it("passes through the healthy top-level fields", () => {
    expect(
      summarizeQueue({
        wip: 13,
        wip_cap: 60,
        wip_degraded: false,
        wip_degraded_detail: "",
        rebuilt_at: "2026-08-14T08:41:10Z",
        items: [{ huge: "ignored" }],
      }),
    ).toEqual({
      wip: 13,
      wip_cap: 60,
      wip_degraded: false,
      wip_degraded_detail: null,
      rebuilt_at: "2026-08-14T08:41:10Z",
    });
  });

  it("NEVER defaults a missing wip to 0 — a degraded rebuild omits it", () => {
    const result = summarizeQueue({
      wip_cap: 60,
      wip_degraded: true,
      wip_degraded_detail: "ledger torn tail; count withheld",
      rebuilt_at: "2026-08-14T08:41:10Z",
      // no `wip` key at all
    });
    expect(result.wip).toBeNull();
    expect(result.wip_degraded).toBe(true);
    expect(result.wip_degraded_detail).toBe("ledger torn tail; count withheld");
  });

  it("treats a non-numeric wip the same as absent", () => {
    expect(summarizeQueue({ wip: "13" }).wip).toBeNull();
  });

  it("handles a malformed/non-object payload without throwing", () => {
    expect(summarizeQueue(null)).toEqual({
      wip: null,
      wip_cap: null,
      wip_degraded: false,
      wip_degraded_detail: null,
      rebuilt_at: null,
    });
    expect(summarizeQueue("not an object")).toEqual({
      wip: null,
      wip_cap: null,
      wip_degraded: false,
      wip_degraded_detail: null,
      rebuilt_at: null,
    });
  });
});
