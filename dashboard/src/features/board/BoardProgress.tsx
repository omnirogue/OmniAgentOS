"use client";

import type { CSSProperties } from "react";
import type { BoardSummary } from "./summary";
import styles from "./board.module.css";

/**
 * The at-a-glance progress readout: "158 of 348 requests done", ONE segmented
 * track (done · running · needs you · queued · blocked), a legend, and the
 * sub-task rollup as a quieter secondary figure.
 *
 * One track, not five loose counts: the strip used to list the five numbers
 * side by side and leave the operator to do the arithmetic that answers "is
 * this moving?". The segments are the same numbers, drawn to scale, in one
 * glance.
 *
 * Counts are requested-task granularity (see summary.ts) — one unit per swarm
 * run, one per solo card. Sub-tasks are reported separately so the two
 * granularities are never read as one number.
 */

type Segment = {
  key: "done" | "running" | "needsResponse" | "queued" | "blocked";
  label: string;
  value: number;
  className: string;
  tone: string;
};

export function BoardProgress({
  summary,
  label = "requests",
}: {
  summary: BoardSummary;
  label?: string;
}) {
  if (!summary.total) return null;
  const { done, total, pct, running, queued, blocked, needsResponse, subtasks } = summary;
  const segments: Segment[] = [
    { key: "done", label: "done", value: done, className: styles.segDone, tone: "done" },
    { key: "running", label: "running", value: running, className: styles.segRunning, tone: "running" },
    { key: "needsResponse", label: "need you", value: needsResponse, className: styles.segNeeds, tone: "awaiting" },
    { key: "queued", label: "queued", value: queued, className: styles.segQueued, tone: "queued" },
    { key: "blocked", label: "blocked", value: blocked, className: styles.segBlocked, tone: "blocked" },
  ];

  return (
    <section className={styles.progressCard} aria-label="Overall progress">
      <div className={styles.bpTop}>
        <strong className={styles.bpHeadline}>
          {done} of {total} {label} done
        </strong>
        <span className={styles.bpPct}>{pct}%</span>
      </div>
      <div
        className={styles.segTrack}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${done} of ${total} ${label} done`}
      >
        {segments
          .filter((segment) => segment.value > 0)
          .map((segment) => (
            <span
              key={segment.key}
              className={`${styles.seg} ${segment.className}`}
              style={{ "--seg-width": `${(segment.value / total) * 100}%` } as CSSProperties}
              aria-hidden="true"
            />
          ))}
      </div>
      <div className={styles.bpFoot}>
        <div className={styles.legend}>
          {segments
            // Zero counts drop out of the legend, EXCEPT done — "0 done" is the
            // answer to the question this strip exists to ask.
            .filter((segment) => segment.value > 0 || segment.key === "done")
            .map((segment) => (
              <span key={segment.key} className={styles.legendItem} data-tone={segment.tone}>
                <span className={styles.legendDot} aria-hidden="true" />
                <b>{segment.value}</b> {segment.label}
              </span>
            ))}
        </div>
        {subtasks.total > 0 ? (
          <span className={styles.swarmNote}>
            {subtasks.done} / {subtasks.total} sub-tasks across {summary.runCount} swarm
            {summary.runCount === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
    </section>
  );
}
