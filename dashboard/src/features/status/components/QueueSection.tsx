"use client";

import { ErrorState, Section } from "@/design";
import type { QueueStatus, Result } from "../types";
import styles from "../status.module.css";

/**
 * Work-in-progress as a big number against its cap — UNLESS the queue is
 * degraded (ledger damage withheld the count), in which case this renders
 * a loud amber banner with the detail and NEVER a number. `wip: null`
 * without `wip_degraded` still refuses to print "0" — it says so in words
 * instead, since a null count with no degraded flag is itself a source
 * bug worth surfacing rather than papering over.
 */
export function QueueSection({ queue }: { queue: Result<QueueStatus> }) {
  return (
    <Section eyebrow="Loop queue" title="Queue">
      {!queue.ok ? (
        <ErrorState message={`Could not read queue.json: ${queue.error}`} />
      ) : queue.data.wip_degraded ? (
        <div className={styles.queueBanner} role="alert">
          <span className={styles.queueBannerTitle}>Queue degraded — WIP withheld</span>
          <span>{queue.data.wip_degraded_detail ?? "Ledger damage detected; count withheld."}</span>
        </div>
      ) : (
        <div className={styles.queueRow}>
          <span className={styles.queueBig}>
            {queue.data.wip === null ? "—" : queue.data.wip}
            <span className={styles.queueCap}>
              {" "}
              / {queue.data.wip_cap === null ? "?" : queue.data.wip_cap}
            </span>
          </span>
          {queue.data.wip === null ? (
            <span className={styles.muted}>WIP not reported (no degraded flag set) — treat as unknown, not zero.</span>
          ) : null}
        </div>
      )}
      {queue.ok && queue.data.rebuilt_at ? (
        <p className={styles.queueMeta}>rebuilt {queue.data.rebuilt_at}</p>
      ) : null}
    </Section>
  );
}
