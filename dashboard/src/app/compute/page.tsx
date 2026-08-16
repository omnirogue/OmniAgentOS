"use client";

import { Page, PageHeader, Section } from "@/design";
import { ComputeSummaryRow, MachineGrid, RunnersStrip, useComputeEstate } from "@/features/compute";
import styles from "@/features/compute/compute.module.css";

/**
 * Observatory /compute — "every machine and runner the estate can dispatch
 * to, right now." Current-state only: no history charts (the pool exposes
 * no time series), just a 15s-polled live snapshot of pool capacity,
 * per-machine load, and CI runner availability. Absence renders honestly —
 * an unmeasured number shows "—", never a fabricated zero or a coerced
 * success color.
 *
 * `useComputeEstate()` is called exactly ONCE here (UI-2 review fix) and
 * its result is passed down to the three display components as props —
 * previously each of them polled the same envelope independently (three
 * intervals, three concurrent fetches of one endpoint).
 */
export default function ComputePage() {
  const { data, status, degraded, error, refresh } = useComputeEstate();
  const viewProps = { data, status, error, refresh };

  return (
    <Page>
      <PageHeader
        title="Estate Compute"
        lead="Every machine and CI runner the estate can dispatch work to, right now — pool capacity, per-machine load, and runner availability."
        meta={
          // UI-1 review fix: a transient poll blip must never blank the page
          // into ErrorState — the last-good snapshot keeps rendering below,
          // and this is the ONLY place that ever admits the latest poll
          // failed. Neutral tone always (Rule 2) — this is not a failure of
          // the data shown, it's a retry-in-progress note.
          degraded ? <span className={styles.degradedNote}>Last update failed — retrying…</span> : undefined
        }
      />
      <p className={styles.lead}>
        Live snapshot · refreshes every 15s. There is no history here — the
        pool exposes no time series — and absence is rendered as absence: an
        unmeasured number shows &ldquo;—&rdquo;, never a fabricated zero.
      </p>

      <ComputeSummaryRow {...viewProps} />

      <Section title="Machines" description="Pool machines plus this dashboard's own local box.">
        <MachineGrid {...viewProps} />
      </Section>

      <Section title="Runners" description="Estate CI runners — busy, idle, or offline.">
        <RunnersStrip {...viewProps} />
      </Section>
    </Page>
  );
}
