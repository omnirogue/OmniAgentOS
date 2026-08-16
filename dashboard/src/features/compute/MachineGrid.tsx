"use client";

import { Badge, ErrorState, Loading } from "@/design";
import { formatBytes, formatCount } from "@/lib/format";
import type { ComputeEstateViewProps } from "./hooks";
import { deriveMachineCards, formatLoadRatio, lastSeenCaption, type MachineCardView } from "./tiles";
import styles from "./compute.module.css";

/**
 * Machine cards grid — every pool machine plus one card for the local box
 * this dashboard runs on. Each card: name, os badge, cores (+ perf cores
 * where present), doctrine-toned load ratio, free mem, in-flight/max slots,
 * last-seen freshness (a "stale" badge past 120s), and a small done_1h/6h
 * throughput caption. A `drain > 0` machine gets a "draining" badge. A card
 * with `available: false` (only the local card can be, today — UI-3 review
 * fix) renders as a neutral refusal instead: name + the backend's reason,
 * no metrics — never a live-looking machine whose numbers just happen to
 * all be null.
 *
 * Takes `data`/`status`/`error`/`refresh` as props (UI-2 review fix) —
 * the estate-polling hook lives ONLY at page.tsx now.
 */
export function MachineGrid({ data, status, error, refresh }: ComputeEstateViewProps) {
  if (status === "loading") {
    return (
      <div className={styles.machineGrid} aria-busy="true">
        <div className={styles.machineCard}>
          <Loading variant="skeleton" lines={4} label="Loading machines" />
        </div>
      </div>
    );
  }

  if (status === "error" || !data) {
    return <ErrorState title="Machines unavailable" message={error ?? "Could not load the compute estate"} onRetry={refresh} />;
  }

  const cards = deriveMachineCards(data);

  return (
    <div className={styles.machineGrid}>
      {cards.map((card) => (
        <MachineCard key={card.key} card={card} />
      ))}
    </div>
  );
}

function MachineCard({ card }: { card: MachineCardView }) {
  if (!card.available) {
    return (
      <div className={`${styles.machineCard} ${styles["machineCard--unavailable"]}`}>
        <div className={styles.machineHead}>
          <span className={styles.machineName}>{card.name}</span>
          {card.isLocal ? <Badge tone="neutral">local</Badge> : null}
        </div>
        <p className={styles.machineRefusal}>{card.reason ?? "unavailable"}</p>
      </div>
    );
  }

  const loadCls = card.loadTone !== "default" ? `${styles.loadValue} ${styles[`loadValue--${card.loadTone}`]}` : styles.loadValue;
  const coresText =
    card.cores == null
      ? "—"
      : card.perfCores != null
        ? `${formatCount(card.cores)} (${formatCount(card.perfCores)}p)`
        : formatCount(card.cores);
  const memText = card.memFreeGb == null ? "—" : formatBytes(card.memFreeGb * 1024 * 1024 * 1024);
  const slotsText =
    card.inFlight == null || card.maxConcurrent == null
      ? "—"
      : `${formatCount(card.inFlight)} / ${formatCount(card.maxConcurrent)}`;
  const throughputText =
    card.done1h == null && card.done6h == null
      ? "—"
      : `${formatCount(card.done1h)} done (1h) · ${formatCount(card.done6h)} done (6h)`;

  return (
    <div className={styles.machineCard}>
      <div className={styles.machineHead}>
        <span className={styles.machineName}>{card.name}</span>
        {card.isLocal ? <Badge tone="neutral">local</Badge> : null}
        {card.os ? <Badge tone="neutral">{card.os}</Badge> : null}
        {card.draining ? <Badge tone="warn">draining</Badge> : null}
        {card.stale ? <Badge tone="warn">stale</Badge> : null}
      </div>

      <div className={styles.machineLoadRow}>
        <span className={loadCls}>{formatLoadRatio(card.loadRatio)}</span>
        <span className={styles.machineLoadLabel}>load</span>
      </div>

      <dl className={styles.machineStats}>
        <div>
          <dt>Cores</dt>
          <dd>{coresText}</dd>
        </div>
        <div>
          <dt>Free mem</dt>
          <dd>{memText}</dd>
        </div>
        <div>
          <dt>Slots</dt>
          <dd>{slotsText}</dd>
        </div>
        <div>
          <dt>Last seen</dt>
          <dd>{lastSeenCaption(card)}</dd>
        </div>
      </dl>

      <p className={styles.machineThroughput}>{throughputText}</p>
    </div>
  );
}
