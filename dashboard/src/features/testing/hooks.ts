/**
 * React hooks for the Observatory /testing surface. Same
 * ``(data, status, error, refresh)`` shape as features/pulse/hooks.ts, with
 * a mountedRef guard so a slow fetch never sets state after unmount.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTestObsOverview, fetchTestObsSeries, fetchTestObsWeakspots } from "./client";
import { seriesIsEmpty } from "./tiles";
import type { TestObsCategory, TestObsOverview, TestObsSeriesResponse, WeakspotItem, WeakspotSources } from "./types";

export type AsyncStatus = "loading" | "ready" | "empty" | "error";

export type UseTestObsOverviewResult = {
  data: TestObsOverview | null;
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

/** Overview is a single object, never "empty" once it loads — every
 * category is always present, even if individually unavailable. */
export function useTestObsOverview(): UseTestObsOverviewResult {
  const [data, setData] = useState<TestObsOverview | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchTestObsOverview().then(
      (res) => {
        if (!mountedRef.current) return;
        setData(res);
        setStatus("ready");
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        setData(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  return { data, status, error, refresh: load };
}

export type UseTestObsSeriesResult = {
  data: TestObsSeriesResponse | null;
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

export function useTestObsSeries(category: TestObsCategory, days = 90): UseTestObsSeriesResult {
  const [data, setData] = useState<TestObsSeriesResponse | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchTestObsSeries(category, days).then(
      (res) => {
        if (!mountedRef.current) return;
        setData(res);
        setStatus(seriesIsEmpty(res) ? "empty" : "ready");
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        setData(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, [category, days]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  return { data, status, error, refresh: load };
}

export type UseWeakspotsResult = {
  items: WeakspotItem[];
  /** Per-source availability the panel uses to tell a genuine "nothing
   * ranked" all-clear from an incomplete ranking (grok T2-M2) — null only
   * before the first response lands or after an error. */
  sources: WeakspotSources | null;
  status: AsyncStatus;
  error: string | null;
  refresh: () => void;
};

export function useWeakspots(): UseWeakspotsResult {
  const [items, setItems] = useState<WeakspotItem[]>([]);
  const [sources, setSources] = useState<WeakspotSources | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const load = useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchTestObsWeakspots().then(
      (res) => {
        if (!mountedRef.current) return;
        setItems(res.items);
        setSources(res.sources);
        setStatus(res.items.length === 0 ? "empty" : "ready");
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        setItems([]);
        setSources(null);
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  return { items, sources, status, error, refresh: load };
}
