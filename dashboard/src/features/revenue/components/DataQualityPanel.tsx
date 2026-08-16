import { Badge, Card, cx, Icon, Section } from "@/design";
import styles from "../revenue.module.css";
import type { DataQualityNote, RevenueCoverage, SourceStatus } from "../contracts";

export type DataQualityPanelProps = {
  notes: DataQualityNote[];
  coverage: RevenueCoverage;
  sourceStatus?: SourceStatus[];
};

function CoverageGroup({ title, items, emptyLabel }: { title: string; items: string[]; emptyLabel: string }) {
  return (
    <div className={styles.coverageGroup}>
      <p className={styles.coverageGroupTitle}>{title}</p>
      <div className={styles.coverageTags}>
        {items.length > 0 ? (
          items.map((item) => (
            <Badge key={item} tone="neutral">
              {item}
            </Badge>
          ))
        ) : (
          <span className={styles.coverageEmpty}>{emptyLabel}</span>
        )}
      </div>
    </div>
  );
}

/**
 * The honesty surface (REVENUE-DASH.md item 5): exactly what is included in
 * the numbers above vs. what is not wired yet. Always rendered — never
 * collapsed, never behind a toggle — directly under the KPI tiles so it
 * cannot be missed. `warn` notes render amber, `info` neutral; neither tone
 * is meant to read as alarming, only as plainly informative.
 */
export function DataQualityPanel({ notes, coverage, sourceStatus = [] }: DataQualityPanelProps) {
  return (
    <Section
      eyebrow="Honesty check"
      title="Data quality & coverage"
      description="What's actually included in the numbers above, and what isn't wired up yet."
    >
      <Card raised padding="lg" className={styles.qualityPanel}>
        {notes.length > 0 ? (
          <ul className={styles.qualityList}>
            {notes.map((note, i) => (
              <li
                key={i}
                className={cx(styles.qualityNote, note.level === "warn" ? styles.qualityNoteWarn : styles.qualityNoteInfo)}
              >
                <span className={styles.qualityNoteIcon} aria-hidden="true">
                  <Icon name={note.level === "warn" ? "alertTriangle" : "helpCircle"} size={14} />
                </span>
                <span>
                  <Badge tone={note.level === "warn" ? "warn" : "neutral"} style={{ marginRight: "var(--space-2)" }}>
                    {note.level === "warn" ? "Warn" : "Info"}
                  </Badge>
                  {note.message}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.muted}>No data quality notes for this day.</p>
        )}

        <div className={styles.coverageGroup}>
          <p className={styles.coverageGroupTitle}>Source collection status</p>
          <div className={styles.coverageTags}>
            {sourceStatus.length > 0 ? (
              sourceStatus.map((item) => (
                <span key={`${item.vertical}:${item.source}`}>
                  <Badge tone={item.status === "failed" ? "warn" : "neutral"} style={{ marginRight: "var(--space-2)" }}>
                    {item.status}
                  </Badge>
                  {item.vertical} {item.source}: {item.message || "No detail recorded"}
                  {item.status === "failed" ? ` (${item.consecutive_failures} consecutive failure(s))` : ""}
                </span>
              ))
            ) : (
              <span className={styles.coverageEmpty}>No collector status has been recorded yet.</span>
            )}
          </div>
        </div>

        <div className={styles.coverageGrid}>
          <CoverageGroup title="Revenue sources live" items={coverage.revenue_sources_live} emptyLabel="None connected" />
          <CoverageGroup title="Spend sources live" items={coverage.spend_sources_live} emptyLabel="None connected" />
          <CoverageGroup title="Not wired yet" items={coverage.not_wired} emptyLabel="Nothing outstanding" />
        </div>
      </Card>
    </Section>
  );
}
