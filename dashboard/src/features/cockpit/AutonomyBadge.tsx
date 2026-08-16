"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Icon, StatusDot } from "@/design";
import { api } from "@/lib/api";
import type { PauseState } from "@/lib/contracts";
import { useEvents } from "@/lib/useEvents";
import { useAlertCount } from "@/features/steward/hooks";
import styles from "./cockpit.module.css";

/**
 * Read-only autonomy indicator for the cockpit. Shows that the system runs itself
 * (Full-Auto) — or that it's paused — and links to the escalations queue for the
 * rare done/blocked/limit pings. Control (pause/resume) stays in the top bar's
 * PauseControl; this is just the calm, always-visible state line.
 */
export function AutonomyBadge() {
  const [paused, setPaused] = useState(false);
  const [reason, setReason] = useState("");
  const { lastEvent } = useEvents(["pause.changed"]);
  const { count: escalations } = useAlertCount();

  const apply = useCallback((state: PauseState) => {
    setPaused(!!state.paused);
    setReason(state.reason ?? "");
  }, []);

  const refresh = useCallback(async () => {
    try {
      apply(await api.pause());
    } catch {
      // Leave the last-known state; the top bar's PauseControl surfaces real errors.
    }
  }, [apply]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const payload = lastEvent?.payload ?? {};
    if (lastEvent?.type === "pause.changed" && typeof payload.paused === "boolean") {
      setPaused(payload.paused);
      setReason(typeof payload.reason === "string" ? payload.reason : "");
    }
  }, [lastEvent]);

  return (
    <div className={styles.autonomy} role="status" aria-live="polite">
      <span className={styles.autonomyState}>
        <StatusDot state={paused ? "paused" : "ok"} />
        {paused ? "Paused" : "Full-Auto"}
      </span>
      <span className={styles.autonomyNote}>
        {paused
          ? reason || "Agents are on hold — resume from the top bar."
          : "Agents act on their own."}
      </span>
      <Link href="/alerts" className={styles.autonomyLink}>
        Escalations
        {escalations > 0 ? (
          <span className={styles.escalationCount} aria-label={`${escalations} open`}>
            {escalations > 99 ? "99+" : escalations}
          </span>
        ) : (
          <Icon name="chevronRight" size={13} />
        )}
      </Link>
    </div>
  );
}
