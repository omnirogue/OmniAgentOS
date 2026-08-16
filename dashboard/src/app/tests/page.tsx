"use client";

import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Page,
  PageHeader,
  Section,
  Stat,
  StatusDot,
  Table,
  Tooltip,
  type BadgeTone,
  type StatusDotState,
  type TableColumn,
} from "@/design";
import type {
  CiRun,
  CiSection,
  GateRun,
  GateStep,
  LandingsSection,
  RefusalClass,
  TestsPayload,
} from "@/features/tests/types";
import styles from "@/features/tests/tests.module.css";

const POLL_MS = 30_000;
const REFUSAL_TRUNCATE = 96;

const FAILURE_OK = new Set(["success", "neutral", "skipped"]);

type TrainRow =
  | { kind: "run"; run: GateRun; key: string }
  | { kind: "error"; file: string; error: string; key: string };

function isCiError(ci: CiSection): ci is { error: string } {
  return "error" in ci;
}

function isLandingsError(landings: LandingsSection): landings is { error: string } {
  return "error" in landings;
}

function formatDuration(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const abs = Math.abs(seconds);
  if (abs < 60) {
    return Number.isInteger(seconds) ? `${seconds}s` : `${seconds.toFixed(1)}s`;
  }
  const m = Math.floor(abs / 60);
  const s = Math.round(abs % 60);
  return `${m}m ${s}s`;
}

function formatCounts(step: GateStep): string {
  if (!step.counts) return step.detailRaw || "—";
  const f = step.counts.failed == null ? "—" : String(step.counts.failed);
  const p = step.counts.passed == null ? "—" : String(step.counts.passed);
  const k = step.counts.skipped == null ? "—" : String(step.counts.skipped);
  return `${f} failed / ${p} passed / ${k} skipped`;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

function classTone(cls: RefusalClass): BadgeTone {
  if (cls === "pass") return "ok";
  if (cls === "candidate_defect") return "danger";
  if (cls === "instrument_error" || cls === "mechanical_refusal") return "warn";
  return "neutral";
}

function classLabel(cls: RefusalClass): string {
  if (cls === "pass") return "pass";
  if (cls === "candidate_defect") return "candidate defect";
  if (cls === "instrument_error") return "instrument error";
  if (cls === "mechanical_refusal") return "mechanical";
  return "UNCLASSIFIED";
}

function stepDotState(status: string): StatusDotState {
  const s = status.toLowerCase();
  if (s === "ok" || s === "passed" || s === "completed") return "ok";
  if (s === "failed" || s === "error" || s === "fail") return "failed";
  if (s === "skipped" || s === "cancelled") return "cancelled";
  if (s === "running" || s === "in_progress") return "running";
  return "warn";
}

function ciTone(conclusion: string, status: string): BadgeTone {
  const c = conclusion.toLowerCase();
  if (c === "success") return "ok";
  if (c === "neutral" || c === "skipped") return "cancelled";
  if (c === "failure" || c === "timed_out" || c === "cancelled" || c === "action_required" || c === "startup_failure") {
    return "failed";
  }
  if (!c && (status === "in_progress" || status === "queued" || status === "waiting" || status === "pending")) {
    return "running";
  }
  if (!c) return "neutral";
  return "failed";
}

function isFailureConclusion(conclusion: string): boolean {
  if (!conclusion) return false;
  return !FAILURE_OK.has(conclusion.toLowerCase());
}

function shortSha(sha: string): string {
  return sha ? sha.slice(0, 12) : "—";
}

function findMainHealthFailure(runs: CiRun[]): CiRun | null {
  return (
    runs.find(
      (run) =>
        /main-health/i.test(run.workflowName) &&
        run.headBranch === "main" &&
        isFailureConclusion(run.conclusion),
    ) ?? null
  );
}

function StepStrip({ steps }: { steps: GateStep[] }) {
  if (steps.length === 0) return <span className={styles.faint}>—</span>;
  return (
    <span className={styles.strip} aria-label={`${steps.length} steps`}>
      {steps.map((step, i) => (
        <Tooltip
          key={`${step.name}-${i}`}
          content={`${step.name || "step"} · ${formatCounts(step)}`}
        >
          <span className={styles.stripCell}>
            <StatusDot state={stepDotState(step.status)} label={step.name || "step"} />
          </span>
        </Tooltip>
      ))}
    </span>
  );
}

export default function TestsPage() {
  const [data, setData] = useState<TestsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // F04: same stale-banner pattern as features/status/hooks/useStatus.ts --
  // `lastUpdated` is the epoch ms of the last successful fetch, `refreshError`
  // is the reason the most recent attempt failed (cleared on the next
  // success). A poll failure after a good load must not render silently as
  // if `data` were still current.
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/local/tests", { cache: "no-store" });
      const json: unknown = await res.json();
      if (!res.ok || !json || typeof json !== "object" || !("gateRuns" in json)) {
        const message =
          json && typeof json === "object" && "error" in json
            ? String((json as { error: unknown }).error)
            : `HTTP ${res.status}`;
        setError(message);
        setRefreshError(message);
        return;
      }
      setData(json as TestsPayload);
      setError(null);
      setRefreshError(null);
      setLastUpdated(Date.now());
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      setRefreshError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const trainRows = useMemo<TrainRow[]>(() => {
    if (!data) return [];
    const rows: TrainRow[] = data.gateRuns.runs.map((run, i) => ({
      kind: "run",
      run,
      key: `run:${run.candidate_sha}:${run.started_at ?? ""}:${i}`,
    }));
    for (const err of data.gateRuns.errors) {
      rows.push({ kind: "error", file: err.file, error: err.error, key: `err:${err.file}` });
    }
    return rows;
  }, [data]);

  const trainColumns = useMemo<TableColumn<TrainRow>[]>(
    () => [
      {
        key: "sha",
        header: "SHA",
        sortable: true,
        sortValue: (row) => (row.kind === "run" ? row.run.shortSha : row.file),
        render: (row) =>
          row.kind === "error" ? (
            <span className={styles.errorFile}>{row.file}</span>
          ) : (
            <span className={styles.mono}>{row.run.shortSha || "—"}</span>
          ),
      },
      {
        key: "branch",
        header: "Branch",
        sortable: true,
        sortValue: (row) => (row.kind === "run" ? row.run.branch : ""),
        render: (row) =>
          row.kind === "error" ? (
            <span className={styles.faint}>unparseable</span>
          ) : (
            <span className={styles.mono}>{row.run.branch || "—"}</span>
          ),
      },
      {
        key: "mode",
        header: "Mode",
        sortable: true,
        sortValue: (row) => (row.kind === "run" ? row.run.mode ?? "unknown" : "—"),
        render: (row) =>
          row.kind === "error" ? (
            <span className={styles.faint}>—</span>
          ) : row.run.mode ? (
            <Badge tone="neutral">{row.run.mode}</Badge>
          ) : (
            <Badge tone="invalid" className={styles.modeUnknown}>
              unknown
            </Badge>
          ),
      },
      {
        key: "class",
        header: "Class",
        sortable: true,
        sortValue: (row) => (row.kind === "run" ? row.run.refusal_class : "error"),
        render: (row) =>
          row.kind === "error" ? (
            <Badge tone="invalid">parse error</Badge>
          ) : (
            <Badge
              tone={classTone(row.run.refusal_class)}
              className={row.run.refusal_class === "unclassified" ? styles.classUnclassified : undefined}
            >
              {classLabel(row.run.refusal_class)}
            </Badge>
          ),
      },
      {
        key: "duration",
        header: "Duration",
        sortable: true,
        sortValue: (row) => (row.kind === "run" ? row.run.duration_s ?? -1 : -1),
        render: (row) => (
          <span className={styles.mono}>
            {row.kind === "run" ? formatDuration(row.run.duration_s) : "—"}
          </span>
        ),
      },
      {
        key: "steps",
        header: "Steps",
        render: (row) =>
          row.kind === "error" ? (
            <span className={styles.errorMsg}>{row.error}</span>
          ) : (
            <StepStrip steps={row.run.steps} />
          ),
      },
      {
        key: "refusal",
        header: "Refusal",
        render: (row) => {
          if (row.kind === "error") return <span className={styles.faint}>—</span>;
          if (row.run.refusal_class === "pass" || !row.run.refusal_reason) {
            return <span className={styles.faint}>—</span>;
          }
          const full = row.run.refusal_reason;
          return (
            <Tooltip content={full}>
              <span className={styles.refusal} title={full}>
                {truncate(full, REFUSAL_TRUNCATE)}
              </span>
            </Tooltip>
          );
        },
      },
    ],
    [],
  );

  const ciColumns = useMemo<TableColumn<CiRun>[]>(
    () => [
      {
        key: "workflow",
        header: "Workflow",
        sortable: true,
        sortValue: (row) => row.workflowName,
        render: (row) => <span>{row.workflowName || "—"}</span>,
      },
      {
        key: "branch",
        header: "Branch",
        sortable: true,
        sortValue: (row) => row.headBranch,
        render: (row) => <span className={styles.mono}>{row.headBranch || "—"}</span>,
      },
      {
        key: "sha",
        header: "SHA",
        sortable: true,
        sortValue: (row) => row.headSha,
        render: (row) => <span className={styles.mono}>{shortSha(row.headSha)}</span>,
      },
      {
        key: "status",
        header: "Status",
        sortable: true,
        sortValue: (row) => row.status,
        render: (row) => <Badge tone="neutral">{row.status || "—"}</Badge>,
      },
      {
        key: "conclusion",
        header: "Conclusion",
        sortable: true,
        sortValue: (row) => row.conclusion,
        render: (row) => (
          <Badge tone={ciTone(row.conclusion, row.status)}>{row.conclusion || row.status || "—"}</Badge>
        ),
      },
      {
        key: "created",
        header: "Created",
        sortable: true,
        sortValue: (row) => row.createdAt,
        render: (row) => <span className={styles.mono}>{row.createdAt || "—"}</span>,
      },
    ],
    [],
  );

  const ciFailure = data && !isCiError(data.ci) ? findMainHealthFailure(data.ci.runs) : null;
  const landingDays = data && !isLandingsError(data.landings) ? data.landings.days : [];
  const landingMax = Math.max(1, ...landingDays.map((d) => d.count));

  // No `data` yet AND the fetch that would have produced it failed -- the
  // page has nothing honest to show but the error itself.
  const initialLoadFailed = !loading && !data && Boolean(error);
  // F04: `data` is present (from a prior successful load) but the MOST
  // RECENT poll failed -- the payload on screen is stale and that must be
  // visible, not silently rendered as if it were still fresh.
  const showStaleBanner = Boolean(data) && Boolean(refreshError);

  return (
    <Page>
      <PageHeader
        eyebrow="Engineering"
        title="Tests"
        lead="Latest merge-gate trains, GitHub Actions for Globex/OmniAgentOS, and landings on origin/main."
        meta={
          data ? (
            <span className={styles.meta}>as of {data.generatedAt}</span>
          ) : null
        }
        actions={
          <Button variant="secondary" size="sm" onClick={() => void refresh()}>
            Refresh
          </Button>
        }
      />

      {loading && !data ? (
        <Card>
          <Loading variant="skeleton" label="Loading tests…" lines={6} />
        </Card>
      ) : null}

      {initialLoadFailed ? <ErrorState message={error ?? "Could not load tests."} onRetry={() => void refresh()} /> : null}

      {showStaleBanner ? (
        <div className={styles.unavailable} role="status">
          Data is stale — last successful update{" "}
          {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : "unknown"}, most recent refresh failed:{" "}
          {refreshError}
        </div>
      ) : null}

      {data ? (
        <>
          {isCiError(data.ci) ? (
            <div className={styles.unavailable} role="status">
              CI status unavailable: {data.ci.error}
            </div>
          ) : null}
          {ciFailure ? (
            <div className={styles.dangerBanner} role="alert">
              main-health {ciFailure.conclusion || "failed"} on main · {shortSha(ciFailure.headSha)} ·{" "}
              {ciFailure.workflowName}
            </div>
          ) : null}

          <Section
            title="Train board"
            description="Newest 20 merge-gate run receipts. Red is reserved for candidate defects."
          >
            {trainRows.length === 0 ? (
              <Card>
                <EmptyState title="No gate runs" message="No *.run-*.json receipts in the evidence directory." />
              </Card>
            ) : (
              <Table
                columns={trainColumns}
                rows={trainRows}
                rowKey={(row) => row.key}
                caption="Merge-gate train board"
                emptyMessage="No gate runs"
              />
            )}
          </Section>

          <Section title="CI" description="Last 10 workflow runs on Globex/OmniAgentOS.">
            {isCiError(data.ci) ? (
              <ErrorState title="CI unavailable" message={data.ci.error} onRetry={() => void refresh()} />
            ) : data.ci.runs.length === 0 ? (
              <Card>
                <EmptyState title="No CI runs" message="gh run list returned an empty list." />
              </Card>
            ) : (
              <Table
                columns={ciColumns}
                rows={data.ci.runs}
                rowKey={(row) => `${row.headSha}:${row.createdAt}:${row.workflowName}:${row.headBranch}`}
                caption="GitHub Actions runs"
              />
            )}
          </Section>

          <Section title="Landings" description="Commits on origin/main, last 7 local calendar days.">
            {isLandingsError(data.landings) ? (
              <ErrorState title="Landings unavailable" message={data.landings.error} onRetry={() => void refresh()} />
            ) : (
              <Card>
                <div className={styles.landings}>
                  <div className={styles.bars} role="img" aria-label="Landings per day, last 7 days">
                    {landingDays.map((day) => {
                      const today = day.date === landingDays[landingDays.length - 1]?.date;
                      const heightPct = day.count === 0 ? 6 : Math.max(10, (day.count / landingMax) * 100);
                      return (
                        <Tooltip key={day.date} content={`${day.date}: ${day.count} landing${day.count === 1 ? "" : "s"}`}>
                          <div className={styles.barCol}>
                            <div className={styles.barTrack}>
                              <div
                                className={`${styles.bar} ${day.count === 0 ? styles.barZero : ""} ${today ? styles.barToday : ""}`}
                                style={{ "--bar-h": `${heightPct}%` } as CSSProperties}
                              />
                            </div>
                            <div className={styles.barLabel}>
                              {day.date.slice(5)}
                              <br />
                              {day.count}
                            </div>
                          </div>
                        </Tooltip>
                      );
                    })}
                  </div>
                  <div className={styles.todayCallout}>
                    <Stat
                      label="Today"
                      value={data.landings.todayCount}
                      tone={data.landings.todayCount > 0 ? "ok" : "default"}
                    />
                  </div>
                </div>
              </Card>
            )}
          </Section>
        </>
      ) : null}
    </Page>
  );
}
