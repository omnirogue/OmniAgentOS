"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Icon, useToast, type BadgeTone } from "../../design";
import { cx } from "../../design/utils";
import { useNotifications } from "./hooks";
import type { NotificationRow } from "./types";
import styles from "./notifications.module.css";

function age(iso: string): string {
  const minutes = Math.floor(Math.max(0, Date.now() - Date.parse(iso)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

function severityTone(severity: string): BadgeTone {
  switch (severity) {
    case "critical":
      return "danger";
    case "high":
      return "reject";
    case "warning":
      return "warn";
    default:
      return "neutral";
  }
}

function stateTone(state: string | null): BadgeTone {
  switch (state) {
    case "approved":
      return "completed";
    case "rejected":
    case "expired":
      return "failed";
    case "resolved":
      return "neutral";
    default:
      return "awaiting";
  }
}

/** A single feed row. Approval-kind rows that are still pending expose inline
 * Approve/Reject; resolved ones show their outcome (never a dead link). */
function NotificationItem({
  row,
  onOpen,
  onDecide,
  busy,
}: {
  row: NotificationRow;
  onOpen: (row: NotificationRow) => void;
  onDecide: (row: NotificationRow, decision: "approved" | "rejected") => void;
  busy: boolean;
}) {
  const target = row.target;
  const isApproval = row.kind === "approval" || target.type === "approval";
  const actionable = isApproval && target.actionable;
  const resolved = isApproval && target.resolved;
  // A done-kind (board_task) row deep-links to the finished card's files drawer.
  // A swarm_failed row also targets a board card (or the run itself) but there
  // are no deliverables to advertise — it must never read as "View files".
  const isFailure = row.kind === "swarm_failed";
  const isDone = !isFailure && (row.kind === "done" || target.type === "board_task");
  const filesCount = typeof target.files_count === "number" ? target.files_count : null;
  const bodyText =
    isDone && filesCount && filesCount > 0
      ? `${row.body ? `${row.body} · ` : ""}${filesCount} file${filesCount === 1 ? "" : "s"}`
      : row.body;

  return (
    <div className={cx(styles.item, !row.read && styles.itemUnread)}>
      <div className={styles.itemTop}>
        <button type="button" className={styles.itemMain} onClick={() => onOpen(row)}>
          <span className={styles.itemTitle}>
            {!row.read ? <span className={styles.dot} aria-hidden="true" /> : null}
            {row.title}
          </span>
          {bodyText ? <span className={styles.itemBody}>{bodyText}</span> : null}
        </button>
        <Badge tone={severityTone(row.severity)}>{row.kind}</Badge>
      </div>

      <div className={styles.itemMeta}>
        <time dateTime={row.created_at}>{age(row.created_at)}</time>
        {isApproval && target.state ? (
          <Badge tone={stateTone(target.state)}>
            {resolved ? (target.state === "resolved" ? "resolved" : target.state) : target.state}
          </Badge>
        ) : null}
      </div>

      {actionable ? (
        <div className={styles.actions}>
          <Button size="sm" variant="primary" disabled={busy} onClick={() => onDecide(row, "approved")}>
            {busy ? "Saving…" : "Approve"}
          </Button>
          <Button size="sm" variant="danger" disabled={busy} onClick={() => onDecide(row, "rejected")}>
            Reject
          </Button>
        </div>
      ) : isDone && target.href ? (
        <div className={styles.actions}>
          <Button size="sm" variant="primary" onClick={() => onOpen(row)}>
            {filesCount && filesCount > 0 ? `View files · ${filesCount}` : "View files"}
          </Button>
        </div>
      ) : isFailure && target.href ? (
        <div className={styles.actions}>
          <Button size="sm" variant="danger" onClick={() => onOpen(row)}>
            {target.type === "board_task" ? "View task" : "View run"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function NotificationsBell() {
  const router = useRouter();
  const { push } = useToast();
  const {
    notifications,
    unreadCount,
    loading,
    error,
    refresh,
    markRead,
    markAllRead,
    decideApproval,
  } = useNotifications();
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [markingAll, setMarkingAll] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const openTarget = useCallback(
    (row: NotificationRow) => {
      if (!row.read) void markRead(row.id);
      const href = row.target.href;
      if (href) {
        setOpen(false);
        router.push(href);
      }
    },
    [markRead, router],
  );

  const decide = useCallback(
    async (row: NotificationRow, decision: "approved" | "rejected") => {
      setBusyId(row.id);
      setActionError(null);
      try {
        await decideApproval(row, decision);
      } catch (reason) {
        setActionError(reason instanceof Error ? reason.message : "Unable to decide this approval.");
      } finally {
        setBusyId(null);
      }
    },
    [decideApproval],
  );

  // Bulk mark-all-read (POST /api/notifications/read-all). Pre-backend-commit
  // that route 404s — `markAllRead` reports `{ ok: false }` rather than
  // throwing, so the graceful path here is a toast, never a crash.
  const onMarkAllRead = useCallback(async () => {
    setMarkingAll(true);
    try {
      const result = await markAllRead();
      if (!result.ok) {
        push({
          title: "Not available yet",
          message: "Bulk mark-all-read isn't available on this server yet.",
          tone: "warn",
        });
      }
    } catch (reason) {
      push({
        title: "Mark all read failed",
        message: reason instanceof Error ? reason.message : "Could not mark notifications read.",
        tone: "error",
      });
    } finally {
      setMarkingAll(false);
    }
  }, [markAllRead, push]);

  const hasUnread = notifications.some((row) => !row.read);

  return (
    <div className={styles.root}>
      <button
        type="button"
        className={cx("ds-icon-btn", styles.bellButton)}
        aria-label={unreadCount > 0 ? `${unreadCount} unread notifications` : "Notifications"}
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
          if (!open) void refresh();
        }}
      >
        <Icon name="inbox" size={16} />
        {unreadCount > 0 ? (
          <span className={styles.count} aria-hidden="true">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        ) : null}
      </button>

      {open ? (
        <>
          <button
            type="button"
            className={styles.backdrop}
            aria-label="Close notifications"
            onClick={() => setOpen(false)}
          />
          <div className={styles.panel} ref={panelRef} role="dialog" aria-label="Notifications">
            <div className={styles.header}>
              <span className={styles.headerTitle}>Notifications</span>
              <button
                type="button"
                className={styles.markAll}
                onClick={() => void onMarkAllRead()}
                disabled={!hasUnread || markingAll}
              >
                {markingAll ? "Marking…" : "Mark all read"}
              </button>
            </div>

            {actionError ? <div className={cx(styles.state, styles.errorState)}>{actionError}</div> : null}
            {error ? (
              <div className={cx(styles.state, styles.errorState)}>{error}</div>
            ) : loading && notifications.length === 0 ? (
              <div className={styles.state}>Loading…</div>
            ) : notifications.length === 0 ? (
              <div className={styles.state}>No notifications</div>
            ) : (
              <div className={styles.list}>
                {notifications.map((row) => (
                  <NotificationItem
                    key={row.id}
                    row={row}
                    onOpen={openTarget}
                    onDecide={decide}
                    busy={busyId === row.id}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
