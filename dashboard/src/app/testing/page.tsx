"use client";

import { Page, PageHeader } from "@/design";
import { CategoryTrendPanel, TESTOBS_CATEGORIES, TestObsStatRow, WeakSpotsPanel } from "@/features/testing";
import styles from "@/features/testing/testing.module.css";

/**
 * Observatory /testing — "every test lane visible." Read-side only: North
 * Star cert, Memory (memcert), Diagnostics (feature-health), and the general
 * Suite (merge-gate + lane JUnit), each with a stat tile, a trend chart, and
 * a shared weak-spots table ranking failures/refusals/stale cells across all
 * four. Memory renders honestly as absent today — memcert persists no
 * durable runs yet — never a fabricated zero or a green tile.
 */
export default function TestingPage() {
  return (
    <Page>
      <PageHeader
        title="Test Observatory"
        lead="Every test lane visible — North Star cert, Memory, Diagnostics, and the general Suite — broken down over time, with weak spots surfaced across all four."
      />
      <p className={styles.lead}>
        Trend charts show the last 90 days. The stat row above sparklines the
        last 30. Absence is rendered as absence — a category with no data
        yet (memory, today) shows a neutral state, never a coerced zero.
      </p>

      <TestObsStatRow />

      <div className={styles.trendGrid}>
        {TESTOBS_CATEGORIES.map((category) => (
          <CategoryTrendPanel key={category} category={category} />
        ))}
      </div>

      <WeakSpotsPanel />
    </Page>
  );
}
