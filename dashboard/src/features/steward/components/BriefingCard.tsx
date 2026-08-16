"use client";

import { useState } from "react";
import { Badge, Button, Card, Icon } from "../../../design";
import { voiceAudioUrl } from "../api";
import { formatDateTime } from "../format";
import type { Briefing } from "../types";
import styles from "../steward.module.css";

export type BriefingCardProps = {
  briefing: Briefing;
  onAck?: (id: string) => Promise<unknown>;
};

export function BriefingCard({ briefing, onAck }: BriefingCardProps) {
  const [acking, setAcking] = useState(false);
  const acked = Boolean(briefing.acked_at);

  const ack = async () => {
    if (!onAck || acked) return;
    setAcking(true);
    try {
      await onAck(briefing.id);
    } finally {
      setAcking(false);
    }
  };

  return (
    <Card>
      <div className={styles.briefingHead}>
        <div>
          <h2 style={{ margin: "0 0 var(--space-1)", fontSize: "var(--text-h2)" }}>{briefing.summary.subject}</h2>
          <p className={styles.muted} style={{ margin: 0 }}>{briefing.summary.headline}</p>
        </div>
        <div className={styles.row}>
          <Badge tone={briefing.summary.composed_by === "llm" ? "promote" : "neutral"}>{briefing.summary.composed_by}</Badge>
          {acked ? (
            <Badge tone="ok">Acked {formatDateTime(briefing.acked_at)}</Badge>
          ) : onAck ? (
            <Button variant="secondary" size="sm" onClick={() => void ack()} disabled={acking}>
              {acking ? "Acking…" : "Ack"}
            </Button>
          ) : null}
        </div>
      </div>
      <div className={styles.briefingMeta}>
        <span>{briefing.briefing_date}</span>
        {briefing.vault_path ? (
          <span className={styles.faint} style={{ fontFamily: "var(--font-mono)" }}>{briefing.vault_path}</span>
        ) : null}
      </div>

      {briefing.voice_artifact_id ? (
        <div className={styles.audioWrap}>
          <audio controls src={voiceAudioUrl(briefing.voice_artifact_id)}>
            Your browser does not support inline audio.
          </audio>
        </div>
      ) : null}

      <div className={styles.briefingSections}>
        {briefing.summary.sections.map((section) => (
          <div className={styles.briefingSection} key={section.title}>
            <h3>{section.title}</h3>
            <p className={styles.briefingSectionBody}>{section.body}</p>
          </div>
        ))}
      </div>

      {briefing.deliveries.length ? (
        <div className={styles.deliveryList}>
          {briefing.deliveries.map((delivery, i) => (
            <div className={styles.deliveryRow} key={`${delivery.channel}-${i}`}>
              <Icon name={delivery.ok ? "checkCircle" : "alertTriangle"} size={14} />
              <strong>{delivery.channel}</strong>
              <span>{delivery.detail || (delivery.ok ? "delivered" : "failed")}</span>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
