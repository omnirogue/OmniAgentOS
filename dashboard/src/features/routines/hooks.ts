"use client";

import { useCallback, useEffect, useState } from "react";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { RoutinesApiError, routinesApi } from "./api";
import type { SystemJobsSnapshot } from "./systemJobs";
import type { CreateRoutineInput, LoopTemplate, RecentRunItem, Routine } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof RoutinesApiError || reason instanceof Error) return reason.message;
  return fallback;
}

export function useRoutines(status?: string) {
  const [routines, setRoutines] = useState<Routine[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRoutines(await routinesApi.list(status));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load routines"));
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setEnabled = useCallback(async (id: string, enabled: boolean) => {
    const updated = enabled ? await routinesApi.enable(id) : await routinesApi.disable(id);
    setRoutines((current) => current.map((r) => (r.id === id ? updated : r)));
    return updated;
  }, []);

  const remove = useCallback(async (id: string) => {
    await routinesApi.remove(id);
    setRoutines((current) => current.filter((r) => r.id !== id));
  }, []);

  return { routines, loading, error, refresh, setEnabled, remove };
}

export function useCreateRoutine() {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = useCallback(async (input: CreateRoutineInput): Promise<Routine> => {
    setSaving(true);
    setError(null);
    try {
      const routine = await routinesApi.create(input);
      return routine;
    } catch (reason) {
      setError(errorMessage(reason, "Failed to create routine"));
      throw reason;
    } finally {
      setSaving(false);
    }
  }, []);

  return { create, saving, error };
}

export function useRecentRuns(limit = 50) {
  const [runs, setRuns] = useState<RecentRunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRuns(await routinesApi.recentRuns(limit));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load recent runs"));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { runs, loading, error, refresh };
}

export function useRecommendedLoops() {
  const [templates, setTemplates] = useState<LoopTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setTemplates(await routinesApi.recommendedLoops());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load recommended loops"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { templates, loading, error, refresh };
}

/** Snapshot data goes stale the moment it's fetched — relative "last run"
 * text and the launchctl/remote-probe availability notes are only honest
 * while the page keeps re-fetching. Polls every 60s (paused in a hidden
 * tab, matching `useLoopHealth`'s convention) on top of the initial load
 * and the caller-triggered manual `refresh()`. */
const SYSTEM_JOBS_POLL_MS = 60_000;

export function useSystemJobs() {
  const [snapshot, setSnapshot] = useState<SystemJobsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setSnapshot(await routinesApi.systemJobs());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load system loops"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), SYSTEM_JOBS_POLL_MS);
  }, [refresh]);

  return { snapshot, loading, error, refresh };
}
