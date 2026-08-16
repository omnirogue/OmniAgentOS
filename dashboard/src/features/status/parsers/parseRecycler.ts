export type RecyclerEntry = Record<string, unknown>;
export type RecyclerResult = RecyclerEntry | { raw: string };

/**
 * Last non-empty line of a `hang-recycler.log` tail — the log is JSONL,
 * one tick per line. Returns null when the tail carries no line at all
 * (e.g. a brand-new, empty log).
 */
export function lastNonEmptyLine(text: string): string | null {
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  return lines.length > 0 ? lines[lines.length - 1] : null;
}

/**
 * Parses one hang-recycler.log line as JSON. A malformed or truncated line
 * (a tail window can cut a JSONL line mid-write) must never throw or hide
 * the tick — it falls back to `{ raw: line }` so the operator can still
 * read what's there. `null` in (no line at all) also falls back to
 * `{ raw: "" }` rather than inventing a healthy default.
 */
export function parseRecyclerLine(line: string | null): RecyclerResult {
  if (line === null) return { raw: "" };
  try {
    const parsed: unknown = JSON.parse(line);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as RecyclerEntry;
    }
    return { raw: line };
  } catch {
    return { raw: line };
  }
}
