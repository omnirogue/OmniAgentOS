"use client";

import { ErrorState } from "@/design/ErrorState";
import styles from "../errorBoundary.module.css";

/**
 * Error boundary for the Approvals page.
 * Catches render errors in the approval queue and workflow.
 * Prevents data issues from blocking the operator's ability to review and approve tasks.
 */
export default function ApprovalsError({
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
          title="Approvals view failed to load"
          message="Unable to display the approval queue. Please try again to see pending approvals."
          onRetry={reset}
          retryLabel="Reload approvals"
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
