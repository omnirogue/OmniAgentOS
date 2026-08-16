"use client";

import Link from "next/link";
import { Badge, Card, EmptyState, ErrorState, Loading, Section, StatusDot } from "@/design";
import type { StatusDotState } from "@/design";
import { useLoopHealth } from "./useLoopHealth";
import { TICK_TONE, type LoopHealthRow, type LoopTone } from "./loopHealth";
import styles from "./loop-health.module.css";

/** `LoopTone` -> `StatusDot`'s fixed state union. `neutral` has no direct
 * member — "queued" is the dot state whose CSS resolves to grey
 * (`var(--text-muted)`), matching "disabled = dimmed but present". */
const DOT_STATE: Record<LoopTone, StatusDotState> = {
  ok: "ok",
  warn: "warn",
  danger: "danger",
  neutral: "queued",
};

/** `LoopTone` values are already valid `BadgeTone` members. */
const BADGE_TONE: Record<LoopTone, "ok" | "warn" | "danger" | "neutral"> = {
  ok: "ok",
  warn: "warn",
  danger: "danger",
  neutral: "neutral",
};

function TickStrip({ ticks }: { ticks: LoopHealthRow["ticks"] }) {
  if (ticks.length === 0) {
    return <span className={styles.tickEmpty}>no runs yet</span>;
  }
  return (
    <div className={styles.tickStrip} role="img" aria-label={`Last ${ticks.length} runs, oldest to newest`}>
      {ticks.map((tick, index) => (
        <span
          key={tick.runId ?? `${index}`}
          className={styles.tick}
          data-tone={TICK_TONE[tick.outcome]}
          title={`${tick.finishedAt ?? "running"}`}
        />
      ))}
    </div>
  );
}

function LoopRowView({ row }: { row: LoopHealthRow }) {
  const parked = row.pendingApprovals.length;
  return (
    <li className={styles.row}>
      <div className={styles.primary}>
        <div className={styles.headLine}>
          <StatusDot state={DOT_STATE[row.tone]} label={row.stateLabel} />
          <span className={styles.name} data-dimmed={row.routine.status === "disabled"}>
            {row.routine.name}
          </span>
          <Badge tone={BADGE_TONE[row.tone]}>{row.stateLabel}</Badge>
        </div>
        <p className={styles.detail}>
          Last outcome: {row.lastOutcomeLabel}
        </p>
        <TickStrip ticks={row.ticks} />
        <p className={styles.staleness} data-stale={row.isStale}>
          {row.stalenessText}
        </p>
        <p className={styles.facts}>
          <span>{row.acceptanceText}</span>
          <span>{row.totalCostText}</span>
        </p>
      </div>
      <div className={styles.actions}>
        {parked > 0 ? (
          <Link href="/approvals">
            Parked: {parked} →
          </Link>
        ) : (
          <Link href="/loops">View routine →</Link>
        )}
      </div>
    </li>
  );
}

/** Self-contained "Managed routines" section, mounted on `/loops`. Reads
 * only endpoints already generated in `contracts/openapi.json`; the
 * `/api/dashboard/today` response is untouched. */
export function LoopHealthCard() {
  const { result, loading, error, refresh } = useLoopHealth();
  const parkedTotal = result
    ? result.loops.reduce((sum, row) => sum + row.pendingApprovals.length, 0)
    : 0;
  const description = result
    ? `${result.loops.length} loop${result.loops.length === 1 ? "" : "s"}` +
      (parkedTotal > 0 ? ` · ${parkedTotal} waiting on you` : "")
    : undefined;

  return (
    <Section title="Managed routines" description={description}>
      {loading && !result && !error ? (
        <Card>
          <Loading label="Loading loops…" />
        </Card>
      ) : null}
      {error ? (
        <Card>
          <ErrorState
            message={`Could not load loop health: ${error}`}
            onRetry={() => void refresh()}
          />
        </Card>
      ) : null}
      {!error && result ? (
        result.loops.length === 0 ? (
          <Card>
            <EmptyState message="No loops registered." />
          </Card>
        ) : (
          <Card flush>
            <ul className={styles.list}>
              {result.loops.map((row) => (
                <LoopRowView key={row.routine.id} row={row} />
              ))}
            </ul>
            {result.unmatchedLoopApprovals.length > 0 ? (
              <p className={styles.unmatched}>
                {result.unmatchedLoopApprovals.length} loop approval
                {result.unmatchedLoopApprovals.length === 1 ? "" : "s"} pending for a loop instance
                that doesn&apos;t match any registered routine —{" "}
                <Link href="/approvals">review in Approvals →</Link>
              </p>
            ) : null}
          </Card>
        )
      ) : null}
    </Section>
  );
}
