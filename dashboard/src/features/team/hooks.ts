"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useEvents } from "@/lib/useEvents";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import type { EventType } from "@/lib/contracts";
import { teamApi } from "./client";
import type {
  TeamAccountabilityResponse,
  TeamBoardResponse,
  TeamEvidence,
  TeamScoreboardResponse,
  TeamTree,
} from "./types";
import { parseTeamBoard } from "./types";
export { parseTeamBoard } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

/** The board/task SSE types that can move a Team Work OS card — same feed
 * `useLiveBoard` refreshes on, filtered to the two that name a status/claim
 * move (collab.py publishes `board.updated`/`task.updated` on
 * `update_board_task`; TeamStore's own verify/evidence writes go through a
 * raw connection and do NOT publish to this stream, which is why every
 * mutation below also calls `refresh()` directly rather than relying on SSE
 * alone). */
const TEAM_REFRESH_EVENTS: EventType[] = ["board.updated", "task.updated"];

const POLL_MS = 30_000;
const DEBOUNCE_MS = 2_000;

/** Refetch `refresh` on the shared events stream, debounced (mirrors
 * `features/collab/hooks.ts`'s private `useCollabUpdates` — reimplemented
 * here rather than imported because that hook is not exported, and
 * reusing `@/lib/useEvents`'s already-counted `EventSource` keeps this
 * feature's live-update wiring at ZERO new `new EventSource(...)` sites
 * (e2e/rail.spec.ts pins the production budget at exactly 8). */
function useRefreshOnEvents(refresh: () => void) {
  const { lastEvent } = useEvents(TEAM_REFRESH_EVENTS);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastEventId = lastEvent?.id ?? null;
  useEffect(() => {
    if (lastEventId == null) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      refresh();
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEventId]);
}

/**
 * GET /api/team/board — every person's queue, bucketed, live via SSE + a 30s
 * visibility poll (mirrors `features/collab/hooks.ts`'s `useLiveBoard`).
 */
export function useTeamBoard() {
  const [board, setBoard] = useState<TeamBoardResponse>({ pool: null, buckets: {} });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const result = await teamApi.board();
      setBoard(parseTeamBoard(result));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load the team board"));
    } finally {
      setLoading(false);
      setHasLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useRefreshOnEvents(refresh);
  useEffect(() => startVisibilityPoll(() => void refresh(), POLL_MS), [refresh]);

  return { board, loading, error, hasLoaded, refresh };
}

/** GET /api/team/tree — the company goals page's "Company goals" section. */
export function useTeamTree() {
  const [tree, setTree] = useState<TeamTree>({ companies: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setTree(await teamApi.tree());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load company goals"));
    } finally {
      setLoading(false);
      setHasLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useRefreshOnEvents(refresh);

  return { tree, loading, error, hasLoaded, refresh };
}

type ScoreboardState =
  | { state: "loading"; value: null }
  | { state: "ready"; value: TeamScoreboardResponse }
  | { state: "unavailable"; value: null };

/**
 * GET /api/team/scoreboard — DEFENSIVE by design (P5 brief): the endpoint is
 * built in a parallel package and may not exist yet, so any failure — 404,
 * network error, a body that doesn't parse as JSON — settles to
 * `"unavailable"` rather than throwing. `ScoreboardStrip` renders nothing at
 * all in that state; it never blocks the rest of the Team page.
 */
export function useTeamScoreboard() {
  const [state, setState] = useState<ScoreboardState>({ state: "loading", value: null });
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    try {
      const value = await teamApi.scoreboard();
      if (request !== generation.current) return;
      setState({ state: "ready", value });
    } catch {
      if (request !== generation.current) return;
      setState({ state: "unavailable", value: null });
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS * 2);
  }, [refresh]);
  useRefreshOnEvents(refresh);

  return { ...state, refresh };
}

type AccountabilityState =
  | { state: "loading"; value: null }
  | { state: "ready"; value: TeamAccountabilityResponse }
  | { state: "unavailable"; value: null };

/**
 * GET /api/team/accountability — DEFENSIVE, same shape as
 * `useTeamScoreboard` above (copied deliberately, not reused, for the same
 * reason that hook is not exported as a generic): any failure — 404, network
 * error, a body that doesn't parse — settles to `"unavailable"` rather than
 * throwing. `AccountabilityStrip` renders nothing at all in that state; it
 * must never block the rest of the Team page.
 */
export function useTeamAccountability() {
  const [state, setState] = useState<AccountabilityState>({ state: "loading", value: null });
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    try {
      const value = await teamApi.accountability();
      if (request !== generation.current) return;
      setState({ state: "ready", value });
    } catch {
      if (request !== generation.current) return;
      setState({ state: "unavailable", value: null });
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS * 2);
  }, [refresh]);
  useRefreshOnEvents(refresh);

  return { ...state, refresh };
}

/** GET /api/team/evidence/unattributed — the reattribution inbox. */
export function useUnattributedEvidence(limit = 50) {
  const [items, setItems] = useState<TeamEvidence[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await teamApi.unattributedEvidence(limit));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load unattributed evidence"));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => { void refresh(); }, [refresh]);

  return { items, loading, error, refresh };
}
