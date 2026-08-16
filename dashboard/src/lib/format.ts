/**
 * Central display formatters for low-level machine values and relative times.
 * Guided by docs/architecture/dashboard-conventions.md Rule 3.
 */

/** Formats a relative time string, e.g. "5m ago" or "—". */
export function formatRelativeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  const seconds = Math.max(0, Math.floor((now.getTime() - then.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Formats a countdown time string, e.g. "in 5m" or "due now". */
export function formatCountdownTo(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  const seconds = Math.floor((then.getTime() - now.getTime()) / 1000);
  if (seconds <= 0) return "due now";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.ceil(minutes / 60);
  if (hours < 24) return `in ${hours}h`;
  const days = Math.ceil(hours / 24);
  return `in ${days}d`;
}

/** Severity order mapping for system health: lowest number is worst. */
export function healthSeverity(health: string): number {
  switch (health) {
    case "failing":
      return 0;
    case "stale":
      return 1;
    case "unknown":
      return 2;
    case "not_loaded":
      return 3;
    case "healthy":
      return 4;
    default:
      return 5;
  }
}

/** Formats a hexadecimal hash or commit SHA to a short representation. */
export function formatSha(sha: string | null | undefined, len = 8): string {
  if (!sha) return "—";
  return sha.slice(0, len);
}

/** Formats machine bytes into a human-readable string. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  const gb = mb / 1024;
  return `${gb.toFixed(1)} GB`;
}

/** Formats memory or CPU utilization to a percentage string. */
export function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/** Formats a plain machine count (CPU cores, slots, throughput) — "—" when
 * unmeasured or non-finite, else the plain integer string. Guided by Rule 3:
 * low-level machine metrics render through central formatters, never
 * formatted inline in a component. */
export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(value);
}

/** Formats session activity relative time. */
export function formatSessionActivity(iso: string | null): string {
  if (!iso) return "No activity recorded";
  const minutes = Math.floor(Math.max(0, Date.now() - Date.parse(iso)) / 60_000);
  return minutes < 1 ? "Active now" : minutes < 60 ? `${minutes}m ago` : `${Math.floor(minutes / 60)}h ago`;
}

/** Formats a numeric USD value to a cost string. */
export function formatCost(value: number | null): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

/**
 * Formats a date-only value (``YYYY-MM-DD``, optionally with a trailing
 * time/offset that is ignored) as a locale date — WITHOUT going through
 * `new Date(dateOnlyString)`. A bare `YYYY-MM-DD` parses as UTC midnight per
 * the ISO 8601 spec, so `.toLocaleDateString()` on it renders the PREVIOUS
 * calendar day in any negative-UTC-offset timezone. Parses the y/m/d digits
 * directly and builds a local `Date` from them instead.
 */
export function formatDateOnly(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(dateStr);
  if (!m) return "—";
  const [, y, mo, d] = m;
  const local = new Date(Number(y), Number(mo) - 1, Number(d));
  if (Number.isNaN(local.getTime())) return "—";
  return local.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
