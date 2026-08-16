"use client";

import { useMemo, useState } from "react";
import { dataviz } from "../tokens";
import { cx } from "../utils";
import { Empty } from "./Empty";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";
import { Tooltip } from "./Tooltip";
import { CHART_THEME_VARS, useCssVars } from "./useCssVar";

export type LinePoint = {
  x: number | string;
  y: number;
  ciLow?: number;
  ciHigh?: number;
};

export type LineSeries = {
  name: string;
  points: LinePoint[];
  color?: string;
};

export type LineChartProps = {
  series: LineSeries[];
  height?: number;
  status?: "ready" | "loading" | "empty" | "error";
  errorMessage?: string;
  onRetry?: () => void;
  className?: string;
  "aria-label"?: string;
};

export function LineChart({
  series,
  height = 260,
  status = "ready",
  errorMessage,
  onRetry,
  className,
  "aria-label": ariaLabel = "Line chart",
}: LineChartProps) {
  const theme = useCssVars([...CHART_THEME_VARS]);
  const [hover, setHover] = useState<{
    xi: number;
    x: number;
    y: number;
  } | null>(null);

  const layout = useMemo(() => {
    const pad = { top: 16, right: 16, bottom: 32, left: 44 };
    const w = 560;
    const h = height;
    const allPoints = series.flatMap((s) => s.points);
    if (allPoints.length === 0) {
      return null;
    }

    const xs = allPoints.map((p, i) => (typeof p.x === "number" ? p.x : i));
    // Use index-based x for mixed/string x; prefer numeric when all numeric
    const numericX = allPoints.every((p) => typeof p.x === "number");
    const xValues = series[0]?.points.map((p, i) =>
      numericX && typeof p.x === "number" ? p.x : i,
    ) ?? [];

    let yMin = Infinity;
    let yMax = -Infinity;
    for (const s of series) {
      for (const p of s.points) {
        yMin = Math.min(yMin, p.ciLow ?? p.y, p.y);
        yMax = Math.max(yMax, p.ciHigh ?? p.y, p.y);
      }
    }
    if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) return null;
    if (yMin === yMax) {
      yMin -= 1;
      yMax += 1;
    }
    const yPad = (yMax - yMin) * 0.08;
    yMin -= yPad;
    yMax += yPad;

    const xMin = Math.min(...xValues, ...xs);
    const xMax = Math.max(...xValues, ...xs);
    const xSpan = xMax - xMin || 1;

    const innerW = w - pad.left - pad.right;
    const innerH = h - pad.top - pad.bottom;

    const sx = (x: number) => pad.left + ((x - xMin) / xSpan) * innerW;
    const sy = (y: number) => pad.top + innerH - ((y - yMin) / (yMax - yMin)) * innerH;

    return { pad, w, h, yMin, yMax, xMin, xMax, xSpan, innerW, innerH, sx, sy, numericX };
  }, [series, height]);

  if (status === "loading") return <Loading className={className} />;
  if (status === "error") {
    return <ErrorState message={errorMessage} onRetry={onRetry} className={className} />;
  }
  if (status === "empty" || !layout || series.every((s) => s.points.length === 0)) {
    return <Empty className={className} />;
  }

  const { pad, w, h, yMin, yMax, sx, sy, numericX, innerH } = layout;
  const maxLen = Math.max(...series.map((s) => s.points.length), 0);
  const yTicks = 4;
  const yTickVals = Array.from({ length: yTicks + 1 }, (_, i) => yMin + ((yMax - yMin) * i) / yTicks);

  return (
    <div className={cx("ds-chart", className)} style={{ position: "relative" }}>
      <svg
        className="ds-chart__svg"
        viewBox={`0 0 ${w} ${h}`}
        role="img"
        aria-label={ariaLabel}
        onMouseLeave={() => setHover(null)}
      >
        {yTickVals.map((t) => {
          const y = sy(t);
          return (
            <g key={t}>
              <line
                className="ds-chart__grid"
                x1={pad.left}
                x2={w - pad.right}
                y1={y}
                y2={y}
                stroke={theme["--border"] || undefined}
              />
              <text
                className="ds-chart__tick"
                x={pad.left - 6}
                y={y + 3}
                textAnchor="end"
                fill={theme["--text-faint"] || undefined}
              >
                {formatNum(t)}
              </text>
            </g>
          );
        })}
        <line
          className="ds-chart__axis"
          x1={pad.left}
          x2={pad.left}
          y1={pad.top}
          y2={pad.top + innerH}
          stroke={theme["--border-strong"] || undefined}
        />
        <line
          className="ds-chart__axis"
          x1={pad.left}
          x2={w - pad.right}
          y1={pad.top + innerH}
          y2={pad.top + innerH}
          stroke={theme["--border-strong"] || undefined}
        />

        {series.map((s, si) => {
          const color = s.color ?? dataviz.categorical[si % dataviz.categorical.length]!;
          const pts = s.points.map((p, i) => {
            const xv = numericX && typeof p.x === "number" ? p.x : i;
            return { ...p, xv, px: sx(xv), py: sy(p.y) };
          });
          if (pts.length === 0) return null;

          const hasCi = pts.some((p) => p.ciLow != null && p.ciHigh != null);
          let bandPath: string | null = null;
          if (hasCi) {
            const top = pts
              .map((p, i) => `${i === 0 ? "M" : "L"}${p.px},${sy(p.ciHigh ?? p.y)}`)
              .join(" ");
            const bot = [...pts]
              .reverse()
              .map((p, i) => `${i === 0 ? "L" : "L"}${p.px},${sy(p.ciLow ?? p.y)}`)
              .join(" ");
            bandPath = `${top} ${bot} Z`;
          }

          const linePath = pts
            .map((p, i) => `${i === 0 ? "M" : "L"}${p.px},${p.py}`)
            .join(" ");

          return (
            <g key={s.name}>
              {bandPath ? (
                <path d={bandPath} fill={color} opacity={0.15} stroke="none" />
              ) : null}
              <path
                d={linePath}
                fill="none"
                stroke={color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {pts.map((p, i) => (
                <circle
                  key={i}
                  cx={p.px}
                  cy={p.py}
                  r={hover?.xi === i ? 4 : 2.5}
                  fill={color}
                  stroke={theme["--surface"] || "var(--surface)"}
                  strokeWidth={1}
                  onMouseEnter={(e) => {
                    const parent = (e.target as SVGElement).closest(".ds-chart")?.getBoundingClientRect();
                    if (!parent) return;
                    setHover({ xi: i, x: e.clientX - parent.left, y: e.clientY - parent.top });
                  }}
                  onMouseMove={(e) => {
                    const parent = (e.target as SVGElement).closest(".ds-chart")?.getBoundingClientRect();
                    if (!parent) return;
                    setHover({ xi: i, x: e.clientX - parent.left, y: e.clientY - parent.top });
                  }}
                />
              ))}
            </g>
          );
        })}

        {/* x labels from first series */}
        {(series[0]?.points ?? []).map((p, i) => {
          if (maxLen > 12 && i % Math.ceil(maxLen / 8) !== 0) return null;
          const xv = numericX && typeof p.x === "number" ? p.x : i;
          return (
            <text
              key={i}
              className="ds-chart__tick"
              x={sx(xv)}
              y={h - 10}
              textAnchor="middle"
              fill={theme["--text-faint"] || undefined}
            >
              {String(p.x)}
            </text>
          );
        })}
      </svg>

      <div className="ds-chart__legend">
        {series.map((s, si) => {
          const color = s.color ?? dataviz.categorical[si % dataviz.categorical.length]!;
          return (
            <span key={s.name} className="ds-chart__legend-item">
              <span className="ds-chart__legend-swatch" style={{ background: color }} />
              {s.name}
            </span>
          );
        })}
      </div>

      {hover ? (
        <Tooltip
          x={hover.x}
          y={hover.y}
          title={String(series[0]?.points[hover.xi]?.x ?? hover.xi)}
          rows={series.map((s, si) => {
            const p = s.points[hover.xi];
            const color = s.color ?? dataviz.categorical[si % dataviz.categorical.length]!;
            if (!p) return { label: s.name, value: "—", color };
            const ci =
              p.ciLow != null && p.ciHigh != null
                ? ` (${formatNum(p.ciLow)}–${formatNum(p.ciHigh)})`
                : "";
            return { label: s.name, value: `${formatNum(p.y)}${ci}`, color };
          })}
        />
      ) : null}
    </div>
  );
}

function formatNum(n: number): string {
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2);
}
