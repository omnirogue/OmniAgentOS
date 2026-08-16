"use client";

import Link from "next/link";
import { EmptyState, ErrorState, Icon, Loading } from "@/design";
import { totalDelta, useSystemDelta } from "@/features/pulse/hooks";
import type { DeltaResponse } from "@/features/pulse/types";
import styles from "@/features/pulse/pulse.module.css";

/**
 * Cockpit "since you were gone" — ``last_seen`` is persisted in
 * localStorage on every visit; the next visit fetches
 * ``GET /api/system/delta?since=<last_seen>`` and renders each counter
 * as a deep-linkable strip. Server clamps the range at 30 days so
 * long absences deep-link naturally to the full pages.
 *
 * The strip only renders when there is non-zero change; a day with
 * nothing happening should read as calm rather than broken.
 */
export function SinceYouWereGone() {
  const { delta, status, error, refresh } = useSystemDelta();

  if (status === "loading") {
    return (
      <section className={styles.cockpitPulse} aria-busy="true" aria-label="Since you were gone">
        <div className={styles.cockpitPulseHead}>
          <h2 className={styles.cockpitPulseTitle}>Since you were gone</h2>
        </div>
        <Loading />
      </section>
    );
  }

  if (status === "error") {
    return (
      <section className={styles.cockpitPulse} aria-label="Since you were gone">
        <div className={styles.cockpitPulseHead}>
          <h2 className={styles.cockpitPulseTitle}>Since you were gone</h2>
        </div>
        <ErrorState
          message={error ?? "Could not load activity"}
          retryLabel="Retry"
          onRetry={refresh}
        />
      </section>
    );
  }

  if (status === "empty" || !delta || totalDelta(delta) === 0) {
    return (
      <section className={styles.cockpitPulse} aria-label="Since you were gone">
        <div className={styles.cockpitPulseHead}>
          <h2 className={styles.cockpitPulseTitle}>Since you were gone</h2>
          {delta ? (
            <p className={styles.deltaSince}>
              since {formatSince(delta.since)}
            </p>
          ) : null}
        </div>
        <EmptyState
          icon={<Icon name="checkCircle" size={20} />}
          message="Nothing happened. The system is in a quiet state."
        />
      </section>
    );
  }

  return (
    <section className={styles.cockpitPulse} aria-label="Since you were gone">
      <div className={styles.cockpitPulseHead}>
        <div>
          <h2 className={styles.cockpitPulseTitle}>Since you were gone</h2>
          <p className={styles.cockpitPulseSub}>
            What happened while you were away.
          </p>
        </div>
        <p className={styles.deltaSince}>since {formatSince(delta.since)}</p>
      </div>
      <div className={styles.deltaStrip}>
        <DeltaStat
          label="Skills updated"
          value={delta.skills_updated}
          href="/skills"
        />
        <DeltaStat
          label="Improvements decided"
          value={delta.improvements_decided}
          href="/improvements"
        />
        <DeltaStat
          label="Loop runs"
          value={delta.loops_run}
          href="/loops"
        />
        <DeltaStat
          label="Tasks completed"
          value={delta.tasks_completed}
          href="/board"
        />
        <DeltaStat
          label="Active chats"
          value={delta.chats_active}
          href="/chats"
        />
      </div>
    </section>
  );
}

function DeltaStat({
  label,
  value,
  href,
}: {
  label: string;
  value: number;
  href: string;
}) {
  if (value === 0) return null;
  return (
    <Link
      href={href}
      className={styles.deltaStat}
      aria-label={`Open ${label} (${value})`}
    >
      <span className={styles.deltaStatLabel}>{label}</span>
      <span className={`${styles.deltaStatValue} ${styles["deltaStatValue--hot"]}`}>
        {value}
      </span>
    </Link>
  );
}

function formatSince(iso: string): string {
  const when = Date.parse(iso);
  if (Number.isNaN(when)) return iso;
  const diffMs = Date.now() - when;
  if (diffMs < 0) return "just now";
  const totalMinutes = Math.floor(diffMs / 60000);
  if (totalMinutes < 1) return "just now";
  if (totalMinutes < 60) return `${totalMinutes}m ago`;
  const hours = Math.floor(totalMinutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(when).toLocaleDateString();
}
