"use client";

/**
 * SpawnCard (chat-v2 §2.3.3) — fan-out result with live child chips
 * deep-linking to /board?task=<id>.
 */

import Link from "next/link";
import { Badge } from "@/design";
import styles from "./chats.module.css";

export function SpawnCard({ taskIds }: { taskIds: string[] }) {
  if (!taskIds.length) return null;
  return (
    <div className={styles.spawnCard}>
      <div className={styles.spawnCardHead}>
        <Badge tone="promote">Fan-out</Badge>
        <span>{taskIds.length} sub-agent{taskIds.length === 1 ? "" : "s"} dispatched</span>
      </div>
      <div className={styles.spawnCardChips}>
        {taskIds.map((taskId) => (
          <Link
            key={taskId}
            href={`/board?task=${encodeURIComponent(taskId)}`}
            className={styles.spawnChip}
            title={`Open ${taskId} on the board`}
          >
            {taskId}
          </Link>
        ))}
      </div>
    </div>
  );
}
