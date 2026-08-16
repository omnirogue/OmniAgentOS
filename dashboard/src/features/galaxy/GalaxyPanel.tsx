"use client";

import { usePathname, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { GalaxyGraph } from "./GalaxyGraph";
import { GalaxyLegend } from "./GalaxyLegend";
import { vaultHref } from "./paths";
import { SearchPanel } from "./SearchPanel";
import type { AnyNoteType } from "./types";
import { useVaultDataContext } from "./VaultDataContext";

function safeDecode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Derive the currently-open note path (if any) from the /vault/* pathname. */
function selectedPathFrom(pathname: string): string | null {
  if (!pathname.startsWith("/vault/")) return null;
  const rest = pathname.slice("/vault/".length);
  if (!rest || rest === "search" || rest.startsWith("search/")) return null;
  return rest
    .split("/")
    .filter(Boolean)
    .map(safeDecode)
    .join("/");
}

/**
 * The persistent left-hand pane for the whole /vault route group (mounted once by
 * app/vault/layout.tsx): search, the type legend/filter, and the memory galaxy graph.
 * Selecting a node or a search result navigates the shared route so the reader on the
 * right (children) swaps while this pane — and its computed layout — stays mounted.
 */
export function GalaxyPanel() {
  const { status, notes, linkIndex, error, refresh } = useVaultDataContext();
  const router = useRouter();
  const pathname = usePathname() ?? "/vault";
  const [hiddenTypes, setHiddenTypes] = useState<Set<AnyNoteType>>(() => new Set());

  const selectedPath = useMemo(() => selectedPathFrom(pathname), [pathname]);

  const typeCounts = useMemo(() => {
    const counts = new Map<AnyNoteType, number>();
    for (const note of notes) counts.set(note.type, (counts.get(note.type) ?? 0) + 1);
    return counts;
  }, [notes]);

  const toggleType = (type: AnyNoteType) => {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <SearchPanel placeholder="Search notes, runs, learnings…" onNavigate={(path) => router.push(vaultHref(path))} />
      <GalaxyLegend typeCounts={typeCounts} hiddenTypes={hiddenTypes} onToggle={toggleType} />
      <GalaxyGraph
        notes={notes}
        linkIndex={linkIndex}
        status={status}
        errorMessage={error}
        onRetry={refresh}
        selectedPath={selectedPath}
        onSelectNote={(note) => router.push(vaultHref(note.path))}
        hiddenTypes={hiddenTypes}
        height={520}
      />
    </div>
  );
}
