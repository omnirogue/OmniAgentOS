"use client";

/**
 * Data hooks for the machine-mounts browse surface (P6). Deliberately has NO
 * fixtures fallback — unlike features/projects's hierarchyHooks.ts, a failed
 * mounts fetch surfaces as a real `ErrorState` with `onRetry`, because this
 * page exists specifically to show what the machine's filesystem actually
 * looks like right now; masking a backend failure with fixture data here
 * would be actively misleading.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMountDir, fetchMounts, MountApiError } from "./client";
import type { Mount, MountDirEntry } from "./types";

function errText(reason: unknown, fallback: string): string {
  return reason instanceof MountApiError || reason instanceof Error ? reason.message : fallback;
}

export type MountsState = {
  mounts: Mount[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
};

export function useMounts(): MountsState {
  const [mounts, setMounts] = useState<Mount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchMounts();
      setMounts(data);
      setError(null);
    } catch (reason) {
      setError(errText(reason, "Could not load the mounts registry."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { mounts, loading, error, refresh };
}

const DEFAULT_LIMIT = 500;

export type MountDirState = {
  entries: MountDirEntry[];
  total: number;
  truncated: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  loadMore: () => Promise<void>;
  refresh: () => Promise<void>;
};

/**
 * Lazy, per-directory listing for one mount's browser: fetches the first page
 * whenever `mountId`/`path` change and exposes `loadMore` for the
 * offset/limit-paginated remainder (`truncated` from the P2 contract). No
 * cross-directory caching — matches the server's own "shallow, never
 * recurses" listing contract, and keeps a directory's view always fresh.
 */
export function useMountDir(mountId: string | null, path: string): MountDirState {
  const [entries, setEntries] = useState<MountDirEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Guards a slow, stale request from landing after a newer directory's
  // response — same pattern as projects/page.tsx's `countsSeq` (F10).
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!mountId) return;
    const mySeq = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMountDir(mountId, path, 0, DEFAULT_LIMIT);
      if (mySeq !== seq.current) return;
      setEntries(data.entries);
      setTotal(data.total);
      setTruncated(data.truncated);
    } catch (reason) {
      if (mySeq !== seq.current) return;
      setEntries([]);
      setTotal(0);
      setTruncated(false);
      setError(errText(reason, "Could not load this directory."));
    } finally {
      if (mySeq === seq.current) setLoading(false);
    }
  }, [mountId, path]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadMore = useCallback(async () => {
    if (!mountId || loadingMore || !truncated) return;
    const mySeq = seq.current;
    setLoadingMore(true);
    try {
      const data = await fetchMountDir(mountId, path, entries.length, DEFAULT_LIMIT);
      if (mySeq !== seq.current) return;
      setEntries((prev) => [...prev, ...data.entries]);
      setTotal(data.total);
      setTruncated(data.truncated);
      setError(null);
    } catch (reason) {
      if (mySeq !== seq.current) return;
      setError(errText(reason, "Could not load more entries."));
    } finally {
      if (mySeq === seq.current) setLoadingMore(false);
    }
  }, [mountId, path, entries.length, loadingMore, truncated]);

  return { entries, total, truncated, loading, loadingMore, error, loadMore, refresh: load };
}
