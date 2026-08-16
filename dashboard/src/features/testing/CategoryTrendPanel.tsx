"use client";

import { Card, ErrorState, LineChart, Loading } from "@/design";
import { useTestObsSeries } from "./hooks";
import { toLineSeries } from "./tiles";
import type { TestObsCategory } from "./types";
import styles from "./testing.module.css";

const CATEGORY_TITLES: Record<TestObsCategory, string> = {
  northstar: "North Star",
  memory: "Memory",
  diagnostics: "Diagnostics",
  suite: "Suite",
};

export type CategoryTrendPanelProps = {
  category: TestObsCategory;
  /** Query window in days (backend clamps 1..365, default 90). */
  days?: number;
};

/**
 * One of the 2x2 trend panels on /testing — a LineChart of every metric the
 * `/api/testobs/series` endpoint returns for this category. On API error the
 * chart's own ErrorState renders with a retry (errors propagate — this is
 * never silently swapped for fixture data). When the category itself is
 * unavailable (memory today) or every line is empty, a neutral message
 * renders instead of a coerced empty chart shell.
 */
export function CategoryTrendPanel({ category, days = 90 }: CategoryTrendPanelProps) {
  const { data, status, error, refresh } = useTestObsSeries(category, days);
  const title = CATEGORY_TITLES[category];

  return (
    <Card className={styles.trendCard}>
      <div className={styles.trendHead}>
        <h3 className={styles.trendTitle}>{title}</h3>
        {status === "ready" ? <span className={styles.trendMeta}>{days}d</span> : null}
      </div>

      {status === "loading" ? <Loading variant="skeleton" lines={5} label={`Loading ${title} trend`} /> : null}

      {status === "error" ? (
        <ErrorState message={error ?? `Could not load the ${title} trend`} onRetry={refresh} />
      ) : null}

      {status === "empty" ? (
        <p className={styles.trendUnavailable}>
          {data && !data.available ? data.reason : "No data yet — nothing measured in this window."}
        </p>
      ) : null}

      {status === "ready" && data?.available ? (
        <LineChart series={toLineSeries(data.series)} status="ready" aria-label={`${title} trend over ${days} days`} />
      ) : null}
    </Card>
  );
}
