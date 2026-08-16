"use client";

/**
 * TaskDetailDrawer (§2.5/§2.6) — the right overlay hosting TaskDetailPanel on
 * the board. Deep-linkable via /board?task=<id>; Esc closes. 22rem default,
 * wider than the chats workspace drawer because the tabs carry data.
 */

import { useEffect } from "react";
import { Button, Icon } from "@/design";
import { TaskDetailPanel } from "./TaskDetailPanel";
import styles from "./board.module.css";

export function TaskDetailDrawer({
  taskId,
  onClose,
}: {
  taskId: string;
  onClose: () => void;
}) {
  // Esc closes the drawer (§2.7 Esc chain: drawer/dialog last — a dialog open
  // on top of the drawer, e.g. the composer's routing dialog, owns Esc first)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (document.querySelector('[role="dialog"]')) return;
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handler, true);
    return () => document.removeEventListener("keydown", handler, true);
  }, [onClose]);

  return (
    <>
      <button className={styles.drawerScrim} aria-label="Close task details" onClick={onClose} />
      <aside className={styles.drawer} aria-label="Task details">
        <div className={styles.drawerHeader}>
          <Button variant="ghost" size="sm" onClick={onClose} title="Close (Esc)">
            <Icon name="close" size={14} />
          </Button>
        </div>
        <div className={styles.drawerBody}>
          <TaskDetailPanel taskId={taskId} />
        </div>
      </aside>
    </>
  );
}
