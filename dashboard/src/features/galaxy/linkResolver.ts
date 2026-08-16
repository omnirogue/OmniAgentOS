/**
 * Obsidian-style `[[wikilink]]` resolution against the vault tree. A link target may be
 * written as the note's frontmatter id, its vault path, or its filename stem (the usual
 * Obsidian convention) — resolved in that priority order, case-insensitively. Unresolved
 * links render as inert text rather than a guessed/broken navigation.
 */

import { pathStem } from "./paths";
import type { VaultTreeNote } from "./types";

export interface VaultLinkIndex {
  byId: Map<string, VaultTreeNote>;
  byPath: Map<string, VaultTreeNote>;
  byStem: Map<string, VaultTreeNote>;
  byTitle: Map<string, VaultTreeNote>;
}

export function buildLinkIndex(notes: readonly VaultTreeNote[]): VaultLinkIndex {
  const byId = new Map<string, VaultTreeNote>();
  const byPath = new Map<string, VaultTreeNote>();
  const byStem = new Map<string, VaultTreeNote>();
  const byTitle = new Map<string, VaultTreeNote>();
  for (const note of notes) {
    if (note.path) byPath.set(note.path.toLowerCase(), note);
    if (note.id) byId.set(note.id.toLowerCase(), note);
    if (note.path) byStem.set(pathStem(note.path).toLowerCase(), note);
    if (note.title) byTitle.set(note.title.toLowerCase(), note);
  }
  return { byId, byPath, byStem, byTitle };
}

export function resolveWikilink(target: string, index: VaultLinkIndex): VaultTreeNote | null {
  const key = target.trim().toLowerCase();
  if (!key) return null;
  return index.byId.get(key) ?? index.byPath.get(key) ?? index.byStem.get(key) ?? index.byTitle.get(key) ?? null;
}

/** Notes whose forward `links` resolve to `path` — i.e. who links here. */
export function backlinksFor(path: string, notes: readonly VaultTreeNote[], index: VaultLinkIndex): VaultTreeNote[] {
  const target = index.byPath.get(path.toLowerCase());
  if (!target) return [];
  const seen = new Set<string>();
  const out: VaultTreeNote[] = [];
  for (const note of notes) {
    if (note.path === target.path) continue;
    for (const link of note.links) {
      const resolved = resolveWikilink(link, index);
      if (resolved && resolved.path === target.path && !seen.has(note.path)) {
        seen.add(note.path);
        out.push(note);
        break;
      }
    }
  }
  return out.sort((a, b) => a.title.localeCompare(b.title));
}
