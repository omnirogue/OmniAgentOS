import { Badge, Card, cx, Icon, Section } from "@/design";
import styles from "../cash.module.css";
import type { CashCoverage, DataQualityNote } from "../contracts";

export type DataQualityPanelProps = {
  notes: DataQualityNote[];
  coverage: CashCoverage;
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
 * The honesty surface: exactly which banks are feeding the numbers above vs.
 * which are known but not wired yet (Teller needs client-cert auth). Always
 * rendered — never collapsed, never behind a toggle — directly under the KPI
 * tiles so it cannot be missed. Mirrors features/revenue/DataQualityPanel.tsx.
 */
export function DataQualityPanel({ notes, coverage }: DataQualityPanelProps) {
  return (
    <Section
      eyebrow="Honesty check"
      title="Data quality & coverage"
      description="Which banks are actually feeding the numbers above, and what isn't wired up yet."
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

        <div className={styles.coverageGrid}>
          <CoverageGroup title="Banks live" items={coverage.banks_live} emptyLabel="None connected" />
          <CoverageGroup title="Not wired yet" items={coverage.not_wired} emptyLabel="Nothing outstanding" />
        </div>
      </Card>
    </Section>
  );
}
