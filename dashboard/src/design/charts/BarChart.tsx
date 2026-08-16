"use client";

import { useMemo, useState } from "react";
import { dataviz, semantic } from "../tokens";
import { cx } from "../utils";
import { Empty } from "./Empty";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";
import { Tooltip } from "./Tooltip";
import { CHART_THEME_VARS, useCssVars } from "./useCssVar";

export type BarItem = {
  label: string;
  value: number;
  /** Semantic tone key or raw CSS color via token map */
  tone?: string;
};

export type BarChartProps = {
  bars: BarItem[];
  height?: number;
  status?: "ready" | "loading" | "empty" | "error";
  errorMessage?: string;
  onRetry?: () => void;
  className?: string;
  "aria-label"?: string;
};

const TONE_MAP: Record<string, string> = {
  champion: semantic.arm.champion,
  challenger: semantic.arm.challenger,
  promote: semantic.disposition.promote,
  reject: semantic.disposition.reject,
  win: semantic.result.win,
  loss: semantic.result.loss,
  draw: semantic.result.draw,
  ok: "var(--ok)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  accent: "var(--accent)",
  running: semantic.runState.running,
};

function barColor(bar: BarItem, index: number): string {
  if (bar.tone && TONE_MAP[bar.tone]) return TONE_MAP[bar.tone]!;
  if (bar.tone?.startsWith("var(") || bar.tone?.startsWith("#")) return bar.tone;
  return dataviz.sequential[Math.min(index, dataviz.sequential.length - 1)]!;
}

export function BarChart({
  bars,
  height = 220,
  status = "ready",
  errorMessage,
  onRetry,
  className,
  "aria-label": ariaLabel = "Bar chart",
}: BarChartProps) {
  const theme = useCssVars([...CHART_THEME_VARS]);
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);

  const layout = useMemo(() => {
    const pad = { top: 16, right: 12, bottom: 36, left: 40 };
    const w = 480;
    const h = height;
    const max = Math.max(0, ...bars.map((b) => b.value));
    const niceMax = max === 0 ? 1 : max * 1.1;
    const innerW = w - pad.left - pad.right;
    const innerH = h - pad.top - pad.bottom;
    const gap = 8;
    const barW = bars.length ? Math.max(8, (innerW - gap * (bars.length - 1)) / bars.length) : 0;
    return { pad, w, h, niceMax, innerW, innerH, gap, barW };
  }, [bars, height]);

  if (status === "loading") return <Loading className={className} />;
  if (status === "error") {
    return <ErrorState message={errorMessage} onRetry={onRetry} className={className} />;
  }
  if (status === "empty" || bars.length === 0) {
    return <Empty className={className} />;
  }

  const { pad, w, h, niceMax, innerH, gap, barW } = layout;
  const ticks = 4;
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => (niceMax * i) / ticks);

  return (
    <div className={cx("ds-chart", className)} style={{ position: "relative" }}>
      <svg
        className="ds-chart__svg"
        viewBox={`0 0 ${w} ${h}`}
        role="img"
        aria-label={ariaLabel}
      >
        {/* grid + y axis */}
        {yTicks.map((t) => {
          const y = pad.top + innerH - (t / niceMax) * innerH;
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
                {formatTick(t)}
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

        {bars.map((bar, i) => {
          const x = pad.left + i * (barW + gap);
          const bh = (bar.value / niceMax) * innerH;
          const y = pad.top + innerH - bh;
          const fill = barColor(bar, i);
          return (
            <g key={bar.label}>
              <rect
                x={x}
                y={y}
                width={barW}
                height={Math.max(bh, 0)}
                fill={fill}
                rx={3}
                opacity={hover && hover.i !== i ? 0.55 : 1}
                onMouseEnter={(e) => {
                  const rect = (e.target as SVGRectElement).ownerSVGElement?.getBoundingClientRect();
                  const parent = (e.target as SVGRectElement).closest(".ds-chart")?.getBoundingClientRect();
                  if (!parent) return;
                  setHover({
                    i,
                    x: e.clientX - parent.left,
                    y: e.clientY - parent.top,
                  });
                  void rect;
                }}
                onMouseMove={(e) => {
                  const parent = (e.target as SVGRectElement).closest(".ds-chart")?.getBoundingClientRect();
                  if (!parent) return;
                  setHover({ i, x: e.clientX - parent.left, y: e.clientY - parent.top });
                }}
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "default" }}
              />
              <text
                className="ds-chart__tick"
                x={x + barW / 2}
                y={h - 10}
                textAnchor="middle"
                fill={theme["--text-faint"] || undefined}
              >
                {truncate(bar.label, 10)}
              </text>
            </g>
          );
        })}
      </svg>
      {hover && bars[hover.i] ? (
        <Tooltip
          x={hover.x}
          y={hover.y}
          title={bars[hover.i]!.label}
          rows={[{ label: "value", value: String(bars[hover.i]!.value), color: barColor(bars[hover.i]!, hover.i) }]}
        />
      ) : null}
    </div>
  );
}

function formatTick(n: number): string {
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(1);
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
