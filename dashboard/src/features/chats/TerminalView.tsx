"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { useEventChannel } from "@/lib/useEventChannel";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { parseAnsi } from "./ansi";
import styles from "./chats.module.css";

interface TerminalViewProps {
  sessionId: string;
}

export function TerminalView({ sessionId }: TerminalViewProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [hasNewContent, setHasNewContent] = useState(false);
  
  const offsetRef = useRef<number>(0);
  const fetchingRef = useRef<boolean>(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef<boolean>(autoScroll);

  // Keep autoScrollRef in sync
  useEffect(() => {
    autoScrollRef.current = autoScroll;
  }, [autoScroll]);
  
  const { lastEvent } = useEventChannel(["session.updated"]);

  const fetchDelta = useCallback(async (isReset = false) => {
    if (!sessionId || fetchingRef.current) return;
    fetchingRef.current = true;
    
    try {
      const currentOffset = isReset ? 0 : offsetRef.current;
      const url = `/api/sessions/${encodeURIComponent(sessionId)}/transcript/delta?offset=${currentOffset}`;
      
      const data = await api.get<{
        raw: string;
        lines: string[];
        entries: unknown[];
        new_offset: number;
      }>(url);
      
      if (isReset) {
        setLines(data.lines);
        offsetRef.current = data.new_offset;
      } else {
        // Rotation guard reset-to-0 check
        if (data.new_offset < currentOffset) {
          setLines(data.lines);
          offsetRef.current = data.new_offset;
        } else if (data.lines.length > 0) {
          setLines((prev) => [...prev, ...data.lines]);
          offsetRef.current = data.new_offset;
          
          if (!autoScrollRef.current) {
            setHasNewContent(true);
          }
        }
      }
    } catch (err) {
      console.error("Failed to fetch terminal delta:", err);
    } finally {
      fetchingRef.current = false;
    }
  }, [sessionId]);

  // Reset state when session ID changes
  useEffect(() => {
    setLines([]);
    offsetRef.current = 0;
    setAutoScroll(true);
    setHasNewContent(false);
    void fetchDelta(true);
  }, [sessionId, fetchDelta]);

  // Immediate update on SSE session.updated events
  useEffect(() => {
    if (lastEvent?.type === "session.updated") {
      const eventSessionId =
        (lastEvent.payload && typeof lastEvent.payload.session_id === "string" && lastEvent.payload.session_id) ||
        (lastEvent.target_type === "session" ? lastEvent.target_id : "") ||
        "";
      if (eventSessionId && eventSessionId === sessionId) {
        void fetchDelta();
      }
    }
  }, [lastEvent, sessionId, fetchDelta]);

  // Regular poll fallback when tab is visible
  useEffect(() => {
    if (!sessionId) return;
    const stopPoll = startVisibilityPoll(() => {
      void fetchDelta();
    }, 5000);
    return stopPoll;
  }, [sessionId, fetchDelta]);

  // Handle manual scroll to detect when to pause autoScroll
  const handleScroll = () => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    // Standard margin of error for scroll math is 15px
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 25;
    
    if (isNearBottom) {
      setAutoScroll(true);
      setHasNewContent(false);
    } else {
      setAutoScroll(false);
    }
  };

  // Perform autoscroll when lines update and autoScroll is enabled
  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  const handleJumpToLatest = () => {
    setAutoScroll(true);
    setHasNewContent(false);
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  };

  return (
    <div className={styles.sessionPanel}>
      <div className={styles.sessionHeader}>
        <div className={styles.sessionTitle}>Live Terminal Stream</div>
        <div className={styles.terminalFollow}>
          <span
            className={`${styles.terminalFollowDot}${autoScroll ? "" : ` ${styles.terminalFollowDotPaused}`}`}
          />
          <span className={styles.terminalFollowLabel}>
            {autoScroll ? "LIVE FOLLOW" : "PAUSED"}
          </span>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        className={styles.terminalContainer}
      >
        {lines.length === 0 ? (
          <div className={styles.terminalWaiting}>
            Waiting for terminal output...
          </div>
        ) : (
          lines.map((line, idx) => {
            // Try parsing each line of JSONL to display beautifully if it is a structured event,
            // or just render it as raw ANSI stdout. ANSI span colors are
            // data-driven — the one legitimate inline-style exception.
            let displayNode;
            try {
              const parsed = JSON.parse(line);
              if (parsed.type === "event" || parsed.type === "result" || parsed.type === "system") {
                const summary = parsed.summary || parsed.text || parsed.message || "";
                displayNode = (
                  <span className={styles.terminalSystemText}>
                    <span className={styles.terminalSystemTag}>[SYSTEM] </span>
                    {summary}
                  </span>
                );
              } else if (parsed.type === "tool_call") {
                displayNode = (
                  <span className={styles.terminalToolText}>
                    <span className={styles.terminalToolTag}>[TOOL CALL] </span>
                    {parsed.tool_name} with input: {JSON.stringify(parsed.tool_input)}
                  </span>
                );
              } else {
                // If it is regular JSON, let's just parse ANSI over its raw summary/text
                const textVal = parsed.text || parsed.summary || parsed.message || line;
                displayNode = parseAnsi(textVal).map((span, sidx) => (
                  <span 
                    key={sidx} 
                    style={{ 
                      color: span.color, 
                      backgroundColor: span.bgColor, 
                      fontWeight: span.bold ? "bold" : "normal" 
                    }}
                  >
                    {span.text}
                  </span>
                ));
              }
            } catch {
              // Fallback to raw ANSI styling for standard raw text lines
              displayNode = parseAnsi(line).map((span, sidx) => (
                <span 
                  key={sidx} 
                  style={{ 
                    color: span.color, 
                    backgroundColor: span.bgColor, 
                    fontWeight: span.bold ? "bold" : "normal" 
                  }}
                >
                  {span.text}
                </span>
              ));
            }
            
            return (
              <div key={idx} className={styles.terminalLine}>
                {displayNode}
              </div>
            );
          })
        )}
      </div>

      {!autoScroll && (
        <button
          onClick={handleJumpToLatest}
          className={`${styles.resumeButton}${hasNewContent ? ` ${styles.resumeButtonNew}` : ""}`}
        >
          {hasNewContent ? "⬇ Jump to latest (New activity)" : "⬇ Jump to latest"}
        </button>
      )}
    </div>
  );
}
