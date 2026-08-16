"use client";

import Link from "next/link";
import { EmptyState, ErrorState, Icon, Sparkline, Stat } from "@/design";
import { usePulseSeries } from "@/features/pulse/hooks";
import type { AsyncStatus } from "@/features/pulse/hooks";
import { deltaFromSeries, formatValue, latestValue } from "@/features/pulse/tiles";
import styles from "@/features/pulse/pulse.module.css";

/**
 * Cockpit Growth pulse — a single-row strip of headline KPIs:
 * skills delta, improvements applied / reverted, routine runs, and
 * spend telemetry. Every stat is a deep link via Stat's wrapper and
 * shows a 14-day sparkline of the underlying metric.
 *
 * Spend telemetry is deliberately omitted here (no live cost aggregation
 * yet); the tile reserves space by showing only the growth signals
 * backed by real pulse_series rows, so the strip never renders broken.
 */
export function GrowthPulse() {
  const skillsTotal = usePulseSeries("skills.total", 14);
  const improvements = usePulseSeries("improvements.applied", 14);
  const loops = usePulseSeries("loops.fires", 14);

  return (
    <section className={styles.cockpitPulse} aria-label="Growth">
      <div className={styles.cockpitPulseHead}>
        <div>
          <h2 className={styles.cockpitPulseTitle}>Growth</h2>
          <p className={styles.cockpitPulseSub}>
            Skills, improvements, and loop runs — how the system has been
            growing over the last two weeks.
          </p>
        </div>
        <Link
          href="/pulse"
          className="ds-btn ds-btn--ghost ds-btn--sm"
          aria-label="Open observatory"
        >
          Observatory
          <Icon name="chevronRight" size={12} />
        </Link>
      </div>
      <div className={styles.pulseRow}>
        <GrowthStat
          label="Skills"
          deepLink="/skills"
          series={skillsTotal.series}
          status={skillsTotal.status}
          error={skillsTotal.error}
          onRetry={skillsTotal.refresh}
          metricKey="skills.total"
          sparkTone="accent"
        />
        <GrowthStat
          label="Improvements applied"
          deepLink="/improvements"
          series={improvements.series}
          status={improvements.status}
          error={improvements.error}
          onRetry={improvements.refresh}
          metricKey="improvements.applied"
          sparkTone="promote"
        />
        <GrowthStat
          label="Loop runs"
          deepLink="/loops"
          series={loops.series}
          status={loops.status}
          error={loops.error}
          onRetry={loops.refresh}
          metricKey="loops.fires"
          sparkTone="accent"
        />
      </div>
    </section>
  );
}

type GrowthStatProps = {
  label: string;
  deepLink: string;
  series: ReturnType<typeof usePulseSeries>["series"];
  status: AsyncStatus;
  error: string | null;
  onRetry: () => void;
  metricKey: string;
  sparkTone: "accent" | "ok" | "danger" | "warn" | "promote" | "reject";
};

function GrowthStat({
  label,
  deepLink,
  series,
  status,
  error,
  onRetry,
  metricKey,
  sparkTone,
}: GrowthStatProps) {
  const wrapperClass = styles.pulseRowItem;

  if (status === "loading") {
    return (
      <Stat
        label={label}
        value="—"
        className={wrapperClass}
        aria-busy="true"
      />
    );
  }
  if (status === "error") {
    return (
      <div className={wrapperClass}>
        <ErrorState message={error ?? "Load failed"} onRetry={onRetry} />
      </div>
    );
  }
  if (status === "empty" || !series || series.points.length === 0) {
    return (
      <div className={wrapperClass}>
        <EmptyState message="Awaiting first snapshot." />
      </div>
    );
  }

  const value = latestValue(series);
  const delta = deltaFromSeries(series);
  const deltaTrend: "up" | "down" | "neutral" =
    delta > 0 ? "up" : delta < 0 ? "down" : "neutral";

  return (
    <Link href={deepLink} className={wrapperClass} aria-label={`Open ${label}`}>
      <Stat
        label={label}
        value={formatValue(metricKey, value)}
        delta={delta === 0 ? undefined : delta}
        deltaTrend={deltaTrend}
        hint="14-day trend"
        tone={sparkTone === "promote" ? "promote" : "accent"}
      />
      <Sparkline
        points={series.points.map((p) => p.value)}
        width={140}
        height={28}
        tone={sparkTone}
        aria-label={`${label} 14-day trend`}
      />
    </Link>
  );
}
