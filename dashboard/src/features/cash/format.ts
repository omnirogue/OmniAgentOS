/** Small, pure display helpers for the Cash page. Mirrors
 * features/revenue/format.ts's one-function-per-concern style. */

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

/** Money is always USD, 2dp; a negative amount keeps its leading minus — never
 * hide a negative net flow or a debit behind an unsigned number. */
export function formatMoney(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? USD.format(value) : "—";
}

/** Signed money with an explicit + for deposits, so a transaction's direction is
 * legible at a glance in the largest-transactions table. */
export function formatSignedMoney(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value > 0 ? `+${USD.format(value)}` : USD.format(value);
}

/** `generated_at` / `posted_at` are ISO instants; the page labels the day
 * "America/New_York", so render timestamps in that zone regardless of the
 * viewer's local timezone. */
export function formatEasternTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(parsed);
}

/** Compact ET timestamp for dense table cells (no seconds/year). */
export function formatPostedAt(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

export function formatDay(value: string | null | undefined): string {
  if (!value) return "—";
  // day is YYYY-MM-DD (a calendar date, not an instant) — parse as UTC noon so
  // no local-timezone shift can roll it to the adjacent day.
  const parsed = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric", year: "numeric" }).format(
    parsed,
  );
}
