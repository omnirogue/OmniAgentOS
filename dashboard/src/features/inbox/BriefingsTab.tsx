"use client";

import { useState } from "react";
import { Badge, Button, Card, EmptyState, ErrorState, Loading, Section } from "../../design";
import type { Briefing } from "../steward";
import stewardStyles from "../steward/steward.module.css";
import styles from "./inbox.module.css";

function BriefingRow({ briefing, onAck }: { briefing: Briefing; onAck: (id: string) => Promise<unknown> }) {
  const [acking, setAcking] = useState(false);
  const acked = Boolean(briefing.acked_at);

  const ack = async () => {
    if (acked) return;
    setAcking(true);
    try {
      await onAck(briefing.id);
    } finally {
      setAcking(false);
    }
  };

  return (
    <div className={stewardStyles.historyRow}>
      <div>
        <strong>{briefing.summary.subject || briefing.briefing_date}</strong>
        <p className={`${stewardStyles.muted} ${styles.briefingHeadline}`}>{briefing.summary.headline}</p>
      </div>
      {acked ? (
        <Badge tone="ok">Acked</Badge>
      ) : (
        <Button variant="secondary" size="sm" onClick={() => void ack()} disabled={acking}>
          {acking ? "Acking…" : "Ack"}
        </Button>
      )}
    </div>
  );
}

/** Reads the SAME `GET /api/briefings` (newest-first, per steward/store.py's
 * `ORDER BY briefing_date DESC, created_at DESC`) + `POST /api/briefings/{id}/ack`
 * the standalone `/briefing` page uses — every row gets its own Ack action here
 * (the standalone page only offers it on the single latest briefing). */
export function BriefingsTab({
  history,
  loading,
  error,
  refresh,
  ack,
}: {
  history: Briefing[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  ack: (id: string) => Promise<Briefing>;
}) {
  return (
    <div>
      {loading ? (
        <Card>
          <Loading label="Loading briefings…" />
        </Card>
      ) : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && history.length === 0 ? (
        <Card>
          <EmptyState title="No briefings yet" message="No morning briefings have been composed yet." />
        </Card>
      ) : null}
      {!loading && !error && history.length > 0 ? (
        <Section title="Briefings" description="Newest first.">
          <div className={stewardStyles.historyList}>
            {history.map((briefing) => (
              <BriefingRow key={briefing.id} briefing={briefing} onAck={ack} />
            ))}
          </div>
        </Section>
      ) : null}
    </div>
  );
}
