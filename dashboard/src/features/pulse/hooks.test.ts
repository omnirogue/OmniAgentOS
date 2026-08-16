import { describe, expect, it } from "vitest";
import { totalDelta } from "./hooks";
import type { DeltaResponse } from "./types";

function delta(overrides: Partial<DeltaResponse> = {}): DeltaResponse {
  return {
    since: "2026-07-26T00:00:00Z",
    skills_updated: 0,
    improvements_decided: 0,
    loops_run: 0,
    tasks_completed: 0,
    chats_active: 0,
    ...overrides,
  };
}

describe("totalDelta", () => {
  it("sums all counters", () => {
    expect(
      totalDelta(delta({
        skills_updated: 2,
        improvements_decided: 3,
        loops_run: 5,
        tasks_completed: 1,
        chats_active: 4,
      })),
    ).toBe(15);
  });

  it("returns 0 when every counter is 0 (no activity)", () => {
    expect(totalDelta(delta())).toBe(0);
  });
});
