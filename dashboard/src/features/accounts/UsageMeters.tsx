"use client";

import { Pill } from "@/design";
import { formatAge, formatCredits, formatPercent, untilTime } from "./format";
import type { ProviderUsage } from "./types";
import styles from "./accounts.module.css";

/**
 * Consumption meters for one account's rate-limit windows.
 *
 * Every number here is a CACHE the CLI wrote when it last ran, so the age is
 * rendered alongside and a stale group is dimmed and labelled — an idle
 * account's quota can be hours behind, and a stale number shown as if it were
 * live is worse than showing nothing. Severity comes from the provider rather
 * than being re-derived from the percentage, so our thresholds can't drift
 * away from the ones the CLI itself stamps.
 *
 * When there is no telemetry (grok/gemini/kimi, or an account that has never
 * been run), the reason is shown instead of a fabricated zero.
 */
export function UsageMeters({ snapshot }: { snapshot: ProviderUsage | undefined }) {
  if (!snapshot) return <span className={styles.faint}>—</span>;

  if (!snapshot.available) {
    return <span className={styles.faint}>{snapshot.reason ?? "No usage data"}</span>;
  }

  const credits = formatCredits(snapshot.credits);

  return (
    <div className={`${styles.meters} ${snapshot.stale ? styles.meterStale : ""}`}>
      {snapshot.windows.map((window) => (
        <div key={`${window.kind}-${window.label}`} className={styles.meter}>
          <div className={styles.meterHead}>
            <span>{window.label}</span>
            <span className={styles.meterValue}>{formatPercent(window.percent)}</span>
          </div>
          <div
            className={styles.meterTrack}
            role="meter"
            aria-valuenow={Math.round(window.percent)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`${window.label} consumed`}
            title={window.resets_at ? `Resets ${untilTime(window.resets_at)}` : undefined}
          >
            <div
              className={styles.meterFill}
              data-severity={window.severity}
              style={{ width: `${Math.min(100, Math.max(0, window.percent))}%` }}
            />
          </div>
        </div>
      ))}

      <div className={styles.faint}>
        {snapshot.stale ? <Pill tone="warn">Stale</Pill> : null} {formatAge(snapshot.age_seconds)}
        {credits ? ` · ${credits}` : ""}
        {snapshot.credits?.disabled_reason ? ` · credits off (${snapshot.credits.disabled_reason})` : ""}
      </div>
    </div>
  );
}

/** Fleet-wide card for a provider that has no per-account row (codex/grok/…). */
export function ProviderUsageCard({ snapshot }: { snapshot: ProviderUsage }) {
  return (
    <div className={styles.providerCard}>
      <div className={styles.row}>
        <span className={styles.providerName}>{snapshot.provider}</span>
        {snapshot.plan ? <Pill tone="neutral">{snapshot.plan}</Pill> : null}
      </div>
      <UsageMeters snapshot={snapshot} />
    </div>
  );
}
