/** Small, pure display helpers for the Revenue page. Mirrors
 * features/steward/format.ts / features/experiments/format.ts's
 * one-function-per-concern style. */

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

/** Money is always USD, 2dp, with a leading sign for negatives — never hide
 * a negative gross contribution behind an unsigned number. */
export function formatMoney(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? USD.format(value) : "—";
}

/** ROAS is revenue / spend — undefined (not "infinite") when spend is 0.
 * Also used for `blended_roas` (real revenue ÷ ad spend), which shares the
 * same "N.NNx" presentation as the Meta-attributed `roas` field — the
 * distinction between the two lives in the label, not the format. */
export function formatRoas(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}x` : "—";
}

/** `roi` is a ratio ((revenue − ad spend) / ad spend; 0.5 == +50%) — always
 * signed so a reader never mistakes a positive return for a raw share, and
 * "—" (not "0%") when there is no ad spend to divide by. */
export function formatRoiPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const pct = Math.round(value * 100);
  return `${pct > 0 ? "+" : ""}${pct.toLocaleString("en-US")}%`;
}

/** `generated_at` is an ISO instant; the page always labels it explicitly
 * "America/New_York" next to this, so render it in that zone regardless of
 * the viewer's local timezone — a 2am-ET reader must see 2am-ET, not their
 * browser's offset. */
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
