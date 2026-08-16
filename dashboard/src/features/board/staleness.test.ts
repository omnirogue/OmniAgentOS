import { describe, expect, it } from "vitest";
import { taskStaleness } from "./staleness";

const NOW = new Date("2026-08-03T12:00:00.000Z");

describe("taskStaleness", () => {
  it.each([
    ["0 days old", "2026-08-03T12:00:00.000Z", { days: 0, stale: false }],
    ["6 days old", "2026-07-28T12:00:00.000Z", { days: 6, stale: false }],
    ["7 days old", "2026-07-27T12:00:00.000Z", { days: 7, stale: true }],
    ["30 days old", "2026-07-04T12:00:00.000Z", { days: 30, stale: true }],
    ["an invalid date", "not-an-iso-date", { days: 0, stale: false }],
  ])("handles %s", (_label, updatedAt, expected) => {
    expect(taskStaleness(updatedAt, NOW)).toEqual(expected);
  });
});
