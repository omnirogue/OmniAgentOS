"use client";

import { EmptyState, ErrorState, Section } from "@/design";
import { relativeFromIso } from "../format";
import type { GateStatus, Result } from "../types";
import styles from "../status.module.css";

/**
 * The gate loop's train lines (up to the last 5 "still gating" / "no
 * trains assembled" / "gate slots full" lines from its log tail) plus the
 * timestamp of its own last tick — present even on a quiet tick, so a
 * silent gate loop still shows it's alive vs. one that's actually stalled.
 */
export function GateSection({ gate }: { gate: Result<GateStatus> }) {
  const now = Date.now();

  return (
    <Section eyebrow="Merge gate" title="Gate">
      {!gate.ok ? (
        <ErrorState message={`Could not read gate-loop.log: ${gate.error}`} />
      ) : gate.data.trainLines.length === 0 ? (
        <EmptyState message="No train lines in the current log tail." />
      ) : (
        <ul className={styles.gateList}>
          {gate.data.trainLines.map((line, i) => (
            <li key={i} className={styles.gateLine}>
              {line}
            </li>
          ))}
        </ul>
      )}
      {gate.ok ? (
        <p className={styles.gateTick}>
          {gate.data.lastAt
            ? `last tick ${relativeFromIso(gate.data.lastAt, now)} (${gate.data.lastAt})`
            : "no tick timestamp in the current log tail"}
        </p>
      ) : null}
    </Section>
  );
}
