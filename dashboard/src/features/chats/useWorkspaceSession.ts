"use client";

/**
 * ONE session-list fetch for the workspace drawer.
 *
 * SessionFollow and WorkspaceTabs each used to run the *same* `GET /api/sessions`
 * + task-session resolution on mount, so opening the drawer fired the whole
 * workspace session list twice and the two copies could disagree about which
 * session was "the" one. The drawer now owns a single instance of this hook and
 * hands the result down.
 *
 * Resolution order (the misleading third step is deliberately gone):
 *   1. companion task → its live session, else its most recent session
 *   2. project id / project name → the first session whose dir or title matches
 *   3. nothing — a chat with no companion task follows NO session. Showing the
 *      machine's most recent session here implied a link that does not exist.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { collabApi } from "@/features/collab/client";
import type { Session } from "@/lib/contracts";

export interface WorkspaceSessionArgs {
  taskId?: string | null;
  projectId?: string | null;
  projectName?: string | null;
}

export interface WorkspaceSessionState {
  sessions: Session[];
  /** "" when no session is linked to this chat. */
  sessionId: string;
  selectSession: (id: string) => void;
  loading: boolean;
}

function matchByProject(
  sessions: Session[],
  projectId?: string | null,
  projectName?: string | null,
): Session | undefined {
  const byId = (projectId ?? "").toLowerCase();
  const byName = (projectName ?? "").toLowerCase();
  if (!byId && !byName) return undefined;
  return sessions.find((session) => {
    const dir = (session.project_dir ?? "").toLowerCase();
    const title = (session.title ?? "").toLowerCase();
    return (
      (byId !== "" && dir.includes(byId)) ||
      (byName !== "" && (dir.includes(byName) || title.includes(byName)))
    );
  });
}

export function useWorkspaceSession({
  taskId,
  projectId,
  projectName,
}: WorkspaceSessionArgs): WorkspaceSessionState {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    const resolve = async () => {
      let all: Session[] = [];
      try {
        all = [...(await api.sessions())].sort(
          (a, b) => Date.parse(b.created_at || "0") - Date.parse(a.created_at || "0"),
        );
      } catch {
        all = [];
      }
      if (cancelled) return;
      setSessions(all);

      if (all.length === 0) {
        setSessionId("");
        setLoading(false);
        return;
      }

      if (taskId) {
        try {
          const taskSessions = await collabApi.fetchTaskSessions(taskId);
          const liveId =
            taskSessions.live_session_id ||
            taskSessions.sessions?.[taskSessions.sessions.length - 1]?.id ||
            null;
          if (!cancelled && liveId && all.some((session) => session.id === liveId)) {
            setSessionId(liveId);
            setLoading(false);
            return;
          }
        } catch {
          /* fall through to the project match */
        }
      }
      if (cancelled) return;

      const matched = matchByProject(all, projectId, projectName);
      setSessionId(matched?.id ?? "");
      setLoading(false);
    };

    void resolve();
    return () => {
      cancelled = true;
    };
  }, [taskId, projectId, projectName]);

  const selectSession = useCallback((id: string) => setSessionId(id), []);

  return useMemo(
    () => ({ sessions, sessionId, selectSession, loading }),
    [sessions, sessionId, selectSession, loading],
  );
}
