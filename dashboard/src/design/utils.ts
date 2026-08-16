import { dataviz } from "./tokens";

/** Class-name helper — joins truthy string values. */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Read a CSS custom property from the document element. */
export function cssVar(name: string, fallback = ""): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** First n colors from the categorical dataviz ramp. */
export function chartSeriesColors(n: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < n; i++) {
    out.push(dataviz.categorical[i % dataviz.categorical.length]!);
  }
  return out;
}

/** Sequential ramp color for index i of total. */
export function sequentialColor(i: number, total: number): string {
  if (total <= 1) return dataviz.sequential[dataviz.sequential.length - 1]!;
  const t = Math.max(0, Math.min(1, i / (total - 1)));
  const idx = Math.round(t * (dataviz.sequential.length - 1));
  return dataviz.sequential[idx]!;
}
