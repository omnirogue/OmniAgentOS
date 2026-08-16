"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AccountsApiError,
  addAccount,
  listAccounts,
  listUsage,
  pauseAccount,
  removeAccount,
  resumeAccount,
  setAccountDefault,
  setAccountEnabled,
} from "./client";
import type { Account, AddAccountInput, ProviderUsage, UsageWindow } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof AccountsApiError || reason instanceof Error) return reason.message;
  return fallback;
}

/**
 * Loads + mutates the account roster. `setEnabled`/`remove` patch (or drop)
 * just the one affected row locally since neither has side effects on other
 * rows. `create`/`makeDefault` refetch the whole list instead — adding an
 * account or flipping the default can change `is_default` on ANOTHER row
 * too (only one account is ever default), and the API only ever returns the
 * single row the call targeted, so a local patch could leave a stale
 * `is_default: true` behind on the row that just lost it.
 */
export function useAccounts() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setAccounts(await listAccounts());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load accounts"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = useCallback(
    async (input: AddAccountInput): Promise<Account> => {
      const created = await addAccount(input);
      await refresh();
      return created;
    },
    [refresh],
  );

  const setEnabled = useCallback(async (id: string, enabled: boolean): Promise<Account> => {
    const updated = await setAccountEnabled(id, enabled);
    setAccounts((current) => current.map((account) => (account.id === id ? updated : account)));
    return updated;
  }, []);

  const makeDefault = useCallback(
    async (id: string): Promise<Account> => {
      const updated = await setAccountDefault(id);
      await refresh();
      return updated;
    },
    [refresh],
  );

  const remove = useCallback(async (id: string): Promise<void> => {
    await removeAccount(id);
    setAccounts((current) => current.filter((account) => account.id !== id));
  }, []);

  const patchOne = useCallback((updated: Account) => {
    setAccounts((current) => current.map((a) => (a.id === updated.id ? updated : a)));
  }, []);

  const pause = useCallback(
    async (id: string, minutes: number, reason: string): Promise<Account> => {
      const updated = await pauseAccount(id, { minutes, reason });
      patchOne(updated);
      return updated;
    },
    [patchOne],
  );

  const resume = useCallback(
    async (id: string): Promise<Account> => {
      const updated = await resumeAccount(id);
      patchOne(updated);
      return updated;
    },
    [patchOne],
  );

  return { accounts, loading, error, refresh, create, setEnabled, makeDefault, remove, pause, resume };
}

/**
 * Quota snapshots, loaded independently of the roster.
 *
 * Kept in its own hook and its own request on purpose: collecting reads files
 * off disk, so a slow or failing collection must never delay or blank the
 * accounts table. A usage error surfaces as "unknown" in the Usage column
 * while every other column keeps working.
 */
export function useAccountUsage() {
  const [usage, setUsage] = useState<ProviderUsage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setUsage(await listUsage());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load usage"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Claude snapshots join to an account by config dir; every other provider is
  // fleet-wide (one row per provider, no account to attach it to).
  const byConfigDir = useMemo(() => {
    const index = new Map<string, ProviderUsage>();
    for (const snapshot of usage) {
      if (snapshot.provider === "claude" && snapshot.account_key) {
        index.set(snapshot.account_key, snapshot);
      }
    }
    return index;
  }, [usage]);

  const otherProviders = useMemo(
    () => usage.filter((snapshot) => snapshot.provider !== "claude"),
    [usage],
  );

  return { usage, byConfigDir, otherProviders, loading, error, refresh };
}

/** The window closest to exhaustion — what a one-line summary should show. */
export function worstWindow(snapshot: ProviderUsage | undefined): UsageWindow | null {
  if (!snapshot?.windows.length) return null;
  return snapshot.windows.reduce((worst, w) => (w.percent > worst.percent ? w : worst));
}
