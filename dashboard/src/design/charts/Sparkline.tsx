"use client";

import { useMemo } from "react";
import { dataviz } from "../tokens";
import { cx } from "../utils";
import { Empty } from "./Empty";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

export type SparklineProps = {
  points: number[];
  width?: number;
  height?: number;
  color?: string;
  tone?: "accent" | "ok" | "danger" | "warn" | "promote" | "reject";
  fill?: boolean;
  status?: "ready" | "loading" | "empty" | "error";
  errorMessage?: string;
  onRetry?: () => void;
  className?: string;
  "aria-label"?: string;
};

const TONE_COLOR: Record<string, string> = {
  accent: "var(--accent)",
  ok: "var(--ok)",
  danger: "var(--danger)",
  warn: "var(--warn)",
  promote: "var(--promote)",
  reject: "var(--reject)",
};

export function Sparkline({
  points,
  width = 120,
  height = 32,
  color,
  tone = "accent",
  fill = true,
  status = "ready",
  errorMessage,
  onRetry,
  className,
  "aria-label": ariaLabel = "Sparkline",
}: SparklineProps) {
  const stroke = color ?? TONE_COLOR[tone] ?? dataviz.categorical[0]!;

  const path = useMemo(() => {
    if (points.length < 2) return null;
    const min = Math.min(...points);
    const max = Math.max(...points);
    const span = max - min || 1;
    const pad = 2;
    const w = width - pad * 2;
    const h = height - pad * 2;
    const coords = points.map((y, i) => {
      const x = pad + (i / (points.length - 1)) * w;
      const py = pad + h - ((y - min) / span) * h;
      return [x, py] as const;
    });
    const line = coords
      .map((c, i) => `${i === 0 ? "M" : "L"}${c[0].toFixed(2)},${c[1].toFixed(2)}`)
      .join(" ");
    const area = `${line} L${coords[coords.length - 1]![0].toFixed(2)},${(height - pad).toFixed(2)} L${coords[0]![0].toFixed(2)},${(height - pad).toFixed(2)} Z`;
    return { line, area };
  }, [points, width, height]);

  if (status === "loading") return <Loading label="…" className={className} />;
  if (status === "error") {
    return <ErrorState message={errorMessage ?? "Error"} onRetry={onRetry} className={className} />;
  }
  if (status === "empty" || points.length < 2 || !path) {
    return <Empty message="—" className={className} />;
  }

  return (
    <svg
      className={cx("ds-chart__svg", className)}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel}
    >
      {fill ? <path d={path.area} fill={stroke} opacity={0.18} stroke="none" /> : null}
      <path
        d={path.line}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
