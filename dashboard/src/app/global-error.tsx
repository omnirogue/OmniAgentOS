"use client";

import { useEffect } from "react";
import { ErrorState } from "@/design/ErrorState";
import styles from "./errorBoundary.module.css";

/**
 * Global error boundary for the entire application.
 * Catches unhandled errors that bubble to the root level.
 * Provides recovery and error reporting without leaking stack traces.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Log the error to an external service (optional in production).
    // For now, just log to console in development.
    if (process.env.NODE_ENV === "development") {
      console.error("Global error:", error);
    }
  }, [error]);

  // NOTE: global-error.tsx REPLACES the root layout when it fires, so it must
  // render its own <html>/<body> — layout.tsx is not in the tree at that point.
  // Do not remove this wrapper to make a test render more conveniently; see
  // __tests__/error-boundary.test.tsx for the harness that asserts it.
  return (
    <html>
      <body>
        <div className={styles.globalContainer}>
          <div className={styles.wrapper}>
            <ErrorState
              title="Something went wrong"
              message="An unexpected error occurred. Please try again or contact support if the problem persists."
              onRetry={reset}
              retryLabel="Try again"
            />
            {error.digest && (
              <p className={styles.digestText}>
                Error ID: {error.digest}
              </p>
            )}
          </div>
        </div>
      </body>
    </html>
  );
}
