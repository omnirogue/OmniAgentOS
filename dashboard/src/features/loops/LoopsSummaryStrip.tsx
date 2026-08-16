import { Badge, Button, Icon } from "@/design";
import { systemJobHealthLabel, systemJobHealthTone } from "@/features/routines/systemJobs";
import { formatDateTime } from "@/features/routines/format";
import type { SystemJobsSnapshot } from "@/features/routines/systemJobs";
import type { LoopsSummaryCounts } from "./filterSort";
import styles from "./loops.module.css";

const ORDER: Array<keyof LoopsSummaryCounts> = ["healthy", "stale", "failing", "unknown", "not_loaded"];

export function LoopsSummaryStrip({
  counts,
  snapshot,
  onRetry,
}: {
  counts: LoopsSummaryCounts;
  snapshot: SystemJobsSnapshot;
  /** Re-fetches the snapshot — wired to the degraded-probe notes' retry
   * affordance so a stale/unavailable probe isn't a dead end. */
  onRetry: () => void;
}) {
  const launchctlDegraded = Boolean(snapshot.launchctl && !snapshot.launchctl.available);
  // A MISSING `remote_probe` must read exactly like `available: false`, never
  // like "fine" — the live API predates the backend package that populates
  // this field, so its absence is the common case today, not an edge case.
  // Rendering "no note" for "we don't know" would be a favourable-absence
  // bug: silence reads as health, and it is not.
  const remoteProbeDegraded = !snapshot.remote_probe || snapshot.remote_probe.available === false;
  const remoteProbeReason = snapshot.remote_probe?.reason || "not yet reported by this snapshot";

  return (
    <div>
      <div className={styles.summaryStrip}>
        {ORDER.map((health) =>
          counts[health] > 0 ? (
            <Badge key={health} tone={systemJobHealthTone(health)}>
              {counts[health]} {systemJobHealthLabel(health).toLowerCase()}
            </Badge>
          ) : null,
        )}
        {ORDER.every((health) => counts[health] === 0) ? (
          <Badge tone="neutral">no loops discovered</Badge>
        ) : null}
      </div>
      {snapshot.generated_at ? (
        <p className={styles.summaryMeta}>Snapshot generated {formatDateTime(snapshot.generated_at)}</p>
      ) : null}
      {launchctlDegraded ? (
        <p className={styles.degradedNote}>
          <Icon name="alertTriangle" size={14} aria-hidden />
          <span>launchctl probe unavailable — {snapshot.launchctl!.reason}</span>
          <Button variant="ghost" size="sm" onClick={onRetry}>
            Retry
          </Button>
        </p>
      ) : null}
      {remoteProbeDegraded ? (
        <p className={styles.degradedNote}>
          <Icon name="alertTriangle" size={14} aria-hidden />
          <span>Remote probe unavailable — {remoteProbeReason}. Remote job health may be stale.</span>
          <Button variant="ghost" size="sm" onClick={onRetry}>
            Retry
          </Button>
        </p>
      ) : null}
    </div>
  );
}
