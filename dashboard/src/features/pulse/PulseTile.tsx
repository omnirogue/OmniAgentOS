"use client";

import Link from "next/link";
import { ErrorState, EmptyState, Icon, Sparkline, Loading } from "@/design";
import type { AsyncStatus } from "./hooks";
import type { PulseTileData } from "./types";
import { trendFromDelta } from "./tiles";
import styles from "./pulse.module.css";

export type PulseTileProps = {
  data: PulseTileData | null;
  status: AsyncStatus;
  error: string | null;
  onRetry?: () => void;
  /** Override the sparkline tone (derived from data.tone by default). */
  sparkTone?: "accent" | "ok" | "danger" | "warn" | "promote" | "reject";
};

/**
 * A single Observatory tile. Renders the metric's headline value, caption,
 * an optional delta badge, a 14-day sparkline, and a deep-link footer.
 * Handles loading/empty/error states inline so the grid never collapses
 * into a blank column — Stripe's "every state is designed" rule.
 */
export function PulseTile({ data, status, error, onRetry, sparkTone }: PulseTileProps) {
  if (status === "loading") {
    return (
      <div className={styles.tile} aria-busy="true">
        <div className={styles.tileHead}>
          <span className={styles.tileTitle}>…</span>
        </div>
        <div className={styles.tileBody}>
          <span className={styles.tileValue}>—</span>
        </div>
        <Loading />
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className={styles.tile}>
        <div className={styles.tileHead}>
          <span className={styles.tileTitle}>{data?.title ?? "Metric"}</span>
        </div>
        <ErrorState
          message={error ?? "Could not load metric"}
          retryLabel="Retry"
          onRetry={onRetry}
        />
      </div>
    );
  }

  if (status === "empty" || !data) {
    return (
      <div className={styles.tile}>
        <div className={styles.tileHead}>
          <span className={styles.tileTitle}>{data?.title ?? "Metric"}</span>
        </div>
        <EmptyState
          icon={<Icon name="barChart" size={20} />}
          message="No data yet — the first snapshot will populate this shortly."
        />
      </div>
    );
  }

  const valueTone = data.tone ?? "default";
  const valueCls = valueTone !== "default" ? `${styles.tileValue} ${styles[`tileValue--${valueTone}`]}` : styles.tileValue;
  const toneToSpark: Record<string, "accent" | "ok" | "danger" | "warn" | "promote" | "reject"> = {
    accent: "accent",
    ok: "ok",
    danger: "danger",
    warn: "warn",
    promote: "promote",
    reject: "reject",
  };
  const sparkColor: "accent" | "ok" | "danger" | "warn" | "promote" | "reject" =
    sparkTone ?? toneToSpark[valueTone] ?? "accent";
  const deltaTrend = data.delta ? trendFromDelta(
    typeof data.delta.value === "number" ? data.delta.value : 0,
  ) : "neutral";
  const deltaCls =
    deltaTrend === "up"
      ? `${styles.tileDelta} ${styles["tileDelta--up"]}`
      : deltaTrend === "down"
      ? `${styles.tileDelta} ${styles["tileDelta--down"]}`
      : styles.tileDelta;
  const deltaText = formatDeltaText(data.delta?.value);

  return (
    <Link href={data.deepLink} className={styles.tile} aria-label={`Open ${data.title}`}>
      <div className={styles.tileHead}>
        <span className={styles.tileTitle}>{data.title}</span>
        {data.sparkPoints.length >= 2 ? (
          <span className={styles.tileSpark}>
            <Sparkline
              points={data.sparkPoints}
              width={120}
              height={32}
              tone={sparkColor}
              aria-label={`${data.title} trend`}
            />
          </span>
        ) : null}
      </div>
      <div className={styles.tileBody}>
        <span className={valueCls}>{data.primary}</span>
        {deltaText ? <span className={deltaCls}>{deltaText}</span> : null}
      </div>
      {data.caption ? <p className={styles.tileCaption}>{data.caption}</p> : null}
      <span className={styles.tileLink}>
        Open {data.title.toLowerCase()}
        <Icon name="chevronRight" size={12} />
      </span>
    </Link>
  );
}

function formatDeltaText(delta: string | number | undefined): string {
  if (delta == null || delta === "") return "";
  if (typeof delta === "number") {
    if (delta === 0) return "";
    return delta > 0 ? `+${delta.toFixed(Number.isInteger(delta) ? 0 : 1)}` : delta.toFixed(Number.isInteger(delta) ? 0 : 1);
  }
  return String(delta);
}
