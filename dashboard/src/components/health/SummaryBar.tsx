import { Stat, type StatTone } from "@/design";
import styles from "./health.module.css";
import { BROKEN_FIRST_ORDER, type StatusCounts } from "./logic";
import { STATUS_LABEL } from "./StatusBadge";
import type { CapabilityStatus } from "./types";

const STAT_TONE: Record<CapabilityStatus, StatTone> = {
  OK: "ok",
  DEGRADED: "warn",
  DOWN: "danger",
  STALE: "warn",
  CANNOT_EVALUATE: "warn",
  // The brief calls for UNVERIFIED to carry EQUAL prominence to DOWN — both
  // read as "danger" tone here, not a lesser/neutral one, so a reader
  // scanning the summary bar for "what needs attention" cannot miss the
  // "we aren't even watching this" count next to the "it's actually down"
  // count.
  UNVERIFIED: "danger",
};

export function SummaryBar({ counts }: { counts: StatusCounts }) {
  return (
    <div className={styles.statGrid} role="group" aria-label="Capability status summary">
      {BROKEN_FIRST_ORDER.map((status) => (
        <Stat key={status} label={STATUS_LABEL[status]} value={counts[status]} tone={STAT_TONE[status]} />
      ))}
    </div>
  );
}
