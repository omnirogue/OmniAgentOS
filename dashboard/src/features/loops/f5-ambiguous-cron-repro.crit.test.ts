/**
 * Copied (paths adapted to this repo's alias layout) from the codex-critic
 * repro fixture for round-3 finding f5 (regression in the round-2 cron
 * cadence fix):
 * .fusion/runs/crit-20260815T114756Z-6436-8775/repro/f5-ambiguous-cron-repro.test.ts
 *
 * The round-2 cron-cadence parser accepted ANY non-wildcard weekday field as
 * a single "once a week" day — including ranges ("1-5"), lists ("1,3,5"),
 * and a step value, which the backend's cron grammar
 * (omniagentos/scheduler/routines.py:916) genuinely serializes. Those are
 * ambiguous cadences (fire on 1 to 7 days a week) and must resolve to
 * unknown/Infinity (sorts last), never a confident 604800s "weekly".
 */
import { expect, it } from "vitest";

import { effectiveCadenceSeconds, parseScheduleDescriptionSeconds, sortJobs } from "./filterSort";

it.each([
  ["daily 00:30 local", 86_400],
  ["twice daily 03:30 + 15:30 local", 43_200],
  ["3x daily 03:40 + 11:40 + 19:40 local", 86_400 / 3],
  ["10x daily 08:00 + 09:00 + 10:00 + 11:00 + 12:00 + 13:00 + 14:00 + 15:00 + 17:00 + 18:00 local", 86_400 / 10],
  ["11x daily 08:05 + 09:05 + 10:05 + 11:05 + 12:05 + 13:05 + 14:05 + 15:05 + 16:05 + 17:05 + 18:05 local", 86_400 / 11],
  ["Sun 09:00 local", 604_800],
  ["cron 10 6 * * * — UTC", 86_400],
  ["cron 25 6 * * * — UTC", 86_400],
  ["cron */5 * * * * — UTC", 300],
  ["cron 0 8,20 * * * — UTC", null],
  ["cron 7 */6 * * * — UTC", null],
] as const)("matches a schedule-description shape in the current backend inventory: %s", (description, expected) => {
  expect(parseScheduleDescriptionSeconds(description)).toBe(expected);
});

function job(key: string, description: string) {
  return {
    key,
    name: key,
    executor: "remote_cron",
    category: "Remote",
    label: null,
    purpose: "repro",
    source: "repro",
    module: null,
    schedule: { kind: "cron" as const, seconds: null, description },
    env_overrides: [],
    loaded: null,
    plist_present: false,
    last_exit_status: null,
    last_run_at: null,
    next_fire_at: "2026-08-15T12:00:01Z",
    health: "unknown" as const,
    health_reason: "repro",
    managed_candidate: false,
    candidate_reason: "",
  };
}

it.each([
  "cron 0 3 * * 1-5",
  "cron 0 3 * * 1,3,5",
  "cron 0 3 * * */2",
])("routes ambiguous weekday syntax to unknown: %s", (description) => {
  expect.soft(parseScheduleDescriptionSeconds(description)).toBeNull();
  expect.soft(effectiveCadenceSeconds(job("ambiguous", description))).toBe(
    Number.POSITIVE_INFINITY,
  );
});

it("sorts ambiguous cron after a confidently parsed weekly cron", () => {
  const ambiguous = job("ambiguous-weekdays", "cron 0 3 * * 1-5");
  const knownWeekly = job("known-weekly", "cron 0 3 * * 1");

  expect(sortJobs([ambiguous, knownWeekly], "schedule").map((item) => item.key)).toEqual([
    "known-weekly",
    "ambiguous-weekdays",
  ]);
});
