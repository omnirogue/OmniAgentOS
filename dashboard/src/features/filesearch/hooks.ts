"use client";

/**
 * Search state + data hook for the /files search-first header.
 *
 * - The query text is debounced 400ms; the discrete controls (mode / root /
 *   category / sort) refetch immediately since a click is already an
 *   intentional, single event.
 * - A monotonic generation ref guards against a slow, stale response landing
 *   after a newer one — the same pattern as features/mounts/hooks.ts's `seq`
 *   and features/collab's `requestGeneration`.
 * - Every fetch is bounded by fetchWithTimeout (inside the client), so a wedged
 *   backend surfaces as a clear error, never an infinite spinner.
 * - A 404 is treated as "backend indexing not yet deployed" (`notDeployed`),
 *   distinct from a real error, so the page can render a calm EmptyState.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { FileSearchApiError, isNotDeployed, searchFiles } from "./client";
import type { FileCategory, FileHit, FileRoot, SearchMode, SortKey } from "./types";

const DEBOUNCE_MS = 400;
const RESULT_LIMIT = 50;

/** "All" sentinel for the single-select root/category facets. */
export const ALL = "all" as const;
export type RootFilter = FileRoot | typeof ALL;
export type CategoryFilter = FileCategory | typeof ALL;

function errText(reason: unknown, fallback: string): string {
  return reason instanceof FileSearchApiError || reason instanceof Error ? reason.message : fallback;
}

export type FileSearchState = {
  query: string;
  setQuery: (value: string) => void;
  mode: SearchMode;
  setMode: (mode: SearchMode) => void;
  root: RootFilter;
  setRoot: (root: RootFilter) => void;
  category: CategoryFilter;
  setCategory: (category: CategoryFilter) => void;
  sort: SortKey;
  setSort: (sort: SortKey) => void;
  /** True once the trimmed query is non-empty — drives the mounts-collapse. */
  active: boolean;
  hits: FileHit[];
  loading: boolean;
  error: string | null;
  /** Endpoint returned 404 — the indexer/route isn't on this build yet. */
  notDeployed: boolean;
  /** A settled search (loading done, no error) has produced its result set. */
  searched: boolean;
  clear: () => void;
};

export function useFileSearch(): FileSearchState {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [mode, setMode] = useState<SearchMode>("name");
  const [root, setRoot] = useState<RootFilter>(ALL);
  const [category, setCategory] = useState<CategoryFilter>(ALL);
  const [sort, setSort] = useState<SortKey>("recency");

  const [hits, setHits] = useState<FileHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notDeployed, setNotDeployed] = useState(false);
  const [searched, setSearched] = useState(false);

  const seq = useRef(0);

  // Debounce the query text only. Filters below refetch immediately.
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [query]);

  useEffect(() => {
    const q = debounced;
    if (!q) {
      // Empty query: abandon any in-flight response and reset to the resting state.
      seq.current += 1;
      setHits([]);
      setLoading(false);
      setError(null);
      setNotDeployed(false);
      setSearched(false);
      return;
    }

    const mySeq = ++seq.current;
    setLoading(true);
    setError(null);
    setNotDeployed(false);

    void searchFiles({
      q,
      mode,
      root: root === ALL ? undefined : root,
      category: category === ALL ? undefined : category,
      sort,
      limit: RESULT_LIMIT,
    })
      .then((results) => {
        if (mySeq !== seq.current) return;
        setHits(results);
        setSearched(true);
      })
      .catch((reason: unknown) => {
        if (mySeq !== seq.current) return;
        setHits([]);
        if (isNotDeployed(reason)) {
          setNotDeployed(true);
          setSearched(true);
        } else {
          setError(errText(reason, "Could not run this search."));
        }
      })
      .finally(() => {
        if (mySeq === seq.current) setLoading(false);
      });
  }, [debounced, mode, root, category, sort]);

  const clear = useCallback(() => {
    setQuery("");
    setDebounced("");
  }, []);

  return {
    query,
    setQuery,
    mode,
    setMode,
    root,
    setRoot,
    category,
    setCategory,
    sort,
    setSort,
    active: query.trim().length > 0,
    hits,
    loading,
    error,
    notDeployed,
    searched,
    clear,
  };
}
