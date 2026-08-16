"use client";

import { useState } from "react";
import { Badge, Button, Card, StatusDot, useToast } from "../../../design";
import { alertStateTone, formatDateTime, severityBadgeTone, severityDotState, severityLabel, sortAlertsBySeverity } from "../format";
import type { Alert } from "../types";
import styles from "../steward.module.css";

export type AlertsListProps = {
  alerts: Alert[];
  onAck: (id: number, by?: string) => Promise<Alert>;
};

function AlertRow({ alert, onAck }: { alert: Alert; onAck: AlertsListProps["onAck"] }) {
  const { push } = useToast();
  const [acking, setAcking] = useState(false);

  const ack = async () => {
    setAcking(true);
    try {
      await onAck(alert.id, "operator");
    } catch (reason) {
      push({ title: "Ack failed", message: reason instanceof Error ? reason.message : "Could not acknowledge this alert.", tone: "error" });
    } finally {
      setAcking(false);
    }
  };

  return (
    <Card padding="none" className={styles.alertRow}>
      <StatusDot state={severityDotState(alert.severity)} label={`${alert.severity} severity`} />
      <div className={styles.alertText}>
        <div className={styles.alertHead}>
          <p className={styles.alertTitle}>{alert.title}</p>
          {/* DES-003: severity is ALWAYS a text label (never color-only), so a
              color-blind operator can tell critical apart from noise at a glance. */}
          <Badge tone={severityBadgeTone(alert.severity)}>{severityLabel(alert.severity)}</Badge>
        </div>
        <p className={styles.alertBody}>{alert.body}</p>
        <div className={styles.alertMeta}>
          <span>{alert.rule}</span>
          <span>{formatDateTime(alert.created_at)}</span>
          <Badge tone={alertStateTone(alert.state)}>{alert.state}</Badge>
          {/* RR-PROD-002: a high/critical alert that could not be pushed (no
              channel configured) is flagged here, never silently identical to a
              delivered one — so the operator who wasn't notified can still see it. */}
          {typeof alert.evidence?.delivery_warning === "string" ? (
            <Badge tone="danger">⚠ not pushed</Badge>
          ) : null}
        </div>
      </div>
      {alert.state === "open" ? (
        <Button variant="secondary" size="sm" onClick={() => void ack()} disabled={acking}>
          {acking ? "Acking…" : "Ack"}
        </Button>
      ) : (
        <span className={styles.faint}>{formatDateTime(alert.acked_at)}</span>
      )}
    </Card>
  );
}

export function AlertsList({ alerts, onAck }: AlertsListProps) {
  // DES-003: severity-first, not newest-first — a critical alert must never
  // scroll behind a screenful of low/medium noise.
  const sorted = sortAlertsBySeverity(alerts);
  return (
    <div className={styles.stack}>
      {sorted.map((alert) => (
        <AlertRow key={alert.id} alert={alert} onAck={onAck} />
      ))}
    </div>
  );
}
