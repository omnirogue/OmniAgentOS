"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Dialog, EmptyState, ErrorState, Loading } from "@/design";
import { api } from "@/lib/api";
import type { Session } from "@/lib/contracts";
import {
  extractFilesTouched,
  parseTranscript,
  type TranscriptEntry,
} from "@/lib/transcriptParser";
import type { SwarmBoardTask } from "./types";
import styles from "./swarm.module.css";

function formatTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Resolve the board card a session backs — a swarm/longhaul card links its
 * session via result_ref (`ses_…`). Returns its id, or null. */
function boardTaskForSession(session: Session, boardTasks: SwarmBoardTask[]): string | null {
  const match = boardTasks.find(
    (task) => task.result_ref === session.id || task.result_ref === session.session_ref,
  );
  return match?.id ?? null;
}

export function TerminalHistoryDrawer({
  session,
  boardTasks,
  open,
  onClose,
}: {
  session: Session | null;
  boardTasks: SwarmBoardTask[];
  open: boolean;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const sessionId = session?.id ?? null;

  const load = useCallback(async () => {
    if (!sessionId) return;
    const gen = ++generation.current;
    setLoading(true);
    try {
      const transcript = await api.sessionTranscript(sessionId);
      if (gen !== generation.current) return;
      setEntries(parseTranscript(transcript));
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(reason instanceof Error ? reason.message : "Could not load the transcript.");
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (!open || !sessionId) return;
    setEntries([]);
    setError(null);
    void load();
  }, [open, sessionId, load]);

  const filesTouched = session?.files?.length ? session.files : extractFilesTouched(entries);
  const boardId = session ? boardTaskForSession(session, boardTasks) : null;
  const recent = [...entries]
    .sort((a, b) => Date.parse(b.ts || "0") - Date.parse(a.ts || "0"))
    .slice(0, 40);

  return (
    <Dialog
      open={open && Boolean(session)}
      onClose={onClose}
      title={session ? (session.title ?? session.id) : "Terminal"}
      footer={
        <div className={styles.drawerFooter}>
          <span>
            {boardId ? (
              <Link href={`/activity/${encodeURIComponent(boardId)}?kind=board`}>View board card</Link>
            ) : session ? (
              <Link href={`/activity/${encodeURIComponent(session.id)}?kind=session`}>
                View session activity
              </Link>
            ) : null}
          </span>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      }
    >
      {session ? (
        <div className={styles.drawer}>
          <div className={styles.badgeRow}>
            <Badge tone="neutral">{session.provider}</Badge>
            {session.model ? <Badge tone="neutral">{session.model}</Badge> : null}
            <Badge tone="neutral">{session.state.replaceAll("_", " ")}</Badge>
            <span className={styles.muted}>{session.project_dir}</span>
          </div>

          <div>
            <strong>Files touched</strong>
            {filesTouched.length ? (
              <ul className={styles.fileList}>
                {filesTouched.slice(0, 20).map((file) => (
                  <li key={file}>
                    <code>{file}</code>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.muted}>No file activity recorded yet.</p>
            )}
          </div>

          <div>
            <strong>Transcript</strong>
            {loading ? <Loading label="Loading transcript…" /> : null}
            {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
            {!loading && !error && recent.length === 0 ? (
              <EmptyState message="No transcript entries recorded for this session yet." />
            ) : null}
            {!loading && !error && recent.length > 0 ? (
              <div className={styles.transcriptList} aria-live="polite">
                {recent.map((entry, index) => (
                  <div key={`${entry.ts}-${index}`} className={styles.transcriptEntry}>
                    <div className={styles.transcriptHead}>
                      <span>{formatTime(entry.ts)}</span>
                      <Badge tone="neutral">{entry.type}</Badge>
                      <span>{entry.actor}</span>
                    </div>
                    <p className={styles.transcriptSummary}>{entry.summary}</p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
