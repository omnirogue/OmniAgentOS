"use client";

import { useEffect, useState } from "react";
import { Badge, Card, EmptyState, Loading, StatusDot } from "@/design";
import { statusDotState, statusLabel, statusTone } from "@/features/accounts/format";
import type { ProviderHealthRow } from "./types";
import styles from "./swarm.module.css";

/** Live "cooling 4m 12s" countdown to `cooldown_until`, ticking each second.
 * Renders nothing once the cooldown has lapsed. */
function CooldownCountdown({ until }: { until: string }) {
  const target = Date.parse(until);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!Number.isFinite(target)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [target]);
  if (!Number.isFinite(target)) return null;
  const remaining = Math.max(0, Math.round((target - now) / 1000));
  if (remaining <= 0) return null;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return (
    <span className={styles.cooldown}>
      cooling {minutes > 0 ? `${minutes}m ` : ""}
      {seconds}s
    </span>
  );
}

/**
 * Provider health cards from `GET /api/swarm/providers` (C4), normalized to
 * `ProviderHealthRow` (the accounts-endpoint fallback is normalized to the same
 * shape upstream in `useProviderHealth`, so this renders one uniform row type).
 * Durable fields (`active_sessions` / `max_inflight`) render as an inflight chip
 * when the primary route supplied them; the fallback leaves them null and the
 * chip is simply omitted.
 */
export function ProviderHealthPanel({
  rows,
  loading,
  stale,
}: {
  rows: ProviderHealthRow[];
  loading: boolean;
  stale: boolean;
}) {
  if (loading && rows.length === 0) {
    return <Loading variant="skeleton" label="Loading provider health" lines={3} />;
  }

  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState message="No provider accounts registered. Add one on the Accounts page and it appears here with its live status, cooldown and inflight count." />
      </Card>
    );
  }

  return (
    <>
      {stale ? (
        <div className={styles.banner} role="status">
          Reconnecting — provider health may be stale
        </div>
      ) : null}
      <div className={styles.providerGrid}>
        {rows.map((row) => {
          const showInflight = row.active_sessions != null && row.max_inflight != null;
          const key = row.account_id ?? `${row.provider}:implicit`;
          return (
            <Card key={key} padding="none" className={styles.providerCard}>
              <div className={styles.providerHead}>
                <span className={styles.providerName}>
                  <StatusDot
                    state={statusDotState(row.status)}
                    label={statusLabel(row.status)}
                  />
                  {row.display_name}
                </span>
                <Badge tone={statusTone(row.status)}>{statusLabel(row.status)}</Badge>
              </div>
              <div className={styles.chips}>
                <Badge tone="neutral">{row.provider}</Badge>
                {showInflight ? (
                  <Badge tone="neutral">
                    {row.active_sessions} / {row.max_inflight} inflight
                  </Badge>
                ) : null}
                {row.account_id == null ? <Badge tone="neutral">no account</Badge> : null}
              </div>
              {row.status === "rate_limited" && row.cooldown_until ? (
                <CooldownCountdown until={row.cooldown_until} />
              ) : null}
              {row.status_detail ? (
                <span className={styles.muted}>{row.status_detail}</span>
              ) : null}
            </Card>
          );
        })}
      </div>
    </>
  );
}
