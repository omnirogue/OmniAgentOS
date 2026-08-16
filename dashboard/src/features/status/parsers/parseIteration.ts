export interface IterationTail {
  lastIterEnd: string | null;
  lastRc: number | null;
}

/**
 * Parses `───── <ISO> <role> iteration end rc=<n>` lines out of a loop-log
 * tail (e.g. `var/loopqueue/logs/implementer-loop.log`) and returns the
 * LAST match. Logs are append-only, so the last match inside a tail window
 * is the most recent iteration end for that role — no fs, no ordering
 * assumptions beyond "later in the string = later in time".
 *
 * Returns nulls when the tail has no iteration-end line for this role yet
 * (loop mid-iteration, or the tail window doesn't reach back far enough) —
 * a legitimate state, never guessed at.
 */
export function parseLastIterationEnd(text: string, role: string): IterationTail {
  const escapedRole = role.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`───── (\\S+) ${escapedRole} iteration end rc=(\\d+)`, "g");
  let match: RegExpExecArray | null;
  let last: RegExpExecArray | null = null;
  while ((match = pattern.exec(text)) !== null) {
    last = match;
  }
  if (!last) return { lastIterEnd: null, lastRc: null };
  return { lastIterEnd: last[1], lastRc: Number(last[2]) };
}
