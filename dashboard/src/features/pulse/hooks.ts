/**
 * React hooks for the Observatory /pulse surface. All hooks follow the
 * same ``(data, status, error, refresh)`` shape the rest of the dashboard
 * uses — consistent enough that PulseTile.tsx can render any tile from
 * the same component tree regardless of metric.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchActiveRoutines,
  fetchCapabilitySnapshot,
  fetchPulseSeries,
  fetchSystemDelta,
  type ActiveRoutineRow,
} from "./client";
import type {
  CapabilitySnapshot,
  DeltaResponse,
  MetricName,
  PulsePoint,
  SeriesResponse,
} from "./types";

export type AsyncStatus = "loading" | "ready" | "empty" | "error";

export type UseSeriesResult = {
  series: SeriesResponse | null;
  points: PulsePoint[];
  values: number[];
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

export function usePulseSeries(metric: MetricName, days = 30): UseSeriesResult {
  const [series, setSeries] = useState<SeriesResponse | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchPulseSeries(metric, days).then(
      (res) => {
        if (!mountedRef.current) return;
        setSeries(res);
        setStatus(res.points.length === 0 ? "empty" : "ready");
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        setSeries(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, [metric, days]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  const { points, values } = useMemo(() => {
    const pts = series?.points ?? [];
    return { points: pts, values: pts.map((p) => p.value) };
  }, [series]);

  return { series, points, values, status, error, refresh: load };
}

export type UseDeltaResult = {
  delta: DeltaResponse | null;
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

const LAST_SEEN_KEY = "omniagentos.last_seen";

/** Read/refresh the "since you were gone" delta, persisting last_seen. */
export function useSystemDelta(): UseDeltaResult {
  const [delta, setDelta] = useState<DeltaResponse | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    let since: string;
    if (typeof window === "undefined") {
      // SSR: default to 1 day ago so the first server-rendered page has content.
      since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    } else {
      const stored = window.localStorage.getItem(LAST_SEEN_KEY);
      since = stored ?? new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    }
    setStatus("loading");
    setError(null);
    fetchSystemDelta(since).then(
      (res) => {
        setDelta(res);
        setStatus(totalDelta(res) === 0 ? "empty" : "ready");
      },
      (err: unknown) => {
        setDelta(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, []);

  useEffect(() => {
    load();
    // Stamp the new last_seen AFTER the fetch resolves so the NEXT visit
    // sees the delta for "since this visit". Doing it before would skip
    // the count the user just landed on.
    if (typeof window !== "undefined") {
      window.localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString());
    }
  }, [load]);

  return { delta, status, error, refresh: load };
}

export type UseCapabilityResult = {
  snapshot: CapabilitySnapshot | null;
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

/** Capability tile data: ELO leader + ranked/tournament counts (live lab API). */
export function useCapabilityPulse(): UseCapabilityResult {
  const [snapshot, setSnapshot] = useState<CapabilitySnapshot | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchCapabilitySnapshot().then(
      (res) => {
        setSnapshot(res);
        setStatus(res.topElo === null && res.ranked === 0 ? "empty" : "ready");
      },
      (err: unknown) => {
        setSnapshot(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { snapshot, status, error, refresh: load };
}

export function useActiveRoutines(limit = 5) {
  const [rows, setRows] = useState<ActiveRoutineRow[]>([]);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchActiveRoutines(limit).then(
      (res) => {
        setRows(res);
        setStatus(res.length === 0 ? "empty" : "ready");
      },
      (err: unknown) => {
        setRows([]);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, [limit]);

  useEffect(() => {
    load();
  }, [load]);

  return { rows, status, error, refresh: load };
}

/** True when every counter in a delta response is 0 (nothing happened). */
export function totalDelta(d: DeltaResponse): number {
  return (
    d.skills_updated +
    d.improvements_decided +
    d.loops_run +
    d.tasks_completed +
    d.chats_active
  );
}
