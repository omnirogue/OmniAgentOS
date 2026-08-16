"use client";

import { EmptyState, ErrorState, Section } from "@/design";
import type { Result } from "../types";
import styles from "../status.module.css";

/** Up to the last 5 ALERTS.md bullets, monospace, most-recent last (the
 * file's own append order). */
export function AlertsSection({ alerts }: { alerts: Result<string[]> }) {
  return (
    <Section eyebrow="Loop queue" title="Alerts">
      {!alerts.ok ? (
        <ErrorState message={`Could not read ALERTS.md: ${alerts.error}`} />
      ) : alerts.data.length === 0 ? (
        <EmptyState message="No alerts in the current log tail." />
      ) : (
        <ul className={styles.alertsList}>
          {alerts.data.map((line, i) => (
            <li key={i} className={styles.alertItem}>
              {line}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
