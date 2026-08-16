"use client";

import { useEffect, useState } from "react";
import { fetchVaultNote, VaultApiError } from "./api";
import { FIXTURE_DETAILS } from "./fixtures";
import { isSafeVaultPath } from "./paths";
import type { FetchStatus, VaultNoteDetail } from "./types";
import { USE_FIXTURES } from "./useVaultData";

export interface NoteDetailState {
  status: FetchStatus;
  detail: VaultNoteDetail | null;
  error: string | null;
  refresh: () => void;
}

/**
 * Fetches GET /api/lab/vault/note?path= for one note, fixtures-aware, path-safe.
 * `path === null` means "nothing to load yet" (e.g. waiting on the tree to resolve a
 * default note) — it is a quiet no-op, not an error.
 */
export function useNoteDetail(path: string | null): NoteDetailState {
  const [status, setStatus] = useState<FetchStatus>("loading");
  const [detail, setDetail] = useState<VaultNoteDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setDetail(null);

    if (path === null) {
      setStatus("loading");
      return;
    }

    setStatus("loading");

    if (!isSafeVaultPath(path)) {
      setStatus("error");
      setError("This is not a valid vault path.");
      return;
    }

    if (USE_FIXTURES) {
      const fixture = FIXTURE_DETAILS[path];
      if (fixture) {
        setDetail(fixture);
        setStatus("ready");
      } else {
        setStatus("empty");
      }
      return;
    }

    fetchVaultNote(path)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setStatus("ready");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof VaultApiError && reason.status === 404) {
          setStatus("empty");
          return;
        }
        const message = reason instanceof Error ? reason.message : "Could not load this note.";
        setError(message);
        setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [path, nonce]);

  return { status, detail, error, refresh: () => setNonce((n) => n + 1) };
}
