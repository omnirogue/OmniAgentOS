"use client";

/**
 * PlanView (§2.6 Plan tab) — the planner's brief + checklist + workbook as
 * components. Every pre-v2 `<pre>` JSON dump is dead: the brief renders
 * markdown, the checklist renders the counts, the workbook renders via the
 * galaxy MarkdownBody.
 */

import { Badge, Card, EmptyState } from "@/design";
import { MarkdownBody } from "@/features/galaxy/markdown";
import type { LiveBoardTask, LonghaulTaskDetail } from "@/features/collab/types";
import styles from "./board.module.css";

export function PlanView({
  task,
  longhaul,
}: {
  task: LiveBoardTask;
  longhaul: LonghaulTaskDetail | null;
}) {
  const brief = task.planner_brief;
  const checklist = task.checklist ?? null;
  const workbook = longhaul?.workbook_content ?? "";

  if (!brief && !checklist && !workbook) {
    return (
      <EmptyState
        title="No plan was recorded"
        message="Cards created from a confirmed plan carry the planner's brief here."
      />
    );
  }

  return (
    <div className={styles.planGrid}>
      {brief ? (
        <Card padding="sm">
          <h4 className={styles.sectionTitle}>Planner brief</h4>
          <div className={styles.markdownWrap}>
            <MarkdownBody markdown={brief} resolveWikilink={() => null} />
          </div>
        </Card>
      ) : task.description ? (
        <Card padding="sm">
          <h4 className={styles.sectionTitle}>Refined spec</h4>
          <pre className={styles.proseBlock}>{task.description}</pre>
        </Card>
      ) : null}

      {checklist ? (
        <Card padding="sm">
          <h4 className={styles.sectionTitle}>Checklist</h4>
          <div className={styles.progressHead}>
            <strong>
              {checklist.done}/{checklist.total} done
            </strong>
            <Badge tone={checklist.done === checklist.total ? "completed" : "running"}>
              {checklist.total > 0 ? Math.round((checklist.done / checklist.total) * 100) : 0}%
            </Badge>
          </div>
          <div className={styles.progressTrack}>
            <div
              className={styles.progressFill}
              style={{
                "--pct": `${checklist.total > 0 ? Math.round((checklist.done / checklist.total) * 100) : 0}%`,
              } as React.CSSProperties}
              role="progressbar"
              aria-valuenow={checklist.total > 0 ? Math.round((checklist.done / checklist.total) * 100) : 0}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
        </Card>
      ) : null}

      {workbook ? (
        <Card padding="sm">
          <h4 className={styles.sectionTitle}>Workbook</h4>
          <div className={styles.markdownWrap}>
            <MarkdownBody markdown={workbook} resolveWikilink={() => null} />
          </div>
        </Card>
      ) : null}
    </div>
  );
}
