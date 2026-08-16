"use client";

import { useState } from "react";
import { Card, EmptyState, ErrorState, Loading, Page, PageHeader, Select } from "@/design";
import { AlertsList, useAlerts } from "@/features/steward";

const STATE_OPTIONS = [
  { value: "", label: "All alerts" },
  { value: "open", label: "Open" },
  { value: "acked", label: "Acked" },
];

export default function AlertsPage() {
  const [state, setState] = useState("open");
  const { alerts, loading, error, refresh, ack } = useAlerts(state || undefined);

  return (
    <Page>
      <PageHeader
        eyebrow="Steward"
        title="Alerts"
        lead="Cooldown-aware alerts raised by the Steward's monitors — ROAS floor, payment failure bursts, and urgent messages."
        actions={<Select aria-label="Filter by state" value={state} onChange={setState} options={STATE_OPTIONS} />}
      />

      {loading ? (
        <Card>
          <Loading variant="skeleton" label="Loading alerts…" lines={4} />
        </Card>
      ) : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && alerts.length === 0 ? (
        <Card>
          <EmptyState title="No alerts" message="Nothing has tripped a monitor yet." />
        </Card>
      ) : null}
      {!loading && !error && alerts.length > 0 ? <AlertsList alerts={alerts} onAck={ack} /> : null}
    </Page>
  );
}
