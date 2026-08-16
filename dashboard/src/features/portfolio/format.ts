/** Pure display helpers for the portfolio screen. */

import type { BadgeTone, StatusDotState } from "@/design";
import type { PortfolioState } from "./types";

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

export function formatMoney(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? USD.format(value) : "—";
}

/** Compact relative time for the queue column: "now" / "41m" / "2h" / "1d". */
export function compactRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "—";
  if (delta < 60_000) return "now";
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** Full relative string for tooltips. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "No activity";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "No activity";
  if (delta < 60_000) return "Just now";
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function isPortfolioState(value: string): value is PortfolioState {
  return (
    value === "blocked" ||
    value === "failing" ||
    value === "running" ||
    value === "idle" ||
    value === "healthy"
  );
}

/** Map portfolio state → StatusDot (no new states; reuse design system). */
export function stateToDot(state: string): StatusDotState {
  switch (state) {
    case "blocked":
      return "awaiting";
    case "failing":
      return "failed";
    case "running":
      return "running";
    case "healthy":
      return "ok";
    case "idle":
    default:
      return "queued";
  }
}

/** Map portfolio state → Badge tone for count chips. */
export function stateToBadgeTone(state: string): BadgeTone {
  switch (state) {
    case "blocked":
      return "awaiting";
    case "failing":
      return "failed";
    case "running":
      return "running";
    case "healthy":
      return "ok";
    case "idle":
    default:
      return "neutral";
  }
}

/** CSS modifier for severity stripe / band colour. */
export function stateStripeClass(state: string): "block" | "fail" | "run" | "ok" | "idle" {
  switch (state) {
    case "blocked":
      return "block";
    case "failing":
      return "fail";
    case "running":
      return "run";
    case "healthy":
      return "ok";
    case "idle":
    default:
      return "idle";
  }
}

/** Band headings for the attention queue. Idle + healthy share one band. */
export type QueueBand = {
  key: "blocked" | "failing" | "running" | "idle";
  title: string;
  needsAttention: boolean;
  states: ReadonlySet<string>;
};

export const QUEUE_BANDS: readonly QueueBand[] = [
  {
    key: "blocked",
    title: "Blocked — waiting on you",
    needsAttention: true,
    states: new Set(["blocked"]),
  },
  {
    key: "failing",
    title: "Failing",
    needsAttention: true,
    states: new Set(["failing"]),
  },
  {
    key: "running",
    title: "Running",
    needsAttention: false,
    states: new Set(["running"]),
  },
  {
    key: "idle",
    title: "Idle + Healthy",
    needsAttention: false,
    states: new Set(["idle", "healthy"]),
  },
] as const;

/** Breadcrumb excluding the leaf name (parent path only). */
export function parentBreadcrumb(path: string[] | undefined, name: string): string {
  if (!path || path.length === 0) return "";
  const withoutLeaf =
    path.length > 1 && path[path.length - 1] === name ? path.slice(0, -1) : path.slice(0, -1);
  return withoutLeaf.join(" / ");
}

/** Budget fill ratio 0–1, or null when uncapped / spend unknown. */
export function budgetRatio(
  spent: number | null,
  budget: number | null,
): number | null {
  if (spent == null || budget == null || !(budget > 0)) return null;
  return Math.min(1, Math.max(0, spent / budget));
}

export function budgetFillTone(ratio: number | null): "ok" | "warn" | "danger" | null {
  if (ratio == null) return null;
  if (ratio >= 0.9) return "danger";
  if (ratio >= 0.7) return "warn";
  return "ok";
}
