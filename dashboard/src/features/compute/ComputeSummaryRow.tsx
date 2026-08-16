"use client";

import { ErrorState, Loading } from "@/design";
import type { ComputeEstateViewProps } from "./hooks";
import { deriveSummaryTiles, type SummaryTile } from "./tiles";
import styles from "./compute.module.css";

const SKELETON_KEYS = ["free-slots", "queue-depth", "runners", "local-load"] as const;

/**
 * Four summary tiles — free slots, queue depth, estate runners, and the
 * local box's own load — each a headline number + one-line caption. No
 * sparklines (the pool exposes no time series; this page is current-state
 * only, brief §4).
 *
 * Takes `data`/`status`/`error`/`refresh` as props (UI-2 review fix) —
 * the estate-polling hook lives ONLY at page.tsx now and is shared across
 * the three display components, rather than each one polling the same
 * envelope independently.
 */
export function ComputeSummaryRow({ data, status, error, refresh }: ComputeEstateViewProps) {
  if (status === "loading") {
    return (
      <div className={styles.statGrid} aria-busy="true">
        {SKELETON_KEYS.map((k) => (
          <div key={k} className={styles.statTile}>
            <Loading variant="skeleton" lines={2} />
          </div>
        ))}
      </div>
    );
  }

  if (status === "error" || !data) {
    return (
      <div className={styles.statGrid}>
        <div className={styles.statTile}>
          <ErrorState
            title="Estate compute unavailable"
            message={error ?? "Could not load the compute estate"}
            onRetry={refresh}
          />
        </div>
      </div>
    );
  }

  const tiles = deriveSummaryTiles(data);

  return (
    <div className={styles.statGrid}>
      {tiles.map((tile) => (
        <StatTile key={tile.key} tile={tile} />
      ))}
    </div>
  );
}

function StatTile({ tile }: { tile: SummaryTile }) {
  const valueCls = tile.tone !== "default" ? `${styles.statValue} ${styles[`statValue--${tile.tone}`]}` : styles.statValue;
  return (
    <div className={`${styles.statTile} ${tile.available ? "" : styles["statTile--unavailable"]}`}>
      <span className={styles.statTitle}>{tile.title}</span>
      <span className={valueCls}>{tile.primary}</span>
      <p className={styles.statCaption}>{tile.caption}</p>
    </div>
  );
}
