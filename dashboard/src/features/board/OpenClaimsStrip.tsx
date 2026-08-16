"use client";

/**
 * OpenClaimsStrip (board header) — every OPEN session-ledger claim across
 * the estate, one chip per claim: `project/surface — session`, with a stale
 * marker when the CLI reports `stale: true`. Replaces the SESSION-BOUNDARY
 * doc as the visible who-holds-what (session-ledger integration brief,
 * 2026-08-04, §3).
 *
 * ONE poll of GET /api/ledger/claims per 10s TOTAL for the whole board —
 * never per card — mirroring InsightsStrip's `startVisibilityPoll` pattern
 * (paused while the tab is hidden).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Pill } from "@/design";
import type { LedgerClaim } from "@/features/collab/types";
import { collabApi } from "@/features/collab/client";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import styles from "./board.module.css";

const POLL_MS = 10_000;

type ClaimsState =
  | { state: "loading" }
  | { state: "ready"; claims: LedgerClaim[] }
  | { state: "unavailable" };

function claimLabel(claim: LedgerClaim): string {
  return claim.surface ? `${claim.project}/${claim.surface}` : claim.project;
}

export function OpenClaimsStrip() {
  const [claims, setClaims] = useState<ClaimsState>({ state: "loading" });
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    try {
      const rows = await collabApi.fetchLedgerClaims();
      if (request !== generation.current) return;
      setClaims({ state: "ready", claims: rows });
    } catch {
      // Any failure (network, 401 while the proxy token is unavailable, a
      // 503 from the CLI relay) degrades the same way -- this strip is a
      // convenience readout, never load-bearing, so it just goes quiet
      // rather than showing an error banner on the board.
      if (request !== generation.current) return;
      setClaims({ state: "unavailable" });
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS);
  }, [refresh]);

  if (claims.state === "loading") return null;
  if (claims.state === "unavailable") return null;
  if (claims.claims.length === 0) return null;

  return (
    <section className={styles.claimsStrip} aria-label="Open surface claims">
      {claims.claims.map((claim) => (
        <Pill
          key={claim.id}
          tone={claim.stale ? "warn" : "neutral"}
          title={claim.stale ? "Past its lease — may be stale" : undefined}
        >
          {claimLabel(claim)} — {claim.session}
          {claim.stale ? " · stale" : ""}
        </Pill>
      ))}
    </section>
  );
}
