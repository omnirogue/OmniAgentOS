import { Badge, Pill, StatusDot } from "@/design";
import {
  executorLabel,
  formatCountdownTo,
  formatRelativeTime,
  loadedLabel,
  systemJobDotState,
  systemJobHealthLabel,
  systemJobHealthTone,
  type SystemJob,
} from "@/features/routines/systemJobs";
import { formatDateTime } from "@/features/routines/format";
import styles from "./loops.module.css";

/**
 * `last_result` has three distinguishable "nothing to show" states, plus the
 * real-value case — the field must never collapse missing/null/blank into
 * one indistinguishable dash:
 *   - the key absent (`undefined`) — an older/pre-`last_result` snapshot
 *   - `null` — measured, but no result was captured this run
 *   - `""` — measured, captured, and genuinely blank
 *   - a non-empty string — the real one-line outcome
 */
function lastResultDisplay(job: SystemJob): { text: string; muted: boolean; title?: string } {
  if (job.last_result) return { text: job.last_result, muted: false };
  if (job.last_result === null) {
    return { text: "—", muted: true, title: "No last result captured for this run." };
  }
  if (job.last_result === "") {
    return { text: "—", muted: true, title: "Last result was blank." };
  }
  return { text: "—", muted: true, title: "This snapshot did not report a last result." };
}

/** One row for a system job — name, plain-English purpose, schedule, health,
 * last run/result, executor, and truncated source (full path in `title`). */
export function LoopJobRow({ job, now }: { job: SystemJob; now: Date }) {
  const loaded = loadedLabel(job.loaded);
  const lastResult = lastResultDisplay(job);
  return (
    <li className={styles.jobRow}>
      <div className={styles.jobPrimary}>
        <div className={styles.jobNameLine}>
          <StatusDot state={systemJobDotState(job.health)} />
          <span className={styles.jobName}>{job.name}</span>
          <Badge tone={systemJobHealthTone(job.health)}>{systemJobHealthLabel(job.health)}</Badge>
          <Pill tone="neutral">{executorLabel(job.executor)}</Pill>
          {loaded ? <Pill tone={job.loaded ? "neutral" : "warn"}>{loaded}</Pill> : null}
        </div>
        <p className={styles.jobPurpose}>{job.purpose}</p>
        <div className={styles.jobMetaLine}>
          <span>{job.schedule.description}</span>
          <span title={job.last_run_at ?? undefined}>
            Last run: {formatRelativeTime(job.last_run_at, now)}
          </span>
          {job.next_fire_at ? (
            <span title={formatDateTime(job.next_fire_at)}>
              Next: {formatCountdownTo(job.next_fire_at, now)}
            </span>
          ) : null}
        </div>
        <p
          className={lastResult.muted ? styles.jobLastResultMuted : styles.jobLastResult}
          title={lastResult.title}
        >
          Last result: {lastResult.text}
        </p>
        <p className={styles.jobHealthReason} title={job.health_reason}>
          {job.health_reason}
        </p>
      </div>
      <div className={styles.jobSide}>
        <span className={styles.jobSource} title={job.source}>
          {job.source}
        </span>
      </div>
    </li>
  );
}
