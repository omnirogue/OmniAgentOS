/**
 * Stable per-project color for the hierarchy tree.
 *
 * Strategy: FNV-1a hash of the project id → hue in [0, 360). Light and dark
 * theme strings are precomputed HSL so the rail/dot stays readable on both
 * surfaces without reading the active theme from JS. Same id always yields
 * the same hue across refreshes.
 */

import type { CSSProperties } from "react";

export type ProjectColor = {
  /** Deterministic hue degrees 0–360. */
  hue: number;
  /** Subtle rail / accent for dark theme (higher lightness). */
  dark: string;
  /** Subtle rail / accent for light theme (lower lightness). */
  light: string;
  /** Very soft fill tint for dark theme. */
  darkTint: string;
  /** Very soft fill tint for light theme. */
  lightTint: string;
};

/** FNV-1a 32-bit — fast, stable, good distribution for short ids. */
function hashId(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Map a top-level project id to a stable theme-aware color set. */
export function projectColor(projectId: string): ProjectColor {
  const hue = hashId(projectId) % 360;
  return {
    hue,
    // ~12–15% visual weight rails: readable but not loud.
    dark: `hsl(${hue} 48% 62%)`,
    light: `hsl(${hue} 42% 42%)`,
    darkTint: `hsl(${hue} 28% 50% / 0.12)`,
    lightTint: `hsl(${hue} 35% 50% / 0.08)`,
  };
}

/**
 * CSS custom properties to stamp on a tree root (and inherit to descendants).
 * `--project-rail` / `--project-tint` flip with `[data-theme]` via CSS.
 */
export function projectColorStyle(projectId: string): CSSProperties {
  const c = projectColor(projectId);
  return {
    ["--project-hue" as string]: String(c.hue),
    ["--project-rail-dark" as string]: c.dark,
    ["--project-rail-light" as string]: c.light,
    ["--project-tint-dark" as string]: c.darkTint,
    ["--project-tint-light" as string]: c.lightTint,
  };
}
