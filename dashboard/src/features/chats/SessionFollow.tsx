"use client";

/**
 * Session activity feed for the workspace drawer.
 *
 * It no longer fetches the session list or re-resolves which session belongs to
 * the chat — the drawer does that ONCE (useWorkspaceSession) and passes the
 * result in. This component owns exactly one thing: the transcript of the
 * session it is handed (initial load, 5s visible poll, session.updated push).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, EmptyState, Loading, Select } from "@/design";
import { api } from "@/lib/api";
import { useEventChannel } from "@/lib/useEventChannel";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { parseTranscript, type TranscriptEntry } from "@/lib/transcriptParser";
import type { Session } from "@/lib/contracts";
import styles from "./chats.module.css";

interface SessionFollowProps {
  sessions: Session[];
  /** "" when the chat follows no session. */
  sessionId: string;
  onSelectSession: (id: string) => void;
}

function formatTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function SessionFollow({ sessions, sessionId, onSelectSession }: SessionFollowProps) {
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const containerRef = useRef<HTMLDivElement>(null);
  const { lastEvent } = useEventChannel(["session.updated"]);

  // Load the transcript of the active session
  const loadTranscript = async (id: string) => {
    if (!id) return;
    setLoadingTranscript(true);
    try {
      const raw = await api.sessionTranscript(id);
      setTranscript(parseTranscript(raw));
      setError(null);
    } catch (err) {
      console.error("Failed to load session transcript:", err);
      setError("Could not load the session transcript.");
    } finally {
      setLoadingTranscript(false);
    }
  };

  useEffect(() => {
    if (sessionId) {
      void loadTranscript(sessionId);
    } else {
      setTranscript([]);
    }
  }, [sessionId]);

  // Poll transcript every 5s while the tab is visible
  useEffect(() => {
    if (!sessionId) return;
    const stopPoll = startVisibilityPoll(() => {
      void loadTranscript(sessionId);
    }, 5000);
    return stopPoll;
  }, [sessionId]);

  // session.updated for OUR session refetches immediately
  useEffect(() => {
    if (lastEvent?.type === "session.updated") {
      const eventSessionId =
        (lastEvent.payload &&
          typeof lastEvent.payload.session_id === "string" &&
          lastEvent.payload.session_id) ||
        (lastEvent.target_type === "session" ? lastEvent.target_id : "") ||
        "";
      if (eventSessionId && eventSessionId === sessionId) {
        void loadTranscript(sessionId);
      }
    }
  }, [lastEvent, sessionId]);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScroll(isAtBottom);
  };

  const handleJumpToLatest = () => {
    setAutoScroll(true);
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [transcript, autoScroll]);

  const sessionOptions = useMemo(
    () =>
      sessions.map((s) => ({
        value: s.id,
        label: s.title ? `${s.title} (${s.id.slice(0, 8)})` : s.id,
      })),
    [sessions],
  );

  const currentSession = useMemo(
    () => sessions.find((s) => s.id === sessionId) || null,
    [sessions, sessionId],
  );

  return (
    <div className={styles.sessionPanel}>
      <div className={styles.sessionHeader}>
        <div className={styles.sessionTitle} title={currentSession?.title || currentSession?.id || ""}>
          SESSION: {currentSession ? (currentSession.title ?? currentSession.id.slice(0, 12)) : "None selected"}
        </div>
        {sessionOptions.length > 0 && (
          <div className={styles.sessionSelectWrap}>
            <Select
              aria-label="Active Session"
              value={sessionId}
              onChange={onSelectSession}
              options={sessionOptions}
            />
          </div>
        )}
      </div>

      <div className={styles.activityContainer} ref={containerRef} onScroll={handleScroll}>
        {loadingTranscript && !transcript.length ? (
          <div className={styles.terminalLoadingWrap}>
            <Loading label="Tailing session log..." />
          </div>
        ) : error ? (
          <div className={styles.terminalError}>{error}</div>
        ) : transcript.length === 0 ? (
          <EmptyState
            title="No session transcripts"
            message="Logs will display here as agents execute tasks."
          />
        ) : (
          transcript.map((entry, idx) => {
            let actorClass = styles.terminalActor;
            if (entry.actor === "user") actorClass = styles.terminalActorUser;
            else if (entry.actor === "system") actorClass = styles.terminalActorSystem;

            return (
              <div key={`${entry.ts}-${idx}`} className={styles.terminalRow}>
                <div className={styles.terminalHead}>
                  <span>[{formatTime(entry.ts)}]</span>
                  <Badge tone="neutral">{entry.type}</Badge>
                  <span className={actorClass}>{entry.actor}</span>
                </div>
                <p className={styles.terminalSummary}>{entry.summary}</p>
              </div>
            );
          })
        )}
      </div>

      {!autoScroll && transcript.length > 0 && (
        <button className={styles.resumeButton} onClick={handleJumpToLatest}>
          ↓ Jump to latest
        </button>
      )}
    </div>
  );
}
