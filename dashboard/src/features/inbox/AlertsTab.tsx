"use client";

import { Card, EmptyState, ErrorState, Loading } from "../../design";
import { AlertsList } from "../steward";
import type { Alert } from "../steward";

/** Reads the SAME `/api/alerts` (open-scoped) + `/api/alerts/{id}/ack` route the
 * standalone `/alerts` page uses, via the shared `AlertsList` component — no new
 * endpoint, no new UI for the ack action. */
export function AlertsTab({
  alerts,
  loading,
  error,
  refresh,
  ack,
}: {
  alerts: Alert[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  ack: (id: number, by?: string) => Promise<Alert>;
}) {
  return (
    <div>
      {loading ? (
        <Card>
          <Loading variant="skeleton" label="Loading alerts…" lines={4} />
        </Card>
      ) : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && alerts.length === 0 ? (
        <Card>
          <EmptyState title="No open alerts" message="Nothing has tripped a monitor." />
        </Card>
      ) : null}
      {!loading && !error && alerts.length > 0 ? <AlertsList alerts={alerts} onAck={ack} /> : null}
    </div>
  );
}
