"use client";

/**
 * TaskDetailPanel (§2.6) — the "everything" panel: six tabs over one
 * useTaskDetail fetch. Mounted by TaskDetailDrawer (board overlay) and by
 * /activity/[taskId] (full page) — same components, two mounts.
 *
 * Per-tab empty/loading/error states are independent (§2.8): one tab's
 * failure never blanks the others. The design Tabs primitive renders only the
 * active item's content, so the ChatSurface mounts lazily per visit.
 */

import { useCallback, useMemo } from "react";
import { Badge, EmptyState, ErrorState, Loading, Tabs } from "@/design";
import { ChatSurface } from "@/features/chats/ChatSurface";
import { EvidenceTab } from "@/features/team/EvidenceTab";
import { useTaskDetail } from "./useTaskDetail";
import { TaskOverview } from "./TaskOverview";
import { PlanView } from "./PlanView";
import { TaskFilesPanel } from "./TaskFilesPanel";
import { ApprovalsTab, RunsTab } from "./RunsTab";
import { LedgerTimeline } from "./LedgerTimeline";
import styles from "./board.module.css";

export function TaskDetailPanel({ taskId }: { taskId: string }) {
  const {
    task,
    sessions,
    longhaul,
    conversation,
    children,
    eta,
    ledger,
    ledgerLoading,
    loading,
    error,
    refresh,
  } = useTaskDetail(taskId);

  const steeringLive = useMemo(() => {
    const state = task?.work?.state ?? "";
    return ["queued", "planning", "starting", "running", "awaiting_approval", "resuming"].includes(
      state,
    );
  }, [task?.work?.state]);

  // When the dock grows a chat on a chatless card (§2.5), or the user
  // renames/archives/deletes a chat from the dock, refresh the task so the
  // panel's 💬 badge, title, and chat_origin stay in sync with the board.
  const handleChatCreated = useCallback(() => {
    void refresh();
  }, [refresh]);
  const handleChatMutated = useCallback(() => {
    void refresh();
  }, [refresh]);

  if (loading && !task) {
    return <Loading variant="skeleton" label="Loading the task" lines={5} />;
  }
  if (error && !task) {
    return <ErrorState title="Could not load the task" message={error} onRetry={() => void refresh()} />;
  }
  if (!task) {
    return <EmptyState title="Task not found" message="It may have been deleted." />;
  }

  return (
    <div className={styles.panelRoot}>
      <div className={styles.panelHeader}>
        <h3 className={styles.panelTitle}>{task.title}</h3>
        <div className={styles.panelBadges}>
          <Badge tone={task.status === "done" ? "completed" : "neutral"}>
            {task.status.replace(/_/g, " ")}
          </Badge>
          {task.lane ? <Badge tone="neutral">{task.lane}</Badge> : null}
          {task.chat_origin ? (
            <Badge tone="promote" title={task.chat_origin.title ?? "From chat"}>
              💬 chat
            </Badge>
          ) : null}
        </div>
      </div>

      <Tabs
        aria-label="Task detail tabs"
        defaultValue="overview"
        items={[
          {
            id: "overview",
            label: "Overview",
            content: (
              <TaskOverview
                task={task}
                sessions={sessions}
                longhaul={longhaul}
                eta={eta}
                onVerifyChanged={() => void refresh()}
              />
            ),
          },
          {
            id: "plan",
            label: "Plan",
            content: <PlanView task={task} longhaul={longhaul} />,
          },
          {
            id: "chat",
            label: "Chat",
            content: (
              <div className={styles.chatDock}>
                <ChatSurface
                  chatId={task.chat_origin?.chat_id ?? null}
                  variant="panel"
                  boardTaskId={task.id}
                  steeringLive={steeringLive}
                  onChatCreated={handleChatCreated}
                  onChatMutated={handleChatMutated}
                />
              </div>
            ),
          },
          {
            id: "files",
            label: "Files",
            content: <TaskFilesPanel taskId={task.id} />,
          },
          {
            id: "runs",
            label: "Runs",
            content: (
              <RunsTab
                longhaul={longhaul}
                conversation={conversation}
                subTasks={children}
              />
            ),
          },
          {
            id: "evidence",
            label: "Evidence",
            content: <EvidenceTab taskId={task.id} />,
          },
          {
            id: "approvals",
            label: "Approvals",
            content: <ApprovalsTab task={task} onDecided={() => void refresh()} />,
          },
          {
            id: "ledger",
            label: "Ledger",
            content: <LedgerTimeline events={ledger} loading={ledgerLoading} />,
          },
        ]}
      />
    </div>
  );
}
