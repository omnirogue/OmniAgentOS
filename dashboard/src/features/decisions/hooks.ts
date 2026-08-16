"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { decideDecision, DecisionApiError, fetchDecisions } from "./api";
import type { DecideBody, Decision, DecisionGroups } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof DecisionApiError || reason instanceof Error ? reason.message : fallback;
}

const REFRESH_MS = 30_000;

/**
 * The single Decisions data hook — one fetch feeds the list, the four groups,
 * and the tab badge. The page shell owns the ONE instance and passes the result
 * to the tab as props.
 *
 * F08 (privacy): there is NO SSE subscription here. A global `decision.updated`
 * stream is unauthenticated and would broadcast another owner's activity + a
 * stable private id — a cross-owner leak. Instead this refreshes the OWNER'S OWN
 * list only, via a visibility-gated interval poll (no requests in a hidden tab)
 * plus a focus refetch. Every refetch rides the principal-scoped proxy, so the
 * data never crosses owners.
 */
export function useDecisions() {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setDecisions(await fetchDecisions());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load decisions"));
    } finally {
      setLoading(false);
    }
  }, []);

  const decide = useCallback(
    async (id: string, body: DecideBody): Promise<Decision> => {
      setDeciding(true);
      try {
        const updated = await decideDecision(id, body);
        await refresh();
        return updated;
      } finally {
        setDeciding(false);
      }
    },
    [refresh],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // F08-safe refresh: owner-own-list interval poll (paused while hidden) + a
  // refetch when the tab regains focus. No cross-owner event stream.
  useEffect(() => {
    const stopPoll = startVisibilityPoll(() => void refresh(), REFRESH_MS);
    const onFocus = () => void refresh();
    if (typeof window !== "undefined") window.addEventListener("focus", onFocus);
    return () => {
      stopPoll();
      if (typeof window !== "undefined") window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  const groups = useMemo<DecisionGroups>(() => groupDecisions(decisions), [decisions]);

  // The badge counts only the visible queue: urgent + needs-the operator. MAYBE and
  // snoozed are excluded by construction (§10 / §10.3).
  const badgeCount = groups.urgent.length + groups.needsOwner.length;

  return { decisions, groups, badgeCount, loading, error, deciding, refresh, decide };
}

/** Split a flat owner-scoped list into the four rendered groups. Snoozed rows
 * (any classification) collapse into `snoozed`; everything else routes by
 * classification. */
export function groupDecisions(decisions: Decision[]): DecisionGroups {
  const groups: DecisionGroups = { urgent: [], needsOwner: [], maybe: [], snoozed: [] };
  for (const decision of decisions) {
    if (decision.status === "snoozed") {
      groups.snoozed.push(decision);
      continue;
    }
    if (decision.classification === "urgent") groups.urgent.push(decision);
    else if (decision.classification === "needs_owner") groups.needsOwner.push(decision);
    else groups.maybe.push(decision);
  }
  return groups;
}

export type UseDecisions = ReturnType<typeof useDecisions>;
