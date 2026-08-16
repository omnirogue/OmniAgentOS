"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import { useEventChannel } from "../../lib/useEventChannel";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import {
  fetchNotifications,
  fetchUnreadCount,
  markAllNotificationsRead,
  markNotificationRead,
  NotificationApiError,
  type NotificationCountResponse,
} from "./api";
import type { NotificationRow } from "./types";

const POLL_MS = 15_000;
// Refresh the feed when any of these fire, so a new approval/alert appears within
// a few seconds without waiting for the poll (mirrors the Steward SSE pattern).
const REFRESH_EVENTS = ["approval.requested", "approval.decided", "alert.created", "board.updated"];

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof NotificationApiError || reason instanceof Error ? reason.message : fallback;
}

export interface UseNotificationsResult {
  notifications: NotificationRow[];
  unreadCount: number;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  markRead: (id: string) => Promise<void>;
  /** Bulk mark-all-read. Resolves `{ ok: false }` (never throws) when the
   * backend route hasn't shipped yet (404) — the caller (NotificationsBell)
   * decides how to surface that, typically a toast. */
  markAllRead: () => Promise<{ ok: boolean }>;
  decideApproval: (
    notification: NotificationRow,
    decision: "approved" | "rejected",
  ) => Promise<void>;
}

export function useNotifications(): UseNotificationsResult {
  const [notifications, setNotifications] = useState<NotificationRow[]>([]);
  const [countResponse, setCountResponse] = useState<NotificationCountResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Monotonic request generation: a slow response must never overwrite newer state.
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      // The count endpoint is a cheap, independent read — a slow/failing count
      // must never block the notification list itself from rendering.
      const [rows, count] = await Promise.all([
        fetchNotifications(false, 50),
        fetchUnreadCount().catch(() => null),
      ]);
      if (seq !== requestSeq.current) return;
      setNotifications(rows);
      setCountResponse(count);
      setError(null);
    } catch (reason) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(reason, "Unable to load notifications."));
    } finally {
      // Bounded loading: the fetch has a hard 10s timeout, so this always settles.
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  const markRead = useCallback(async (id: string) => {
    try {
      const updated = await markNotificationRead(id);
      setNotifications((rows) => rows.map((row) => (row.id === id ? updated : row)));
    } catch {
      // Non-fatal: a failed read-marking should not surface as a blocking error.
    }
  }, []);

  const markAllRead = useCallback(async (): Promise<{ ok: boolean }> => {
    try {
      await markAllNotificationsRead();
      await refresh();
      return { ok: true };
    } catch (reason) {
      // Not shipped yet on this server — a graceful no-op, not a crash.
      if (reason instanceof NotificationApiError && reason.status === 404) {
        return { ok: false };
      }
      throw reason;
    }
  }, [refresh]);

  const decideApproval = useCallback(
    async (notification: NotificationRow, decision: "approved" | "rejected") => {
      const approvalId = notification.target.approval_id ?? notification.ref_id;
      if (!approvalId) throw new NotificationApiError("This notification has no approval to decide.", 0);
      // Reuse the Approvals decide path (same-origin token proxy).
      await api.decideApproval(approvalId, decision);
      // Mark it read locally; a full refresh re-resolves the target to its outcome.
      await markRead(notification.id);
      await refresh();
    },
    [markRead, refresh],
  );

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS);
  }, [refresh]);

  // The bell mounts on EVERY page (design/AppShell.tsx), so it rides the ONE
  // shared per-tab EventSource owned by EventStreamProvider (T1.4) rather than
  // opening its own — three always-on hooks used to burn half the browser's
  // 6-connections-per-origin HTTP/1.1 budget before a page rendered anything.
  const { lastEvent } = useEventChannel(REFRESH_EVENTS);
  useEffect(() => {
    if (!lastEvent) return;
    void refresh();
  }, [lastEvent, refresh]);

  // The bell's badge is scoped to ACTIONABLE unread (e.g. a pending approval),
  // never the raw unread total, so a pile of already-resolved/info rows never
  // makes the badge cry wolf. Preference order: the count endpoint's
  // `actionable` field (post-backend-commit) -> its plain `unread` total
  // (pre-backend-commit — the param was silently ignored) -> the locally
  // derived total if the count endpoint itself couldn't be reached at all.
  const locallyDerivedTotal = notifications.reduce((sum, row) => (row.read ? sum : sum + 1), 0);
  const unreadCount = countResponse?.actionable ?? countResponse?.unread ?? locallyDerivedTotal;

  return { notifications, unreadCount, loading, error, refresh, markRead, markAllRead, decideApproval };
}
