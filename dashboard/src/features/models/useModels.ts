"use client";

/**
 * useModels — the ONE model-list hook for the app (chat-v2 §2.3.1).
 *
 * Feeds the composer ModelPicker popover from GET /api/models. Falls back to
 * [{id:"auto"}] with a warn flag when the endpoint fails (§2.8) — the picker
 * renders the warn badge and keeps working.
 */

import { useCallback, useEffect, useState } from "react";
import { fetchModels, type ModelEntry } from "@/features/chats/chatApi";

export interface ModelsState {
  models: ModelEntry[];
  loading: boolean;
  error: string | null;
  /** True when the endpoint failed and the auto-only fallback is in use. */
  fallback: boolean;
  refresh: () => Promise<void>;
}

const FALLBACK_MODELS: ModelEntry[] = [
  {
    id: "auto",
    label: "Auto — router decides",
    provider: "router",
    tier: null,
    available: true,
    lineage: null,
  },
];

export function useModels(): ModelsState {
  const [state, setState] = useState<Omit<ModelsState, "refresh">>({
    models: [],
    loading: true,
    error: null,
    fallback: false,
  });

  const refresh = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true }));
    try {
      const models = await fetchModels();
      setState({ models, loading: false, error: null, fallback: false });
    } catch (reason) {
      setState({
        models: [...FALLBACK_MODELS],
        loading: false,
        error: reason instanceof Error ? reason.message : "Could not load models",
        fallback: true,
      });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { ...state, refresh };
}

/** Legacy select-option adapter (HierarchyViews); prefer useModels() directly. */
export function useModelOptions(): Array<{ value: string; label: string }> {
  const { models, fallback } = useModels();
  if (fallback || models.length === 0) {
    return [{ value: "", label: "Auto — router decides" }];
  }
  return models.map((m) => ({
    value: m.id,
    label: m.available ? m.label : `${m.label} (unavailable)`,
  }));
}
