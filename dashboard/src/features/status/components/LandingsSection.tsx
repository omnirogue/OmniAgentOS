"use client";

import { ErrorState, Section } from "@/design";
import type { LandingsStatus, Result } from "../types";
import styles from "../status.module.css";

/** Commits landed on `origin/main` today (UTC) plus the single most recent
 * one, straight from `git log`. */
export function LandingsSection({ landings }: { landings: Result<LandingsStatus> }) {
  return (
    <Section eyebrow="Merge history" title="Landings">
      {!landings.ok ? (
        <ErrorState message={`Could not read git log: ${landings.error}`} />
      ) : (
        <div className={styles.landingsRow}>
          <span className={styles.landingsCount}>{landings.data.countToday}</span>
          <span className={styles.muted}>today</span>
          <span className={styles.landingsLast}>{landings.data.lastLanding ?? "no commits on origin/main"}</span>
        </div>
      )}
    </Section>
  );
}
