"use client";

import { createContext, useContext, type ReactNode } from "react";
import { useVaultData, type VaultDataState } from "./useVaultData";

const VaultDataCtx = createContext<VaultDataState | null>(null);

/**
 * Fetches the vault tree ONCE at the /vault route-group root (see app/vault/layout.tsx)
 * and shares it with every page under /vault — the graph, the note reader, and search
 * all read the same notes/linkIndex without re-fetching per navigation.
 */
export function VaultDataProvider({ children }: { children: ReactNode }) {
  const value = useVaultData();
  return <VaultDataCtx.Provider value={value}>{children}</VaultDataCtx.Provider>;
}

export function useVaultDataContext(): VaultDataState {
  const ctx = useContext(VaultDataCtx);
  if (!ctx) {
    throw new Error("useVaultDataContext must be used within VaultDataProvider (app/vault/layout.tsx)");
  }
  return ctx;
}
