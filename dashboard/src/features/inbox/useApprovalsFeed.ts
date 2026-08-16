"use client";

/** Data + mutation logic for the Inbox's Approvals tab, extracted verbatim
 * (same fetch shape, same SSE + 30s visibility-poll backstop, same monotonic
 * request-seq guard) from the former standalone `/approvals` page, so the
 * Inbox shell can read `pending.length` for the tab header badge without a
 * second fetch — the shell owns exactly one instance of this hook. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../lib/api";
import type { Approval, EventType } from "../../lib/contracts";
import { useEventChannel } from "../../lib/useEventChannel";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { fetchProjectApprovals, useProjectContext } from "../projects";

const APPROVAL_EVENT_TYPES: EventType[] = ["approval.requested", "approval.decided"];

const decisionTime = (approval: Approval) => approval.decided_at ?? approval.expires_at ?? approval.created_at;
const byRecency = (a: Approval, b: Approval) => Date.parse(decisionTime(b)) - Date.parse(decisionTime(a));

export interface ApprovalsFeed {
  pending: Approval[];
  decided: Approval[];
  loading: boolean;
  error: string | null;
  connected: boolean;
  streamError: boolean;
  refresh: () => Promise<void>;
  decide: (approval: Approval, decision: "approved" | "rejected", note?: string) => Promise<Approval>;
}

export function useApprovalsFeed(): ApprovalsFeed {
  const [pending, setPending] = useState<Approval[]>([]);
  const [decided, setDecided] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const watchedEvents = useMemo(() => APPROVAL_EVENT_TYPES, []);
  const { lastEvent, connected, error: streamError } = useEventChannel(watchedEvents);
  const { activeProjectId } = useProjectContext();
  // Monotonic request generation: a slower response for a now-stale project must
  // never overwrite the state of the currently active project (F10).
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    const projectId = activeProjectId;
    setLoading(true);
    const load = (state: string) =>
      projectId ? fetchProjectApprovals(projectId, state) : api.approvals(state);
    try {
      const [next, approved, rejected, expired] = await Promise.all([load("pending"), load("approved"), load("rejected"), load("expired")]);
      if (seq !== requestSeq.current) return;
      setPending(next);
      setDecided(
        [
          ...[...approved, ...rejected].sort(byRecency).slice(0, 20),
          ...expired.sort(byRecency).slice(0, 20),
        ].sort(byRecency),
      );
      setError(null);
    } catch (reason) {
      if (seq !== requestSeq.current) return;
      setError(reason instanceof Error ? reason.message : "Unable to load approvals.");
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [activeProjectId]);

  // Approvals arrive over SSE (approval.requested/decided below); the interval is
  // only a 30s safety backstop for when the stream is down, not a 10s poll.
  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 30000);
  }, [refresh]);
  useEffect(() => {
    if (lastEvent) void refresh();
  }, [lastEvent, refresh]);

  const decide = useCallback(
    async (approval: Approval, decision: "approved" | "rejected", note?: string): Promise<Approval> => {
      const updated = await api.decideApproval(approval.id, decision, note?.trim() || undefined);
      setPending((items) => items.filter((item) => item.id !== updated.id));
      setDecided((items) => [updated, ...items.filter((item) => item.id !== updated.id)].slice(0, 20));
      setError(null);
      void refresh();
      return updated;
    },
    [refresh],
  );

  return { pending, decided, loading, error, connected, streamError, refresh, decide };
}
