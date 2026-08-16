import type { ExperimentStatus } from "./types";

export function labelize(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatDelta(value: number | null | undefined, digits = 1): string {
  if (value == null) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(digits)}%`;
}

export function formatNumber(value: number | null | undefined, digits = 3): string {
  return value == null ? "—" : value.toFixed(digits);
}

export function formatMoney(value: number | null | undefined): string {
  return value == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
}

export function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

export function statusTone(status: ExperimentStatus) {
  if (status === "failed") return "failed" as const;
  if (status === "cancelled") return "cancelled" as const;
  if (status === "promoted") return "completed" as const;
  if (status === "decided" || status === "rolled_back") return "completed" as const;
  if (status === "proposed") return "queued" as const;
  if (status === "canary") return "validating" as const;
  return "running" as const;
}

export function scoreMetric(record: Record<string, number> | undefined, names: string[]): number | null {
  if (!record) return null;
  for (const name of names) {
    if (typeof record[name] === "number") return record[name];
  }
  return null;
}
