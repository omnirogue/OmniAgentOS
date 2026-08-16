"use client";

import Link from "next/link";
import { noteTypeColor, noteTypeLabel } from "./noteType";
import { vaultHref } from "./paths";
import type { VaultTreeNote } from "./types";

export interface BacklinksProps {
  backlinks: VaultTreeNote[];
}

/** "Who links here" — notes whose forward wikilinks resolve to the current note. */
export function Backlinks({ backlinks }: BacklinksProps) {
  if (backlinks.length === 0) {
    return (
      <p style={{ margin: 0, color: "var(--text-faint)", fontSize: "var(--text-small)" }}>
        No other notes link here yet.
      </p>
    );
  }

  return (
    <ul style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)", margin: 0, padding: 0, listStyle: "none" }}>
      {backlinks.map((note) => (
        <li key={note.path}>
          <Link
            href={vaultHref(note.path)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--space-2)",
              padding: "var(--space-2)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text)",
            }}
          >
            <span
              aria-hidden="true"
              style={{ width: "0.5rem", height: "0.5rem", borderRadius: "50%", background: noteTypeColor(note.type), flexShrink: 0 }}
            />
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {note.title}
            </span>
            <span style={{ fontSize: "var(--text-micro)", color: "var(--text-faint)", whiteSpace: "nowrap" }}>
              {noteTypeLabel(note.type)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
