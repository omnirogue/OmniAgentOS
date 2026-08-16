/** Small, pure display helpers for the Accounts page — same spirit as
 * features/routines/format.ts. */
import type { BadgeTone, StatusDotState } from "@/design";
import type { AccountAuthType, AccountCredits, AccountStatus, UsageSeverity } from "./types";

export function authTypeLabel(type: AccountAuthType): string {
  switch (type) {
    case "config_dir":
      return "Config directory";
    case "oauth_token":
      return "OAuth token";
    case "api_key":
      return "API key";
    default:
      return type;
  }
}

export function statusLabel(status: AccountStatus): string {
  switch (status) {
    case "ok":
      return "OK";
    case "unknown":
      return "Unknown";
    case "error":
      return "Error";
    case "rate_limited":
      return "Rate limited";
    default:
      return status;
  }
}

export function statusTone(status: AccountStatus): BadgeTone {
  switch (status) {
    case "ok":
      return "ok";
    case "error":
      return "danger";
    case "rate_limited":
      return "warn";
    case "unknown":
    default:
      return "neutral";
  }
}

export function statusDotState(status: AccountStatus): StatusDotState {
  switch (status) {
    case "ok":
      return "ok";
    case "error":
      return "danger";
    case "rate_limited":
      return "warn";
    case "unknown":
    default:
      return "queued";
  }
}

/** "5m ago" / "Never" — same style as the chat page's local `relativeTime`. */
export function relativeTime(iso: string | null): string {
  if (!iso) return "Never";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "Never";
  if (delta < 60_000) return "Just now";
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Full timestamp for a title/tooltip next to the relative time above. */
export function formatDateTime(value: string | null): string {
  if (!value) return "Never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/** Forward-looking counterpart to `relativeTime`: "in 3h" / "in 12m". */
export function untilTime(iso: string | null): string {
  if (!iso) return "—";
  const delta = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(delta)) return "—";
  if (delta <= 0) return "now";
  const minutes = Math.round(delta / 60_000);
  if (minutes < 60) return `in ${Math.max(1, minutes)}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `in ${hours}h`;
  return `in ${Math.floor(hours / 24)}d`;
}

/** Severity as reported by the provider — never re-derived from the percentage,
 * so our thresholds can't drift away from the ones the CLI itself stamps. */
export function usageTone(severity: UsageSeverity): BadgeTone {
  switch (severity) {
    case "critical":
      return "danger";
    case "warning":
      return "warn";
    case "normal":
    default:
      return "ok";
  }
}

export function formatPercent(percent: number): string {
  return `${Math.round(percent)}%`;
}

/** "12m old" — how far behind the CLI's own cache is. */
export function formatAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "age unknown";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m old`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h old`;
  return `${Math.floor(hours / 24)}d old`;
}

/** "$398.36 of $1,010.00" — amounts arrive pre-converted to major units. */
export function formatCredits(credits: AccountCredits | null): string | null {
  if (!credits) return null;
  if (credits.unlimited) return "Unlimited credits";
  if (credits.balance !== null && credits.used_amount === null) {
    return `Balance ${credits.balance}`;
  }
  if (credits.used_amount === null || credits.limit_amount === null) return null;
  const money = (value: number) =>
    new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: credits.currency ?? "USD",
    }).format(value);
  return `${money(credits.used_amount)} of ${money(credits.limit_amount)}`;
}
