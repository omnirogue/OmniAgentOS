/**
 * NoteType presentation — colors and labels derived ONLY from design tokens
 * (dataviz.categorical), never raw hex. One fixed order so a type always renders the
 * same color everywhere in the app (graph nodes, legend, badges).
 */

import { chartSeriesColors } from "../../design";
import { NOTE_TYPES, type AnyNoteType, type Confidence, type NoteType } from "./types";

const TYPE_ORDER: AnyNoteType[] = [...NOTE_TYPES, "unknown"];

const TYPE_COLORS: Record<AnyNoteType, string> = (() => {
  const ramp = chartSeriesColors(NOTE_TYPES.length);
  const map = Object.fromEntries(NOTE_TYPES.map((t, i) => [t, ramp[i]!])) as Record<
    NoteType,
    string
  >;
  return { ...map, unknown: "var(--text-faint)" };
})();

const TYPE_LABELS: Record<AnyNoteType, string> = {
  run: "Run",
  experiment: "Experiment",
  learning: "Learning",
  failure: "Failure",
  decision: "Decision",
  benchmark: "Benchmark",
  discipline: "Discipline",
  source: "Source",
  tournament: "Tournament",
  leaderboard: "Leaderboard",
  playbook: "Playbook",
  prompt: "Prompt",
  unknown: "Other",
};

/** Stable order for legends / iteration — real types first, "unknown" last. */
export function noteTypeOrder(): readonly AnyNoteType[] {
  return TYPE_ORDER;
}

export function noteTypeColor(type: AnyNoteType): string {
  return TYPE_COLORS[type] ?? TYPE_COLORS.unknown;
}

export function noteTypeLabel(type: AnyNoteType): string {
  return TYPE_LABELS[type] ?? "Other";
}

/** Reuses the existing Badge tone vocabulary — never invents new colors. */
export function statusTone(status: string): "ok" | "warn" | "invalid" {
  switch (status) {
    case "active":
      return "ok";
    case "draft":
      return "warn";
    default:
      return "invalid"; // superseded, or anything else
  }
}

export function confidenceTone(confidence: Confidence | null): "ok" | "warn" | "danger" | "neutral" {
  if (confidence === "high") return "ok";
  if (confidence === "medium") return "warn";
  if (confidence === "low") return "danger";
  return "neutral";
}
