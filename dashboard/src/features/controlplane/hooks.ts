"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { fetchEngineCapabilities, fetchEngineRunSnapshot, fetchEngineRuns } from "./client";
import type {
  EngineCapabilities,
  EngineEvidence,
  EngineRunSnapshot,
  EngineRunSummary,
} from "./types";

const CAPABILITIES_UNAVAILABLE = "LoopDeck engine status is unavailable.";
const SNAPSHOT_UNAVAILABLE =
  "Bound engine run snapshot is unavailable. Showing the last confirmed snapshot when available.";

const ADVERSE_STATUSES = new Set(["failed", "error"]);

function isAdverseStatus(status: string | null | undefined): boolean {
  return typeof status === "string" && ADVERSE_STATUSES.has(status.toLowerCase());
}

/**
 * Merge two evidence lists by identity, keeping the first occurrence's
 * position and values UNLESS a later duplicate carries adverse information
 * the currently kept entry does not — a later failure (or refusal) must
 * never be masked by an earlier pass (or absence). Adverse information
 * merges FIELD-WISE, never via whole-entry replacement: replacing the whole
 * entry lets a later duplicate silently drop an earlier adverse fact it does
 * not itself carry (round-3: a later passed+refused duplicate was clobbering
 * an earlier failed status, and vice versa, because the whole entry was
 * swapped in). Two adverse carriers merge independently: `status` is taken
 * from whichever carrier is adverse (existing wins if it already is; the
 * incoming entry's status only overrides when it is adverse and the kept
 * one is not), and `url_refused` is sticky true if EITHER carrier set it
 * (CP-003 round 2: a later real refusal must not be swallowed by an earlier
 * merely-absent URL). Every other field keeps its first-occurrence value.
 * Entries with no `status` field never participate in the status override
 * (only the identity-based dedup and url_refused merge apply).
 */
function mergeById<T>(
  prior: T[],
  incoming: T[],
  identity: (entry: T) => string,
  status?: (entry: T) => string | null | undefined,
): T[] {
  const merged = new Map<string, T>();
  for (const entry of [...prior, ...incoming]) {
    const key = identity(entry);
    const existing = merged.get(key);
    if (!existing) {
      merged.set(key, entry);
      continue;
    }
    let next: T = existing;
    if (status && isAdverseStatus(status(entry)) && !isAdverseStatus(status(existing))) {
      next = { ...next, status: status(entry) } as T;
    }
    const entryRefused = (entry as { url_refused?: boolean }).url_refused === true;
    const existingRefused = (existing as { url_refused?: boolean }).url_refused === true;
    if (entryRefused && !existingRefused) {
      next = { ...next, url_refused: true } as T;
    }
    merged.set(key, next);
  }
  return Array.from(merged.values());
}

/**
 * Fold one poll's snapshot into the accumulated one.
 *
 * Activity arrives cursor-windowed and evidence arrives as partial, repeated
 * reports, so both accumulate with first-occurrence-wins dedup — except a
 * later report carrying an adverse status (failed/error) always overrides an
 * earlier non-adverse one for the same identity (see `mergeById`). The run id
 * is the authoritative binding: when it changes, nothing from the previous
 * run survives — a stale commit or message shown under a new run would be a
 * lie about what that run did.
 */
export function mergeEngineRunSnapshot(
  previous: EngineRunSnapshot | null,
  incoming: EngineRunSnapshot,
): EngineRunSnapshot {
  const sameBinding = previous !== null && previous.run.id === incoming.run.id;
  const priorActivity = sameBinding ? previous.activity : [];
  const priorEvidence: EngineEvidence = sameBinding
    ? previous.evidence
    : { commits: [], files: [], tests: [], reports: [] };

  const activity = Array.from(
    new Map(
      [...priorActivity, ...incoming.activity].map(
        (event) => [event.id, event] as const,
      ),
    ).values(),
  ).sort((a, b) => a.id - b.id);

  return {
    ...incoming,
    activity,
    evidence: {
      commits: mergeById(priorEvidence.commits, incoming.evidence.commits, (e) => e.sha),
      files: mergeById(priorEvidence.files, incoming.evidence.files, (e) => e.path),
      tests: mergeById(
        priorEvidence.tests,
        incoming.evidence.tests,
        (e) => e.name,
        (e) => e.status,
      ),
      reports: mergeById(
        priorEvidence.reports,
        incoming.evidence.reports,
        (e) => e.name,
        (e) => e.status,
      ),
    },
  };
}

/** The trimmed surface `ControlPlaneView` renders against. */
export interface ControlPlaneState {
  runs: EngineRunSummary[];
  /** The bound run: the operator's selection, else the newest run, else none. */
  runId: string | null;
  setRunId: (id: string) => void;
  capabilities: EngineCapabilities | null;
  capabilitiesError: string | null;
  snapshot: EngineRunSnapshot | null;
  snapshotError: string | null;
  loading: boolean;
  hasLoaded: boolean;
  /** Blocks first paint: the run list failed, so there is no truthful binding. */
  error: string | null;
  stale: boolean;
  refresh: () => void;
}

/**
 * Poll the engine's capabilities, run list, and bound-run snapshot.
 *
 * Only a failed run list blocks first paint (`error`): without it there is no
 * binding and nothing truthful to show. A failed capabilities or snapshot read
 * degrades to its own message beside whatever was last confirmed.
 */
export function useControlPlane(pollMs = 15_000): ControlPlaneState {
  const [runs, setRuns] = useState<EngineRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<EngineCapabilities | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<EngineRunSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generation = useRef(0);
  // Keyed by run id so a binding change restarts the activity window at 0
  // instead of resuming a different run's cursor.
  const cursor = useRef<{ runId: string; value: number }>({ runId: "", value: 0 });

  const load = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const [runsResult, capabilitiesResult] = await Promise.allSettled([
        fetchEngineRuns(),
        fetchEngineCapabilities(),
      ]);
      if (gen !== generation.current) return;

      if (capabilitiesResult.status === "fulfilled") {
        setCapabilities(capabilitiesResult.value);
        setCapabilitiesError(null);
      } else {
        setCapabilitiesError(CAPABILITIES_UNAVAILABLE);
      }

      if (runsResult.status === "rejected") {
        throw runsResult.reason instanceof Error
          ? runsResult.reason
          : new Error("Failed to load LoopDeck runs");
      }
      const nextRuns = runsResult.value.runs;
      setRuns(nextRuns);

      const bound = selectedRunId ?? nextRuns[0]?.id ?? null;
      if (cursor.current.runId !== (bound ?? "")) {
        cursor.current = { runId: bound ?? "", value: 0 };
      }

      if (!bound) {
        setSnapshot(null);
        setSnapshotError(null);
      } else {
        const [snapshotResult] = await Promise.allSettled([
          fetchEngineRunSnapshot(bound, cursor.current.value),
        ]);
        if (gen !== generation.current) return;
        if (snapshotResult.status === "fulfilled") {
          cursor.current = { runId: bound, value: snapshotResult.value.next_activity_cursor };
          setSnapshot((previous) =>
            mergeEngineRunSnapshot(
              previous !== null && previous.run.id === bound ? previous : null,
              snapshotResult.value,
            ),
          );
          setSnapshotError(null);
        } else {
          setSnapshot((previous) =>
            previous !== null && previous.run.id === bound ? previous : null,
          );
          setSnapshotError(SNAPSHOT_UNAVAILABLE);
        }
      }

      setHasLoaded(true);
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(reason instanceof Error ? reason.message : "Failed to load LoopDeck runs");
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, [selectedRunId]);

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setLoading(true);
    void load();
    return startVisibilityPoll(() => void load(), pollMs);
  }, [load, pollMs]);

  const setRunId = useCallback((id: string) => setSelectedRunId(id), []);

  return {
    runs,
    runId: selectedRunId ?? runs[0]?.id ?? null,
    setRunId,
    capabilities,
    capabilitiesError,
    snapshot,
    snapshotError,
    loading,
    hasLoaded,
    error,
    stale: Boolean(error) && hasLoaded,
    refresh,
  };
}
