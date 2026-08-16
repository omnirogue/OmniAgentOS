export interface GateTail {
  trainLines: string[];
  lastAt: string | null;
}

const TRAIN_LINE_RE = /train\/\S+ @ \S+ still gating|no trains assembled|gate slots full/;
const AT_RE = /"at": "([^"]+)"/g;

/**
 * Pulls the operator-relevant lines out of a `var/log/gate-loop.log` tail:
 * up to the last 5 lines that mention a train still gating, no trains
 * assembled, or gate slots full — plus the last `"at": "<ISO>"` stamp seen,
 * which is the gate loop's own last tick (present even on a tick with
 * nothing to report).
 */
export function parseGateTail(text: string): GateTail {
  const matchingLines = text.split("\n").filter((line) => TRAIN_LINE_RE.test(line));
  const trainLines = matchingLines.slice(-5).map((line) => line.trim());

  let lastAt: string | null = null;
  let match: RegExpExecArray | null;
  while ((match = AT_RE.exec(text)) !== null) {
    lastAt = match[1];
  }

  return { trainLines, lastAt };
}
