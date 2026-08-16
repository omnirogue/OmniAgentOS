"use client";

/**
 * Cmd-K integration point for the vault. U01's CommandPalette (dashboard/src/design/
 * CommandPalette.tsx) already ships a "Vault" entry (-> /vault) in DEFAULT_COMMANDS. We
 * cannot edit layout.tsx/AppShell.tsx to splice extra entries into the mounted palette
 * (out of ownership), so instead we EXPOSE this hook: a future edit to the shell can do
 *   <CommandPalette commands={[...DEFAULT_COMMANDS, ...useVaultCommands()]} />
 * to add "search vault" / "open memory galaxy" entries without this package touching
 * shell files. Kept intentionally trivial per the brief ("if trivial").
 */

import { useMemo } from "react";
import type { CommandItem } from "../../design";

export function vaultCommandItems(): CommandItem[] {
  return [
    { id: "vault-galaxy", label: "Open memory galaxy", group: "Vault", href: "/vault", keywords: "graph notes knowledge obsidian" },
    { id: "vault-search", label: "Search vault…", group: "Vault", href: "/vault/search", keywords: "notes knowledge find full-text" },
  ];
}

export function useVaultCommands(): CommandItem[] {
  return useMemo(() => vaultCommandItems(), []);
}
