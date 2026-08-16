/**
 * Folder logic for the chat sidebar (088 folder registry).
 *
 * Folders are free-text names chats carry in meta.folder; the server registry
 * adds identity on top — a named color token and a manual order. This module
 * holds the pure logic (palette tokens, response parsing, grouping, name
 * validation) so it is unit-testable without React or the design system.
 *
 * Colors are TOKEN NAMES only, never hex: each maps to a --ds-folder-<token>
 * CSS variable defined for dark and light in design/theme.css.
 */

import type { Chat } from "./chatApi";

export const FOLDER_COLORS = [
  "gray",
  "red",
  "orange",
  "yellow",
  "green",
  "teal",
  "blue",
  "violet",
] as const;

export type FolderColor = (typeof FOLDER_COLORS)[number];

/** One folder as served by GET /api/chats/folders (088 contract). */
export interface FolderInfo {
  name: string;
  color: FolderColor;
  chat_count: number;
}

export interface FolderGroup {
  name: string;
  color: FolderColor;
  chats: Chat[];
}

export function isFolderColor(value: unknown): value is FolderColor {
  return (
    typeof value === "string" &&
    (FOLDER_COLORS as readonly string[]).includes(value)
  );
}

/** Coerce any server/user value to a valid palette token (gray fallback). */
export function folderColorName(value: unknown): FolderColor {
  return isFolderColor(value) ? value : "gray";
}

const DEFAULT_FOLDER_COLORS = FOLDER_COLORS.slice(1);

/** Choose a stable non-gray color for a folder without a registry entry. */
export function getDefaultFolderColor(name: string): FolderColor {
  let hash = 2166136261;
  for (const character of name) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 16777619);
  }
  return DEFAULT_FOLDER_COLORS[(hash >>> 0) % DEFAULT_FOLDER_COLORS.length];
}

/** A chat's folder name (trimmed) or null when unfiled. */
export function chatFolder(chat: Pick<Chat, "meta">): string | null {
  const folder = chat.meta?.folder;
  if (typeof folder !== "string") return null;
  const trimmed = folder.trim();
  return trimmed ? trimmed : null;
}

/**
 * Parse GET /api/chats/folders. Tolerates the legacy `{folders: [string]}`
 * shape (mixed-deploy safety: legacy names become gray, count unknown → 0)
 * and returns [] for anything malformed.
 */
export function parseFoldersResponse(data: unknown): FolderInfo[] {
  if (!data || typeof data !== "object") return [];
  const folders = (data as { folders?: unknown }).folders;
  if (!Array.isArray(folders)) return [];
  const out: FolderInfo[] = [];
  for (const entry of folders) {
    if (typeof entry === "string") {
      if (entry.trim()) {
        out.push({ name: entry.trim(), color: "gray", chat_count: 0 });
      }
      continue;
    }
    if (!entry || typeof entry !== "object") continue;
    const name = (entry as { name?: unknown }).name;
    if (typeof name !== "string" || !name.trim()) continue;
    const count = (entry as { chat_count?: unknown }).chat_count;
    out.push({
      name: name.trim(),
      color: folderColorName((entry as { color?: unknown }).color),
      chat_count: typeof count === "number" && count >= 0 ? count : 0,
    });
  }
  return out;
}

/**
 * Group chats under folders. Registry order comes first (the server sorts by
 * manual position, then name); folders that exist only as free text on chats
 * follow alphabetically with the default color. Registered folders appear
 * even while empty (they are drop targets). Chats sort newest-first.
 */
export function buildFolderGroups(
  chats: Chat[],
  registry: FolderInfo[],
): FolderGroup[] {
  const byFolder = new Map<string, Chat[]>();
  for (const chat of chats) {
    const folder = chatFolder(chat);
    if (!folder) continue;
    byFolder.set(folder, [...(byFolder.get(folder) ?? []), chat]);
  }

  const groups: FolderGroup[] = [];
  const seen = new Set<string>();
  for (const info of registry) {
    if (seen.has(info.name)) continue;
    seen.add(info.name);
    groups.push({
      name: info.name,
      color: info.color,
      chats: byFolder.get(info.name) ?? [],
    });
  }
  const unregistered = [...byFolder.keys()]
    .filter((name) => !seen.has(name))
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  for (const name of unregistered) {
    groups.push({
      name,
      color: getDefaultFolderColor(name),
      chats: byFolder.get(name) ?? [],
    });
  }

  for (const group of groups) {
    group.chats.sort(
      (a, b) =>
        new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    );
  }
  return groups;
}

/**
 * Client-side mirror of the server's folder-name rules (ChatStore
 * _validate_folder_name). Returns an error message, or null when valid.
 */
export function folderNameError(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Folder name is required";
  if (trimmed.includes("/")) return "Folder name cannot contain “/”";
  if (trimmed.length > 100) return "Folder name is too long (max 100 characters)";
  return null;
}
