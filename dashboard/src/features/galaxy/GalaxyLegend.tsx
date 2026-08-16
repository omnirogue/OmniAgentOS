"use client";

import { noteTypeColor, noteTypeLabel, noteTypeOrder } from "./noteType";
import type { AnyNoteType } from "./types";

export interface GalaxyLegendProps {
  typeCounts: Map<AnyNoteType, number>;
  hiddenTypes: ReadonlySet<AnyNoteType>;
  onToggle: (type: AnyNoteType) => void;
}

/** Type legend that doubles as a filter — click a type to hide/show its notes. */
export function GalaxyLegend({ typeCounts, hiddenTypes, onToggle }: GalaxyLegendProps) {
  const active = noteTypeOrder().filter((t) => (typeCounts.get(t) ?? 0) > 0);
  if (active.length === 0) return null;

  return (
    <ul
      aria-label="Note types (click to filter)"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "var(--space-2)",
        margin: 0,
        padding: 0,
        listStyle: "none",
      }}
    >
      {active.map((type) => {
        const hidden = hiddenTypes.has(type);
        const count = typeCounts.get(type) ?? 0;
        return (
          <li key={type}>
            <button
              type="button"
              onClick={() => onToggle(type)}
              aria-pressed={!hidden}
              title={hidden ? `Show ${noteTypeLabel(type)} notes` : `Hide ${noteTypeLabel(type)} notes`}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "0.4rem",
                border: "1px solid var(--border)",
                background: "var(--surface)",
                borderRadius: "var(--radius-pill)",
                padding: "0.2rem 0.6rem 0.2rem 0.45rem",
                fontSize: "var(--text-micro)",
                color: hidden ? "var(--text-faint)" : "var(--text-muted)",
                cursor: "pointer",
                opacity: hidden ? 0.55 : 1,
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  width: "0.55rem",
                  height: "0.55rem",
                  borderRadius: "50%",
                  background: noteTypeColor(type),
                  flexShrink: 0,
                }}
              />
              {noteTypeLabel(type)}
              <span style={{ color: "var(--text-faint)" }}>{count}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
