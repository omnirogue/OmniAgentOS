import { Card, Icon } from "@/design";
import { ACTION_CLASS_LABEL } from "./riskModel";
import type { GrantSummary } from "./types";
import styles from "./capabilities.module.css";

export type BlastRadiusPanelProps = {
  summary: GrantSummary;
  groupLabels: Record<string, string>;
};

/**
 * The live blast-radius panel — the most important thing on the create-agent page.
 * Answers three questions at a glance as the picker selection changes: what runs on
 * its own, what parks for a human, and what can never run without one. This is what
 * stops someone from accidentally creating an agent that can refund a customer.
 */
export function BlastRadiusPanel({ summary, groupLabels }: BlastRadiusPanelProps) {
  const autoCount =
    (summary.by_action_class.read_only ?? 0) +
    (summary.by_action_class.sandboxed_creation ?? 0) +
    (summary.by_action_class.internal_reversible ?? 0);
  const approvalCount = summary.needs_approval.length;
  const humanCount = summary.always_human.length;
  const dangerGroups = summary.touches_danger_group.map((id) => groupLabels[id] ?? id);
  const isEmpty = summary.total === 0;

  return (
    <Card raised className={styles.blastPanel} aria-label="Blast radius">
      <div className={styles.blastHead}>
        <h3 className={styles.blastTitle}>Blast radius</h3>
        <span className={styles.blastTotal}>{summary.total} capabilit{summary.total === 1 ? "y" : "ies"} selected</span>
      </div>

      {isEmpty ? (
        <p className={styles.blastEmpty}>No capabilities selected yet. This agent can do nothing.</p>
      ) : (
        <div className={styles.blastRows}>
          <div className={styles.blastRow}>
            <div className={styles.blastRowHead}>
              <span className={`${styles.blastDot} ${styles.blastDotAuto}`} aria-hidden="true" />
              <span className={styles.blastRowLabel}>Runs automatically</span>
              <span className={styles.blastRowCount}>{autoCount}</span>
            </div>
            <p className={styles.blastRowHint}>
              Reads data, creates sandboxed artifacts, or writes to our own systems (notes, tags, CDN uploads) —
              no approval required. {summary.auto_writes.length > 0
                ? `${summary.auto_writes.length} of these mutate something (auto-write).`
                : "None of these mutate anything."}
            </p>
          </div>

          <div className={styles.blastRow}>
            <div className={styles.blastRowHead}>
              <span className={`${styles.blastDot} ${styles.blastDotApproval}`} aria-hidden="true" />
              <span className={styles.blastRowLabel}>Parks for approval</span>
              <span className={styles.blastRowCount}>{approvalCount}</span>
            </div>
            <p className={styles.blastRowHint}>
              {approvalCount > 0
                ? "A customer or human sees the result. Every call waits for a human decision before it runs."
                : "This agent cannot do anything a customer would see without a human approving it first."}
            </p>
          </div>

          <div
            className={`${styles.blastRow} ${styles.blastRowHuman} ${humanCount > 0 ? styles.blastRowHumanActive : ""}`}
            role={humanCount > 0 ? "alert" : undefined}
          >
            <div className={styles.blastRowHead}>
              <span className={`${styles.blastDot} ${styles.blastDotHuman}`} aria-hidden="true" />
              <span className={styles.blastRowLabel}>
                {humanCount > 0 ? <Icon name="alertTriangle" size={14} /> : null}
                Always human
              </span>
              <span className={styles.blastRowCount}>{humanCount}</span>
            </div>
            <p className={styles.blastRowHint}>
              {humanCount > 0
                ? `${humanCount} capabilit${humanCount === 1 ? "y" : "ies"} can move money, spend ad budget, or touch infrastructure. These are hard-blocked in code — this agent can NEVER run them without a human approving in the moment, no matter what.`
                : "None selected. This agent cannot move money, spend ad budget, or touch infrastructure."}
            </p>
          </div>

          {dangerGroups.length > 0 ? (
            <div className={styles.blastDangerBanner} role="alert">
              <Icon name="alertTriangle" size={16} />
              <span>
                Touches danger group{dangerGroups.length > 1 ? "s" : ""}: <strong>{dangerGroups.join(", ")}</strong>
              </span>
            </div>
          ) : null}

          {summary.not_yet_callable.length > 0 ? (
            <p className={styles.blastFootnote}>
              {summary.not_yet_callable.length} of the selected capabilities are declared but not yet callable —
              fail-closed on purpose until their call path is reviewed.
            </p>
          ) : null}

          <dl className={styles.blastBreakdown}>
            {(Object.keys(ACTION_CLASS_LABEL) as Array<keyof typeof ACTION_CLASS_LABEL>).map((key) => (
              <div key={key} className={styles.blastBreakdownRow}>
                <dt>{ACTION_CLASS_LABEL[key]}</dt>
                <dd>{summary.by_action_class[key] ?? 0}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </Card>
  );
}
