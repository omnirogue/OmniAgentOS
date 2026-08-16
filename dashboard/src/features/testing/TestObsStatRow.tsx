"use client";

import { ErrorState, Loading, Sparkline } from "@/design";
import { useTestObsOverview, useTestObsSeries } from "./hooks";
import { shapeStat, type TestObsStatItem } from "./tiles";
import type { TestObsCategory } from "./types";
import styles from "./testing.module.css";

const CATEGORIES: readonly TestObsCategory[] = ["northstar", "memory", "diagnostics", "suite"];

/** Stat row's own sparkline window — a short recent trend, independent of
 * the larger trend panels below (which default to 90d). */
const SPARK_DAYS = 30;

/**
 * Four stat tiles — North Star, Memory, Diagnostics, Suite — each a
 * headline number + caption + short sparkline. Mirrors PulseTile.tsx's
 * loading/empty/error handling but these tiles are not deep-links (there is
 * no separate page per category on /testing, the trend panels below cover
 * the same categories on the same page).
 */
export function TestObsStatRow() {
  const overview = useTestObsOverview();
  const northstar = useTestObsSeries("northstar", SPARK_DAYS);
  const memory = useTestObsSeries("memory", SPARK_DAYS);
  const diagnostics = useTestObsSeries("diagnostics", SPARK_DAYS);
  const suite = useTestObsSeries("suite", SPARK_DAYS);

  const seriesByCategory: Record<TestObsCategory, typeof northstar> = {
    northstar,
    memory,
    diagnostics,
    suite,
  };

  if (overview.status === "loading") {
    return (
      <div className={styles.statGrid} aria-busy="true">
        {CATEGORIES.map((c) => (
          <div key={c} className={styles.statTile}>
            <div className={styles.statHead}>
              <span className={styles.statTitle}>…</span>
            </div>
            <Loading variant="skeleton" lines={2} />
          </div>
        ))}
      </div>
    );
  }

  if (overview.status === "error" || !overview.data) {
    return (
      <div className={styles.statGrid}>
        <div className={styles.statTile}>
          <ErrorState
            title="Test observability unavailable"
            message={overview.error ?? "Could not load the testobs overview"}
            onRetry={overview.refresh}
          />
        </div>
      </div>
    );
  }

  const items: TestObsStatItem[] = CATEGORIES.map((category) => {
    const res = seriesByCategory[category];
    const lines = res.data?.available ? res.data.series : [];
    return shapeStat(category, overview.data!, lines);
  });

  return (
    <div className={styles.statGrid}>
      {items.map((item) => (
        <StatTile key={item.category} item={item} />
      ))}
    </div>
  );
}

function StatTile({ item }: { item: TestObsStatItem }) {
  const valueCls =
    item.tone !== "default"
      ? `${styles.statValue} ${styles[`statValue--${item.tone}`]}`
      : styles.statValue;
  const sparkTone = item.tone === "default" ? "accent" : item.tone;

  return (
    <div className={`${styles.statTile} ${item.available ? "" : styles["statTile--unavailable"]}`}>
      <div className={styles.statHead}>
        <span className={styles.statTitle}>{item.title}</span>
        {item.sparkPoints.length >= 2 ? (
          <span className={styles.statSpark}>
            <Sparkline points={item.sparkPoints} width={100} height={28} tone={sparkTone} aria-label={`${item.title} trend`} />
          </span>
        ) : null}
      </div>
      <span className={valueCls}>{item.primary}</span>
      <p className={styles.statCaption}>{item.caption}</p>
    </div>
  );
}
