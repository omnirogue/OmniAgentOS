/** Client-side cron-to-next-fire countdown.
 *
 * Parses the standard 5-field cron expression (``minute hour day month
 * dayOfWeek``) and returns the next :class:`Date` it fires at, given a
 * reference ``now``. Field supports: ``*``, specific values, lists
 * (``1,2,3``), ranges (``1-5``), and steps (``*\/15``, ``0-30/5``). Only
 * 5-field crons are handled; anything else (6-field / seconds, ``@reboot``,
 * named shortcuts) returns ``null``.
 *
 * Pure, synchronous, zero dependencies — designed to run cheaply inside a
 * ``useState`` / ``setInterval`` loop ticking once a second. Covered by
 * vitest in ``__tests__/countdown.test.ts``.
 */

type Field = { values: number[] };

const FIELD_BOUNDS: Array<[number, number]> = [
  [0, 59],  // minute
  [0, 23],  // hour
  [1, 31],  // day of month
  [1, 12],  // month
  [0, 6],   // day of week (Sunday = 0)
];

function parseField(token: string, min: number, max: number): Field | null {
  const values = new Set<number>();
  for (const part of token.split(",")) {
    const stepMatch = /^(.+)\/(\d+)$/.exec(part);
    let rangeToken = part;
    let step = 1;
    if (stepMatch) {
      rangeToken = stepMatch[1]!;
      step = Math.max(1, Number(stepMatch[2]));
      if (!Number.isFinite(step)) return null;
    }
    let start: number;
    let end: number;
    if (rangeToken === "*") {
      start = min;
      end = max;
    } else if (/^\d+$/.test(rangeToken)) {
      start = Number(rangeToken);
      end = start;
    } else {
      const range = /^(\d+)-(\d+)$/.exec(rangeToken);
      if (!range) return null;
      start = Number(range[1]);
      end = Number(range[2]);
    }
    if (start < min || start > max || end < min || end > max) return null;
    if (start > end) return null;
    for (let v = start; v <= end; v += step) values.add(v);
  }
  if (values.size === 0) return null;
  return { values: [...values].sort((a, b) => a - b) };
}

export type CronFields = {
  minute: Field;
  hour: Field;
  day: Field;
  month: Field;
  dow: Field;
};

/** Parse a 5-field cron expression; returns ``null`` on any unsupported
 * shape. Safe to call from the hot countdown path — no exceptions thrown. */
export function parseCron(expression: string): CronFields | null {
  const tokens = expression.trim().split(/\s+/);
  if (tokens.length !== 5) return null;
  const minute = parseField(tokens[0]!, FIELD_BOUNDS[0]![0], FIELD_BOUNDS[0]![1]);
  const hour = parseField(tokens[1]!, FIELD_BOUNDS[1]![0], FIELD_BOUNDS[1]![1]);
  const day = parseField(tokens[2]!, FIELD_BOUNDS[2]![0], FIELD_BOUNDS[2]![1]);
  const month = parseField(tokens[3]!, FIELD_BOUNDS[3]![0], FIELD_BOUNDS[3]![1]);
  const dow = parseField(tokens[4]!, FIELD_BOUNDS[4]![0], FIELD_BOUNDS[4]![1]);
  if (!minute || !hour || !day || !month || !dow) return null;
  return { minute, hour, day, month, dow };
}

/** Brute-force search for the next cron fire from ``from``, bounded by a
 * configurable horizon (default 366 days) so a never-matching expression
 * (e.g. ``0 0 31 2 *``) terminates quickly. Returns ``null`` if no match
 * within the horizon.
 *
 * All matching is done in UTC wall time, mirroring the backend scheduler:
 * ``omniagentos.scheduler.routines.cron_is_due`` matches the expression
 * against ``datetime.now(UTC)`` fields, so the countdown the dashboard
 * renders agrees with when the routine will actually fire. */
export function nextCronFire(
  expression: string,
  from: Date = new Date(),
  /* The longest realistic gap between fires for any valid cron is one year
   * plus a day of slack — beyond that the expression is effectively dead. */
  maxDaysAhead = 366,
): Date | null {
  const fields = parseCron(expression);
  if (!fields) return null;
  // Start one minute after ``from`` so a fire that just happened does not
  // immediately re-fire.
  const cursor = new Date(from.getTime());
  cursor.setUTCSeconds(0, 0);
  cursor.setUTCMinutes(cursor.getUTCMinutes() + 1);

  const horizonMs = from.getTime() + maxDaysAhead * 24 * 60 * 60 * 1000;
  let guard = 0;
  const MAX_ITER = 525_960; // one year of minutes

  while (cursor.getTime() < horizonMs && guard < MAX_ITER) {
    guard += 1;
    if (!fields.month.values.includes(cursor.getUTCMonth() + 1)) {
      cursor.setUTCMonth(cursor.getUTCMonth() + 1, 1);
      cursor.setUTCHours(0, 0, 0, 0);
      continue;
    }
    if (
      !fields.day.values.includes(cursor.getUTCDate()) ||
      !fields.dow.values.includes(cursor.getUTCDay())
    ) {
      cursor.setUTCDate(cursor.getUTCDate() + 1);
      cursor.setUTCHours(0, 0, 0, 0);
      continue;
    }
    if (!fields.hour.values.includes(cursor.getUTCHours())) {
      cursor.setUTCHours(cursor.getUTCHours() + 1, 0, 0, 0);
      continue;
    }
    if (!fields.minute.values.includes(cursor.getUTCMinutes())) {
      cursor.setUTCMinutes(cursor.getUTCMinutes() + 1, 0, 0);
      continue;
    }
    return new Date(cursor.getTime());
  }
  return null;
}

/** Milliseconds until the next cron fire from ``now``. Returns ``null`` if
 * the cron is unparseable or the horizon is exhausted (so the UI renders a
 * dash instead of a misleading zero). */
export function msUntilNextFire(
  expression: string,
  now: Date = new Date(),
): number | null {
  const next = nextCronFire(expression, now);
  if (!next) return null;
  const diff = next.getTime() - now.getTime();
  return diff > 0 ? diff : null;
}

/** Human-readable countdown for a routine's next fire.
 * ``"in 2h 14m"``, ``"in 3d 4h"``, or ``"—"`` when the cron is not
 * parseable / the routine is event-triggered / disabled. */
export function formatCountdown(
  triggerType: string,
  triggerConfig: Record<string, unknown>,
  now: Date = new Date(),
): string {
  if (triggerType !== "cron") return "event";
  const expr = typeof triggerConfig.cron === "string" ? triggerConfig.cron : null;
  if (!expr) return "—";
  const ms = msUntilNextFire(expr, now);
  if (ms === null) return "—";
  const totalSeconds = Math.floor(ms / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (days > 0) return `in ${days}d ${hours}h`;
  if (hours > 0) return `in ${hours}h ${minutes}m`;
  return `in ${minutes}m`;
}
