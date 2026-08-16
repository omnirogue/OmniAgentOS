"use client";

import { useEffect, useMemo, useState } from "react";
import { Button, Card, EmptyState, ErrorState, Loading, Section } from "@/design";
import { useSystemJobs } from "@/features/routines/hooks";
import { categoriesOf, emptyFilterState, filterJobs, groupByCategory, sortJobs, summarizeHealth } from "./filterSort";
import type { LoopsFilterState } from "./filterSort";
import { LoopsFilters, type LoopsSortValue } from "./LoopsFilters";
import { LoopsSummaryStrip } from "./LoopsSummaryStrip";
import { LoopJobRow } from "./LoopJobRow";
import styles from "./loops.module.css";

/** How often "now" (relative-time math: last run, next fire) is recomputed
 * independent of a network refetch — a page left open must not let "5m ago"
 * silently rot into "5m ago" forever between the 60s data polls. */
const CLOCK_TICK_MS = 15_000;

/**
 * "System loops" — every scheduled job on the estate that is NOT a managed DB
 * routine: launchd agents, the CSI self-improvement pipeline, and documented
 * remote cron/docker jobs. Read-only. Grouped by category by default; any
 * other sort flattens to one list, which keeps the UX simple and scannable
 * per the brief.
 */
export function SystemLoopsSection() {
  const { snapshot, loading, error, refresh } = useSystemJobs();
  const [now, setNow] = useState<Date>(() => new Date());
  const [filter, setFilter] = useState<LoopsFilterState>(emptyFilterState());
  const [sort, setSort] = useState<LoopsSortValue>("default");

  // `useSystemJobs` already polls every 60s and re-fetches on manual retry —
  // "now" needs to move independent of that so relative times ("5m ago")
  // don't freeze on a page left open between polls (and every render of a
  // statically prerendered page starts with a live wall-clock value, not a
  // build-time-frozen `useMemo(() => new Date(), [])`).
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), CLOCK_TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  // Also snap "now" forward the moment fresh data lands, so a manual/poll
  // refresh doesn't wait up to CLOCK_TICK_MS to reflect in relative times.
  useEffect(() => {
    if (snapshot) setNow(new Date());
  }, [snapshot]);

  const jobs = useMemo(() => snapshot?.jobs ?? [], [snapshot]);
  const categories = useMemo(() => categoriesOf(jobs), [jobs]);
  const filtered = useMemo(() => filterJobs(jobs, filter), [jobs, filter]);
  // Summary counts reflect EVERY discovered loop, not just the filtered
  // subset — a filter that hides every "healthy" row must never make the
  // strip claim "no loops discovered" when loops exist, just filtered out.
  const counts = useMemo(() => summarizeHealth(jobs), [jobs]);
  const grouped = useMemo(
    () => (sort === "default" ? groupByCategory(filtered) : null),
    [filtered, sort],
  );
  const flat = useMemo(
    () => (sort === "default" ? null : sortJobs(filtered, sort, now)),
    [filtered, sort, now],
  );

  if (loading && !snapshot) {
    return (
      <Section title="System loops" description="Every scheduled job on this estate — launchd agents, the CSI pipeline, documented remote loops.">
        <Card>
          <Loading variant="skeleton" label="Loading system loops…" lines={6} />
        </Card>
      </Section>
    );
  }
  if (error) {
    return (
      <Section title="System loops">
        <ErrorState message={`Could not load system loops: ${error}`} onRetry={() => void refresh()} />
      </Section>
    );
  }
  if (!snapshot) return null;

  return (
    <Section
      title="System loops"
      description="Every scheduled job on this estate — launchd agents, the CSI self-improvement pipeline, documented remote loops. Read-only."
      actions={
        <Button variant="ghost" size="sm" onClick={() => void refresh()} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </Button>
      }
    >
      <LoopsSummaryStrip counts={counts} snapshot={snapshot} onRetry={() => void refresh()} />
      {jobs.length === 0 ? (
        <Card>
          <EmptyState
            title="No system loops found"
            message="No launchd jobs, CSI pipeline routines, or documented remote loops were discovered on this system."
          />
        </Card>
      ) : (
        <>
          <LoopsFilters
            categories={categories}
            filter={filter}
            onFilterChange={setFilter}
            sort={sort}
            onSortChange={setSort}
          />
          {filtered.length === 0 ? (
            <Card>
              <EmptyState message="No loops match the current filters." />
            </Card>
          ) : grouped ? (
            grouped.map((group) => {
              const groupCounts = summarizeHealth(group.jobs);
              const problemCount = groupCounts.failing + groupCounts.stale;
              return (
                <div key={group.category} className={styles.categorySection}>
                  <div className={styles.categoryHead}>
                    <h3 className={styles.categoryTitle}>{group.category}</h3>
                    <span className={styles.categoryCounts}>
                      {group.jobs.length} loop{group.jobs.length === 1 ? "" : "s"}
                      {problemCount > 0 ? ` · ${problemCount} need attention` : ""}
                    </span>
                  </div>
                  <ul className={styles.jobList}>
                    {group.jobs.map((job) => (
                      <LoopJobRow key={job.key} job={job} now={now} />
                    ))}
                  </ul>
                </div>
              );
            })
          ) : (
            <ul className={styles.flatList}>
              {flat!.map((job) => (
                <LoopJobRow key={job.key} job={job} now={now} />
              ))}
            </ul>
          )}
        </>
      )}
    </Section>
  );
}
