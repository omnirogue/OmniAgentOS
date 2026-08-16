"use client";

import Link from "next/link";
import { type MouseEvent as ReactMouseEvent, useState } from "react";
import { Button, EmptyState, cx } from "@/design";
import { compactRelative, relativeTime } from "@/features/portfolio/format";
import portfolioStyles from "@/features/portfolio/portfolio.module.css";
import type { LiveBoardTask } from "@/features/collab/types";
import { selectNeedsResponse } from "./needsResponse";
import styles from "./needsResponse.module.css";

/**
 * Ranked Needs Response band — AttentionQueue pattern for conversations.
 *
 * Reuses portfolio QUEUE_BANDS layout (band heading, state stripe, count chips).
 * Ship no sort control: server/selectNeedsResponse order IS the product.
 * Each row shows the real pending command from params_json, never "Bash".
 */
export type NeedsResponseQueueProps = {
  tasks: LiveBoardTask[];
  /** When provided, Approve decides the bound approval in place. */
  onApprove?: (approvalId: string, taskId: string, event: ReactMouseEvent) => void | Promise<void>;
  approvingId?: string | null;
  /** Empty-state copy when the strict filter yields nothing. */
  emptyMessage?: string;
  /** Hide the empty state entirely (e.g. cockpit: only show when non-empty). */
  hideWhenEmpty?: boolean;
};

export function NeedsResponseQueue({
  tasks,
  onApprove,
  approvingId = null,
  emptyMessage = "Nothing is waiting on you. Blocked cards and open suggestions stay in their own surfaces.",
  hideWhenEmpty = false,
}: NeedsResponseQueueProps) {
  const items = selectNeedsResponse(tasks);
  const [localBusy, setLocalBusy] = useState<string | null>(null);

  if (items.length === 0) {
    if (hideWhenEmpty) return null;
    return (
      <div className={styles.wrap} aria-label="Needs Response">
        <div className={portfolioStyles.qhead}>
          <h2>Needs Response</h2>
          <span className={portfolioStyles.why}>only work that cannot proceed until you act</span>
        </div>
        <div className={styles.empty}>
          <EmptyState title="Clear" message={emptyMessage} />
        </div>
      </div>
    );
  }

  const handleApprove = async (
    approvalId: string,
    taskId: string,
    event: ReactMouseEvent,
  ) => {
    if (!onApprove) return;
    setLocalBusy(approvalId);
    try {
      await onApprove(approvalId, taskId, event);
    } finally {
      setLocalBusy(null);
    }
  };

  return (
    <div className={styles.wrap} aria-label="Needs Response">
      <div className={portfolioStyles.qhead}>
        <h2>Needs Response</h2>
        <span className={portfolioStyles.why}>
          ordered by consequence — no sort control
        </span>
      </div>

      <section aria-label="Waiting on you">
        <div className={cx(portfolioStyles.band, portfolioStyles.bandNeeds)}>
          Waiting on you
          <span className={cx(portfolioStyles.chip, portfolioStyles.chipBlock)}>
            {items.length}
          </span>
          <i className={portfolioStyles.bandRule} aria-hidden="true" />
        </div>

        {items.map((task) => {
          const approval = task.pending_approval ?? null;
          const command = approval?.command ?? "";
          const actionClass = approval?.action_class ?? "";
          const busy = approvingId === approval?.id || localBusy === approval?.id;
          const when = approval?.created_at ?? task.updated_at ?? task.created_at;
          return (
            <div key={task.id} className={cx(portfolioStyles.row, styles.itemRow)}>
              <div
                className={cx(portfolioStyles.stripe, portfolioStyles.stripeBlock)}
                aria-hidden="true"
              />
              <div className={portfolioStyles.rmid}>
                <div className={portfolioStyles.rtop}>
                  <Link
                    href={`/activity/${encodeURIComponent(task.id)}?kind=board`}
                    className={styles.titleLink}
                  >
                    <span className={portfolioStyles.pname}>{task.title}</span>
                  </Link>
                  <span className={cx(portfolioStyles.chip, portfolioStyles.chipBlock)}>
                    needs you
                  </span>
                  {actionClass ? (
                    <span className={portfolioStyles.chip}>
                      {actionClass.replace(/_/g, " ")}
                    </span>
                  ) : null}
                  {task.priority && task.priority !== "normal" ? (
                    <span className={portfolioStyles.chip}>{task.priority}</span>
                  ) : null}
                </div>
                {command ? (
                  <code className={styles.command} title={command}>
                    {command}
                  </code>
                ) : (
                  <div className={portfolioStyles.doing}>
                    Waiting for an approval decision
                  </div>
                )}
              </div>
              <div className={cx(portfolioStyles.rright, styles.actions)}>
                {approval && onApprove ? (
                  <Button
                    variant="primary"
                    size="sm"
                    aria-label={`Approve ${command.slice(0, 60) || task.title}`}
                    disabled={busy}
                    onClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      void handleApprove(approval.id, task.id, event);
                    }}
                  >
                    {busy ? "Approving…" : "Approve"}
                  </Button>
                ) : null}
                <span className={portfolioStyles.when} title={relativeTime(when)}>
                  {compactRelative(when)}
                </span>
              </div>
            </div>
          );
        })}
      </section>
    </div>
  );
}
