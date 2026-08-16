"use client";

import { cx, ErrorState, Section } from "@/design";
import type { RecyclerStatus, Result } from "../types";
import styles from "../status.module.css";

const RECYCLED_ACTIONS = new Set(["RECYCLED", "FORCE-RECYCLED"]);

function isRaw(data: RecyclerStatus): data is { raw: string } {
  return typeof (data as { raw?: unknown }).raw === "string" && !("action" in data);
}

/**
 * One line summarizing the hang-recycler's last tick. `action` is styled
 * red when it recycled/force-recycled a session (a real intervention worth
 * noticing), and neutral for `none`. A tick that failed to parse as JSON
 * falls back to the raw log line rather than hiding it.
 */
export function RecyclerSection({ recycler }: { recycler: Result<RecyclerStatus> }) {
  return (
    <Section eyebrow="Hang recycler" title="Recycler">
      {!recycler.ok ? (
        <ErrorState message={`Could not read hang-recycler.log: ${recycler.error}`} />
      ) : isRaw(recycler.data) ? (
        <p className={cx(styles.recyclerLine, styles.recyclerRaw)}>{recycler.data.raw || "(empty log)"}</p>
      ) : (
        <RecyclerTickLine entry={recycler.data} />
      )}
    </Section>
  );
}

function RecyclerTickLine({ entry }: { entry: Record<string, unknown> }) {
  const action = typeof entry.action === "string" ? entry.action : "unknown";
  const reason = typeof entry.reason === "string" ? entry.reason : null;
  const danger = RECYCLED_ACTIONS.has(action);

  return (
    <p className={styles.recyclerLine}>
      <span className={cx(styles.recyclerAction, danger ? styles.recyclerActionDanger : styles.recyclerActionOk)}>
        {action}
      </span>
      {reason ? <span className={styles.muted}>— {reason}</span> : null}
    </p>
  );
}
