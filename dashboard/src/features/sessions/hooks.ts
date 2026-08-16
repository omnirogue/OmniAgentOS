"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../lib/api";
import type { Approval, EventType, Session } from "../../lib/contracts";
import { useEvents } from "../../lib/useEvents";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { FIXTURE_APPROVALS, FIXTURE_SESSIONS, USE_FIXTURES } from "./fixtures";

const eventTypes: EventType[] = ["session.updated", "approval.requested", "approval.decided"];

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // GET /api/sessions is token-gated (omniagentos/api/routes/sessions.py's
  // `_authorized`) and this hook calls it directly from the browser with no
  // token — a browser that isn't the operator's authorized local session
  // gets a 401 on every poll. That is an expected, permanent condition for
  // this browser/session, not a transient failure: surface it distinctly so
  // the page can show a calm explanation instead of an alarming "error +
  // Retry" that will just fail the same way again every 10s.
  const [unauthorized, setUnauthorized] = useState(false);
  const watchedEvents = useMemo(() => eventTypes, []);
  const { lastEvent, connected } = useEvents(watchedEvents);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      if (USE_FIXTURES) {
        setSessions([...FIXTURE_SESSIONS]);
        setApprovals([...FIXTURE_APPROVALS]);
      } else {
        const [nextSessions, nextApprovals] = await Promise.all([api.sessions(), api.approvals("pending")]);
        setSessions(nextSessions);
        setApprovals(nextApprovals.filter((approval) => !!approval.session_id));
      }
      setError(null);
      setUnauthorized(false);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setUnauthorized(true);
        setError(null);
      } else {
        setUnauthorized(false);
        setError(reason instanceof Error ? reason.message : "Unable to load sessions.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Live sessions are driven by the `session.updated` SSE push (below); this
    // interval is only a 30s safety backstop for a browser whose SSE never
    // connects — no longer a 10s poll.
    return startVisibilityPoll(() => void refresh(), 30_000);
  }, [refresh]);

  useEffect(() => {
    if (USE_FIXTURES) return;
    if (lastEvent) void refresh();
  }, [lastEvent, refresh]);

  return {
    sessions,
    approvals,
    loading,
    error,
    unauthorized,
    refresh,
    connected: USE_FIXTURES ? false : connected,
  };
}
