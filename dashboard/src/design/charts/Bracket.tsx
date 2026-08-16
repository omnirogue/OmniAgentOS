"use client";

import { dataviz } from "../tokens";
import { cx } from "../utils";
import { Empty } from "./Empty";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

export type Match = {
  id: string;
  /** Display names for the two sides */
  a: string;
  b: string;
  /** Optional scores */
  scoreA?: number | string;
  scoreB?: number | string;
  /**
   * Winner: "a" | "b" | null, or the winning side's display name.
   */
  winner?: "a" | "b" | string | null;
};

export type BracketProps = {
  rounds: Match[][];
  status?: "ready" | "loading" | "empty" | "error";
  errorMessage?: string;
  onRetry?: () => void;
  className?: string;
  "aria-label"?: string;
};

const SLOT_H = 56;
const SLOT_W = 168;
const ROUND_GAP = 48;

function resolveWinner(match: Match): "a" | "b" | null {
  if (match.winner == null) return null;
  if (match.winner === "a" || match.winner === "b") return match.winner;
  if (match.winner === match.a) return "a";
  if (match.winner === match.b) return "b";
  return null;
}

export function Bracket({
  rounds,
  status = "ready",
  errorMessage,
  onRetry,
  className,
  "aria-label": ariaLabel = "Tournament bracket",
}: BracketProps) {
  if (status === "loading") return <Loading className={className} />;
  if (status === "error") {
    return <ErrorState message={errorMessage} onRetry={onRetry} className={className} />;
  }
  if (status === "empty" || rounds.length === 0 || rounds.every((r) => r.length === 0)) {
    return <Empty message="No bracket matches" className={className} />;
  }

  const maxMatches = Math.max(...rounds.map((r) => r.length));
  const height = maxMatches * (SLOT_H + 16) + 40;
  const width = rounds.length * (SLOT_W + ROUND_GAP) + 24;

  const matchY = (_roundIndex: number, matchIndex: number, count: number) => {
    const span = height - 40;
    const step = span / count;
    return 24 + step * matchIndex + step / 2 - SLOT_H / 2;
  };

  return (
    <div className={cx("ds-chart", className)} style={{ overflowX: "auto" }}>
      <svg
        className="ds-chart__svg"
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label={ariaLabel}
      >
        {rounds.slice(0, -1).map((round, ri) => {
          const next = rounds[ri + 1] ?? [];
          return round.map((match, mi) => {
            const x1 = 12 + ri * (SLOT_W + ROUND_GAP) + SLOT_W;
            const y1 = matchY(ri, mi, round.length) + SLOT_H / 2;
            const parentIdx = Math.floor(mi / 2);
            const x2 = 12 + (ri + 1) * (SLOT_W + ROUND_GAP);
            const y2 =
              next[parentIdx] != null
                ? matchY(ri + 1, parentIdx, next.length) + SLOT_H / 2
                : y1;
            const midX = (x1 + x2) / 2;
            return (
              <path
                key={`c-${match.id}`}
                d={`M${x1},${y1} H${midX} V${y2} H${x2}`}
                fill="none"
                stroke="var(--border-strong)"
                strokeWidth={1.5}
              />
            );
          });
        })}

        {rounds.map((round, ri) =>
          round.map((match, mi) => {
            const x = 12 + ri * (SLOT_W + ROUND_GAP);
            const y = matchY(ri, mi, round.length);
            const side = resolveWinner(match);
            const aWin = side === "a";
            const bWin = side === "b";
            return (
              <g key={match.id} transform={`translate(${x},${y})`}>
                <rect
                  width={SLOT_W}
                  height={SLOT_H}
                  rx={8}
                  fill="var(--surface)"
                  stroke="var(--border)"
                  strokeWidth={1}
                />
                <line
                  x1={0}
                  x2={SLOT_W}
                  y1={SLOT_H / 2}
                  y2={SLOT_H / 2}
                  stroke="var(--border)"
                />
                <rect
                  x={0}
                  y={0}
                  width={4}
                  height={SLOT_H / 2}
                  fill={aWin ? dataviz.categorical[0]! : "var(--border)"}
                  rx={2}
                />
                <text
                  x={12}
                  y={SLOT_H / 4 + 4}
                  fill={aWin ? "var(--text)" : "var(--text-muted)"}
                  fontSize={11}
                  fontWeight={aWin ? 600 : 400}
                  fontFamily="var(--font-sans)"
                >
                  {truncate(match.a, 18)}
                </text>
                {match.scoreA != null ? (
                  <text
                    x={SLOT_W - 10}
                    y={SLOT_H / 4 + 4}
                    textAnchor="end"
                    fill="var(--text-faint)"
                    fontSize={11}
                    fontFamily="var(--font-mono)"
                  >
                    {match.scoreA}
                  </text>
                ) : null}
                <rect
                  x={0}
                  y={SLOT_H / 2}
                  width={4}
                  height={SLOT_H / 2}
                  fill={bWin ? dataviz.categorical[1]! : "var(--border)"}
                  rx={2}
                />
                <text
                  x={12}
                  y={(3 * SLOT_H) / 4 + 4}
                  fill={bWin ? "var(--text)" : "var(--text-muted)"}
                  fontSize={11}
                  fontWeight={bWin ? 600 : 400}
                  fontFamily="var(--font-sans)"
                >
                  {truncate(match.b, 18)}
                </text>
                {match.scoreB != null ? (
                  <text
                    x={SLOT_W - 10}
                    y={(3 * SLOT_H) / 4 + 4}
                    textAnchor="end"
                    fill="var(--text-faint)"
                    fontSize={11}
                    fontFamily="var(--font-mono)"
                  >
                    {match.scoreB}
                  </text>
                ) : null}
              </g>
            );
          }),
        )}

        {rounds.map((round, ri) => (
          <text
            key={`lbl-${ri}`}
            x={12 + ri * (SLOT_W + ROUND_GAP) + SLOT_W / 2}
            y={14}
            textAnchor="middle"
            fill="var(--text-faint)"
            fontSize={10}
            fontWeight={600}
            letterSpacing="0.04em"
            fontFamily="var(--font-sans)"
          >
            {roundLabel(ri, rounds.length)}
          </text>
        ))}
      </svg>
    </div>
  );
}

function roundLabel(i: number, total: number): string {
  const fromEnd = total - 1 - i;
  if (fromEnd === 0) return "FINAL";
  if (fromEnd === 1) return "SEMI";
  if (fromEnd === 2) return "QUARTER";
  return `R${i + 1}`;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
