"use client";

/**
 * Workspace drawer content (was WorkspaceTabs).
 *
 * Two changes from the tabbed version:
 *   • ONE session fetch. The drawer resolves the followed session with
 *     useWorkspaceSession and hands it to SessionFollow, which used to run the
 *     identical `GET /api/sessions` + task-session resolution a second time.
 *   • Live Terminal is no longer a co-equal tab. Session activity is what the
 *     drawer is FOR; the raw terminal is a disclosure below it (and a link to
 *     the full session page), so the drawer stops implying two equal surfaces.
 */

import { useState } from "react";
import Link from "next/link";
import { EmptyState, Icon } from "@/design";
import { SessionFollow } from "./SessionFollow";
import { TerminalView } from "./TerminalView";
import { useWorkspaceSession } from "./useWorkspaceSession";
import styles from "./chatShell.module.css";

interface WorkspacePanelProps {
  taskId?: string | null;
  projectId?: string | null;
  projectName?: string | null;
}

export function WorkspacePanel({ taskId, projectId, projectName }: WorkspacePanelProps) {
  const { sessions, sessionId, selectSession, loading } = useWorkspaceSession({
    taskId,
    projectId,
    projectName,
  });
  const [terminalOpen, setTerminalOpen] = useState(false);

  return (
    <>
      <div className={styles.sessionSlot}>
        {!loading && !sessionId ? (
          <EmptyState
            title="No session linked to this chat"
            message="Promote the chat to the board (or dispatch a turn) and the session it runs in shows up here."
          />
        ) : (
          <SessionFollow
            sessions={sessions}
            sessionId={sessionId}
            onSelectSession={selectSession}
          />
        )}
      </div>

      <section className={styles.terminalSection}>
        <button
          type="button"
          className={styles.terminalToggle}
          aria-expanded={terminalOpen}
          onClick={() => setTerminalOpen((prev) => !prev)}
        >
          <Icon name={terminalOpen ? "chevronDown" : "chevronRight"} size={12} />
          Live terminal
        </button>
        {terminalOpen ? (
          sessionId ? (
            <div className={styles.terminalBody}>
              <TerminalView sessionId={sessionId} />
              <p className={styles.terminalHint}>
                {/* /sessions is the list surface — there is no per-session route. */}
                <Link className={styles.terminalLink} href="/sessions">
                  Open Sessions →
                </Link>
              </p>
            </div>
          ) : (
            <p className={styles.terminalHint}>
              No active session — the terminal streams here once one starts.
            </p>
          )
        ) : null}
      </section>
    </>
  );
}
