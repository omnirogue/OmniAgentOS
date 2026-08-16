"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchVaultTree, VaultApiError } from "./api";
import { buildLinkIndex, type VaultLinkIndex } from "./linkResolver";
import type { FetchStatus, VaultTreeNote } from "./types";
import { FIXTURE_TREE } from "./fixtures";

/**
 * Dev/showcase-only fixture switch — OBVIOUS and OFF by default. Never used as a silent
 * fallback for an empty or failing backend; only active when explicitly set:
 *   NEXT_PUBLIC_VAULT_USE_FIXTURES=1 npm run dev
 */
export const USE_FIXTURES = process.env.NEXT_PUBLIC_VAULT_USE_FIXTURES === "1";

export interface VaultDataState {
  status: FetchStatus;
  notes: VaultTreeNote[];
  linkIndex: VaultLinkIndex;
  error: string | null;
  usingFixtures: boolean;
  refresh: () => void;
}

/** Fetches the vault tree once, exposes notes + a link index, degrades gracefully. */
export function useVaultData(): VaultDataState {
  const [status, setStatus] = useState<FetchStatus>("loading");
  const [notes, setNotes] = useState<VaultTreeNote[]>([]);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const load = useCallback(() => {
    const gen = ++generation.current;
    setStatus("loading");
    setError(null);

    if (USE_FIXTURES) {
      setNotes(FIXTURE_TREE);
      setStatus(FIXTURE_TREE.length === 0 ? "empty" : "ready");
      return;
    }

    fetchVaultTree()
      .then((result) => {
        if (generation.current !== gen) return;
        setNotes(result);
        setStatus(result.length === 0 ? "empty" : "ready");
      })
      .catch((reason: unknown) => {
        if (generation.current !== gen) return;
        const message =
          reason instanceof VaultApiError
            ? reason.message
            : reason instanceof Error
              ? reason.message
              : "Could not load the vault.";
        setError(message);
        setStatus("error");
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const linkIndex = useMemo(() => buildLinkIndex(notes), [notes]);

  return { status, notes, linkIndex, error, usingFixtures: USE_FIXTURES, refresh: load };
}
