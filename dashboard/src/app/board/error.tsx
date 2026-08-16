"use client";

import { ErrorState } from "@/design/ErrorState";
import styles from "../errorBoundary.module.css";

/**
 * Error boundary for the Board (Kanban) page.
 * Catches render errors in the kanban view, filters, and related panels.
 * Prevents data shape mismatches from blanking the entire board surface.
 * This is a high-value operational view used frequently in the workflow.
 */
export default function BoardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className={styles.routeContainer}>
      <div className={styles.wrapper}>
        <ErrorState
          title="Board view failed to load"
          message="Unable to display the kanban board. There may be an issue with the task data or the live updates."
          onRetry={reset}
          retryLabel="Reload board"
        />
        {error.digest && (
          <p className={styles.digestText}>
            Error ID: {error.digest}
          </p>
        )}
      </div>
    </div>
  );
}
