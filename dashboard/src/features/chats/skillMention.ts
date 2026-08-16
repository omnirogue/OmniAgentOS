/**
 * @skill mention autocomplete for the chat composer.
 *
 * Extracts the current @mention query from the cursor position in the textarea,
 * matches against the skills tree, and provides the state needed for a popover
 * picker. Pure functions — no React, no side effects — so they're trivially
 * unit-testable.
 */

import type { SkillTreeEntry } from "./chatApi";

export interface MentionQuery {
  /** The text between `@` and the cursor (may be empty). */
  query: string;
  /** Byte offset of the `@` character in the draft. */
  start: number;
  /** Current cursor position (= end of the query). */
  end: number;
}

/**
 * Scan backwards from `cursorPos` looking for `@`. Stops at whitespace,
 * newlines, or the start of the text. Returns `null` when no valid mention
 * prefix is found (e.g. the `@` is preceded by a non-whitespace char, which
 * means it's part of an email-like token, not a mention).
 */
export function extractMentionQuery(
  text: string,
  cursorPos: number,
): MentionQuery | null {
  if (cursorPos <= 0 || cursorPos > text.length) return null;

  let i = cursorPos - 1;
  while (i >= 0) {
    const ch = text[i]!;
    if (ch === "@") {
      if (i > 0) {
        const prev = text[i - 1]!;
        if (!/\s/.test(prev)) return null;
      }
      return { query: text.slice(i + 1, cursorPos), start: i, end: cursorPos };
    }
    if (/\s/.test(ch)) return null;
    i -= 1;
  }
  return null;
}

/**
 * Filter skills against the user's query. Empty query returns all skills;
 * otherwise matches name or description (case-insensitive substring).
 */
export function searchSkills(
  skills: SkillTreeEntry[],
  query: string,
): SkillTreeEntry[] {
  if (!query.trim()) return skills;
  const lower = query.toLowerCase();
  return skills.filter(
    (s) =>
      s.name.toLowerCase().includes(lower) ||
      (s.description ?? "").toLowerCase().includes(lower),
  );
}

/**
 * Replace the @mention at `[start, end)` with the chosen skill's canonical
 * reference `{name}` + a trailing space, and return the new draft + the cursor
 * position after the insertion.
 */
export function applyMention(
  text: string,
  mention: MentionQuery,
  skill: SkillTreeEntry,
): { text: string; cursor: number } {
  const before = text.slice(0, mention.start);
  const after = text.slice(mention.end);
  const inserted = `@{${skill.name}} `;
  const newText = `${before}${inserted}${after}`;
  return { text: newText, cursor: before.length + inserted.length };
}
