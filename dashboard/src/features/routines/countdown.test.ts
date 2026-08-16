import { describe, expect, it } from "vitest";
import {
  formatCountdown,
  msUntilNextFire,
  nextCronFire,
  parseCron,
} from "./countdown";

describe("parseCron", () => {
  it("accepts the canonical daily-at-3am expression", () => {
    expect(parseCron("0 3 * * *")).not.toBeNull();
  });

  it("rejects anything that isn't a 5-field cron", () => {
    expect(parseCron("@daily")).toBeNull();
    expect(parseCron("0 3 * *")).toBeNull();
    expect(parseCron("0 3 * * * *")).toBeNull();
    expect(parseCron("")).toBeNull();
  });

  it("rejects out-of-range values", () => {
    expect(parseCron("60 * * * *")).toBeNull();
    expect(parseCron("* 25 * * *")).toBeNull();
    expect(parseCron("* * 0 * *")).toBeNull();
    expect(parseCron("* * * 13 *")).toBeNull();
    expect(parseCron("* * * * 7")).toBeNull();
  });

  it("parses lists, ranges, and steps", () => {
    const a = parseCron("0,15,30,45 * * * *")!;
    expect(a.minute.values).toEqual([0, 15, 30, 45]);
    const b = parseCron("0 9-17 * * 1-5")!;
    expect(b.hour.values).toEqual([9, 10, 11, 12, 13, 14, 15, 16, 17]);
    expect(b.dow.values).toEqual([1, 2, 3, 4, 5]);
    const c = parseCron("*/15 * * * *")!;
    expect(c.minute.values).toEqual([0, 15, 30, 45]);
  });
});

describe("nextCronFire", () => {
  it("returns the next matching minute, never the input itself", () => {
    const from = new Date("2025-06-01T03:00:00Z");
    const next = nextCronFire("0 3 * * *", from)!;
    expect(next.toISOString()).toBe("2025-06-02T03:00:00.000Z");
  });

  it("skips to the next matching minute within the same hour for */5", () => {
    const from = new Date("2025-06-01T10:07:00Z");
    const next = nextCronFire("*/15 * * * *", from)!;
    expect(next.getUTCMinutes()).toBe(15);
    expect(next.getUTCHours()).toBe(10);
  });

  it("rolls across month boundaries", () => {
    const from = new Date("2025-01-31T23:59:00Z");
    const next = nextCronFire("0 0 1 * *", from)!;
    expect(next.toISOString()).toBe("2025-02-01T00:00:00.000Z");
  });

  it("returns null for invalid crons", () => {
    expect(nextCronFire("@daily")).toBeNull();
  });

  it("respects the day-of-month constraint across months", () => {
    // Feb 30 never exists; the search should skip Feb, land on Mar 30.
    const from = new Date("2025-02-20T00:00:00Z");
    const next = nextCronFire("0 0 30 * *", from)!;
    expect(next.getUTCMonth()).toBe(2); // March
    expect(next.getUTCDate()).toBe(30);
  });
});

describe("msUntilNextFire", () => {
  it("returns null for unparseable crons (renders — on the UI)", () => {
    expect(msUntilNextFire("not-a-cron")).toBeNull();
  });

  it("returns a positive duration", () => {
    const from = new Date("2025-06-01T00:00:00Z");
    const ms = msUntilNextFire("0 3 * * *", from)!;
    expect(ms).toBe(3 * 60 * 60 * 1000);
  });
});

describe("formatCountdown", () => {
  it("returns 'event' for event-triggered routines", () => {
    expect(formatCountdown("event", { event: "goal.metric" })).toBe("event");
  });

  it("returns '—' when the cron expression is missing", () => {
    expect(formatCountdown("cron", {})).toBe("—");
  });

  it("formats day-scale countdowns", () => {
    // A daily cron's next fire is always <24h away, so day-scale output needs
    // a wider-spaced schedule: Mondays 03:00 UTC. 2025-06-02 is a Monday, so
    // one second after the fire the next is 6d 23h 59m 59s out.
    const from = new Date("2025-06-02T03:00:01Z");
    const label = formatCountdown("cron", { cron: "0 3 * * 1" }, from);
    expect(label).toMatch(/^in \d+d/);
  });

  it("formats hour-scale countdowns", () => {
    const from = new Date("2025-06-02T01:00:00Z");
    const label = formatCountdown("cron", { cron: "0 3 * * *" }, from);
    expect(label).toMatch(/^in 1h \d+m|^in 2h/);
  });

  it("formats minute-scale countdowns", () => {
    const from = new Date("2025-06-02T02:55:00Z");
    const label = formatCountdown("cron", { cron: "0 3 * * *" }, from);
    expect(label).toMatch(/^in \d+m/);
  });
});
