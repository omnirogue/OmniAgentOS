"use client";

/**
 * Detail dialog for a single integration.
 *
 * Surfaces: status, instances (if multi), the "what this unlocks" one-liner,
 * and a masked checklist of which key families are present (family labels
 * only — no env-var names and no secret values).
 *
 * Design primitive-only: <Dialog>+<Badge>+<Button> from @/design. Zero inline
 * styles; layout via connections.module.css.
 */

import { Badge, Button, Dialog } from "@/design";
import { BrandIcon } from "./brandIcons";
import { statusBadgeTone, statusSummaryLabel } from "./logic";
import type { ConnectionIntegration } from "./types";
import styles from "./connections.module.css";

interface DetailDialogProps {
  integration: ConnectionIntegration | null;
  onClose: () => void;
}

export function DetailDialog({ integration, onClose }: DetailDialogProps) {
  const open = integration !== null;

  if (!integration) return null;

  const tone = statusBadgeTone(integration.status);
  const summary = statusSummaryLabel(integration);
  // Captured so the type stays narrowed inside the onClick closure below.
  const docsUrl = integration.docs_url;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={
        <div className={styles.detailHeader}>
          <div className={styles.logoWrap}>
            <BrandIcon id={integration.logo} size={28} />
          </div>
          <h2 className={styles.detailName}>{integration.name}</h2>
        </div>
      }
      footer={
        <div className={styles.docsFooter}>
          {docsUrl ? (
            <Button
              variant="ghost"
              onClick={() => {
                window.open(docsUrl, "_blank", "noopener,noreferrer");
              }}
            >
              Open docs ↗
            </Button>
          ) : null}
          <Button variant="primary" onClick={onClose}>
            Close
          </Button>
        </div>
      }
    >
      <Badge tone={tone}>{summary}</Badge>

      {integration.unlocks ? (
        <p className={styles.detailUnlocks}>
          <span className={styles.detailSectionTitle}>What this unlocks</span>
          <br />
          {integration.unlocks}
        </p>
      ) : null}

      {integration.instances.length > 0 ? (
        <section className={styles.detailSection} aria-label="Sub-accounts">
          <p className={styles.detailSectionTitle}>Instances</p>
          {integration.instances.map((inst) => {
            const instTone =
              inst.status === "connected"
                ? "ok"
                : inst.status === "error"
                  ? "warn"
                  : "neutral";
            return (
              <div
                key={`${integration.id}-${inst.label}`}
                className={styles.detailRow}
              >
                <span className={styles.detailRowLabel}>{inst.label}</span>
                <Badge tone={instTone}>
                  {inst.status === "connected"
                    ? "Connected"
                    : inst.status === "not_configured"
                      ? "Not configured"
                      : inst.status === "configured"
                        ? "Configured"
                        : "Error"}
                </Badge>
              </div>
            );
          })}
        </section>
      ) : (
        <section className={styles.detailSection} aria-label="Connection details">
          <p className={styles.detailSectionTitle}>Configuration</p>
          <div className={styles.detailRow}>
            <span className={styles.detailRowLabel}>Status</span>
            <Badge tone={tone}>{summary}</Badge>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailRowLabel}>Detail</span>
            <span className={styles.detailRowValue}>{integration.detail}</span>
          </div>
        </section>
      )}
    </Dialog>
  );
}
