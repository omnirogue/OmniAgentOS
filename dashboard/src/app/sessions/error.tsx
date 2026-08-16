"use client";

import { ErrorState } from "@/design/ErrorState";
import styles from "../errorBoundary.module.css";

/**
 * Error boundary for the Sessions page.
 * Catches render errors in the sessions view (session list, live session state).
 * Prevents a single bad session from taking down the entire sessions interface.
 */
export default function SessionsError({
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
          title="Sessions view failed to load"
          message="Unable to display your sessions. The server may be temporarily unavailable or there may be an issue with the session data."
          onRetry={reset}
          retryLabel="Reload sessions"
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
