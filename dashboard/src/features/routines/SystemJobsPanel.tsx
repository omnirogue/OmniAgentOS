"use client";

/** "System loops" section of the Loops page: every scheduled job on the system
 * that is NOT a managed DB routine — launchd agents, the CSI self-improvement
 * pipeline routines, and the documented remote cron jobs — read-only, with
 * per-job health (HANDOFF/LOOPS-VISIBILITY.md §L1/L2: display is the whole win;
 * no enable/disable/run-now for launchd or remote cron in this pass). */
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Icon,
  Loading,
  Pill,
  StatusDot,
  Table,
  type TableColumn,
} from "@/design";
import { useSystemJobs } from "./hooks";
import {
  executorLabel,
  formatCountdownTo,
  formatRelativeTime,
  healthSeverity,
  loadedLabel,
  systemJobDotState,
  systemJobHealthLabel,
  systemJobHealthTone,
  type SystemJob,
} from "./systemJobs";
import { formatDateTime } from "./format";
import styles from "./routines.module.css";

function SystemJobStatus({ job }: { job: SystemJob }) {
  const loaded = loadedLabel(job.loaded);
  return (
    <div className={styles.statusCell}>
      <span className={styles.liveStateDot}>
        <StatusDot state={systemJobDotState(job.health)} />
        <Badge tone={systemJobHealthTone(job.health)}>{systemJobHealthLabel(job.health)}</Badge>
      </span>
      {loaded ? (
        <Pill tone={job.loaded ? "neutral" : "warn"}>{loaded}</Pill>
      ) : null}
      <span className={styles.healthReason}>{job.health_reason}</span>
    </div>
  );
}

function buildColumns(now: Date): TableColumn<SystemJob>[] {
  return [
    {
      key: "name",
      header: "Loop",
      sortable: true,
      sortValue: (j) => j.name,
      render: (j) => (
        <div className={styles.loopNameCell}>
          <div className={styles.loopNamePrimary}>{j.name}</div>
          <div className={styles.loopDescription}>{j.purpose}</div>
          {j.managed_candidate ? (
            <div className={styles.candidateNote}>
              <Icon name="sparkles" size={12} aria-hidden />
              <span>Could become a managed loop — {j.candidate_reason}</span>
            </div>
          ) : null}
        </div>
      ),
    },
    {
      key: "category",
      header: "Where",
      sortable: true,
      sortValue: (j) => `${j.category} ${j.executor}`,
      render: (j) => (
        <div className={styles.categoryCell}>
          <Pill tone="neutral">{j.category}</Pill>
          <span className={styles.faint}>{executorLabel(j.executor)}</span>
        </div>
      ),
    },
    {
      key: "schedule",
      header: "Schedule",
      sortable: true,
      sortValue: (j) => j.schedule.description,
      render: (j) => <span className={styles.mono}>{j.schedule.description}</span>,
    },
    {
      key: "lastRun",
      header: "Last run",
      sortable: true,
      sortValue: (j) => (j.last_run_at ? Date.parse(j.last_run_at) : null),
      render: (j) => (
        <div className={styles.countdownCell}>
          <span className={styles.countdownValue}>{formatRelativeTime(j.last_run_at, now)}</span>
          <span className={`${styles.countdownMuted} ${styles.mono}`}>
            {formatDateTime(j.last_run_at)}
          </span>
        </div>
      ),
    },
    {
      key: "nextFire",
      header: "Next fire",
      sortable: true,
      sortValue: (j) => (j.next_fire_at ? Date.parse(j.next_fire_at) : null),
      render: (j) => (
        <div className={styles.countdownCell}>
          <span className={styles.countdownValue}>{formatCountdownTo(j.next_fire_at, now)}</span>
          <span className={`${styles.countdownMuted} ${styles.mono}`}>
            {formatDateTime(j.next_fire_at)}
          </span>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      sortValue: (j) => healthSeverity(j.health),
      render: (j) => <SystemJobStatus job={j} />,
    },
    {
      key: "source",
      header: "Defined in",
      render: (j) => <span className={styles.sourcePath}>{j.source}</span>,
    },
  ];
}

export function SystemJobsPanel({ now }: { now: Date }) {
  const { snapshot, loading, error, refresh } = useSystemJobs();
  const jobs = snapshot?.jobs ?? [];

  if (loading) {
    return (
      <Card>
        <Loading variant="skeleton" label="Loading system loops…" lines={5} />
      </Card>
    );
  }
  if (error) {
    return <ErrorState message={error} onRetry={refresh} />;
  }
  if (!snapshot || jobs.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No system loops found"
          message="No launchd jobs, CSI pipeline routines, or documented remote loops were discovered on this system."
        />
      </Card>
    );
  }

  const columns = buildColumns(now);
  const loadedCount = snapshot.counts.loaded;
  const problemCount = snapshot.counts.failing + snapshot.counts.stale;

  return (
    <Card padding="none">
      <div className={styles.systemJobsHeader}>
        <div className={styles.panelHeader}>
          <div>
            <h3 className={styles.panelTitle}>System loops</h3>
            <p className={styles.panelSubtitle}>
              Every scheduled job on this system — launchd agents, the CSI self-improvement
              pipeline, documented remote loops. Read-only: load/unload stays with launchd.
            </p>
          </div>
          <Icon name="clock" size={14} className={styles.refreshIcon} aria-hidden />
        </div>
        <div className={styles.summaryStrip}>
          <Badge tone="neutral">{snapshot.counts.total} total</Badge>
          <Badge tone="ok">{loadedCount} loaded</Badge>
          {snapshot.counts.failing > 0 ? (
            <Badge tone="danger">{snapshot.counts.failing} failing</Badge>
          ) : null}
          {snapshot.counts.stale > 0 ? (
            <Badge tone="warn">{snapshot.counts.stale} stale</Badge>
          ) : null}
          {problemCount === 0 ? <Badge tone="neutral">no problems detected</Badge> : null}
        </div>
      </div>
      <Table columns={columns} rows={jobs} rowKey={(j) => j.key} caption="System loops" />
    </Card>
  );
}
