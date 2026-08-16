"use client";

/**
 * A single integration tile in the connections grid.
 *
 * Renders logo (via brandIcons), name, status Badge, instance chips for
 * multi-instance services, and a docs link. Clicking or pressing Enter
 * emits onOpen() so the page can show the DetailDialog.
 *
 * Stripe-like restraint: tile chrome stays neutral; brand colors only on the
 * SVG logo glyph itself.
 */

import type { KeyboardEvent } from "react";
import { Badge, Card } from "@/design";
import { BrandIcon } from "./brandIcons";
import { statusBadgeTone, statusSummaryLabel } from "./logic";
import type { ConnectionIntegration } from "./types";
import styles from "./connections.module.css";

interface TileProps {
  integration: ConnectionIntegration;
  onOpen: (integration: ConnectionIntegration) => void;
}

export function Tile({ integration, onOpen }: TileProps) {
  const tone = statusBadgeTone(integration.status);
  const summary = statusSummaryLabel(integration);

  const handleKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen(integration);
    }
  };

  const handleDocsClick = (
    e: React.MouseEvent<HTMLAnchorElement>,
  ) => {
    e.stopPropagation();
  };

  return (
    <Card
      padding="md"
      className={styles.tile}
      role="button"
      tabIndex={0}
      aria-label={`${integration.name} — ${summary}`}
      onClick={() => onOpen(integration)}
      onKeyDown={handleKey}
    >
      <div className={styles.tileHead}>
        <div className={styles.logoWrap}>
          <BrandIcon id={integration.logo} size={24} />
        </div>
        <div className={styles.tileCopy}>
          <p className={styles.tileName}>{integration.name}</p>
          <p className={styles.tileMeta}>{integration.detail}</p>
        </div>
      </div>

      {integration.instances.length > 0 ? (
        <ul className={styles.instancesRow} aria-label="Sub-accounts">
          {integration.instances.map((inst) => {
            const chipTone =
              inst.status === "connected" ? styles.instanceChipConnected : "";
            return (
              <li
                key={`${integration.id}-${inst.label}`}
                className={`${styles.instanceChip} ${chipTone}`}
                title={`${inst.label}: ${inst.status}`}
              >
                {inst.label}
              </li>
            );
          })}
        </ul>
      ) : null}

      <div className={styles.tileFoot}>
        <Badge tone={tone}>{summary}</Badge>
        <div className={styles.tileFootRight}>
          {integration.docs_url ? (
            <a
              href={integration.docs_url}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.docsLink}
              onClick={handleDocsClick}
              aria-label={`${integration.name} docs (opens in a new tab)`}
            >
              Docs ↗
            </a>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
