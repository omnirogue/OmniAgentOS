const MAX_LEN = 300;
const MAX_ITEMS = 5;

/**
 * Last up-to-5 "- " bullet lines from a `var/loopqueue/ALERTS.md` tail,
 * each capped at 300 chars so a single long alert can't push the whole
 * card off-screen. Lines that don't start with "- " (headers, blank lines,
 * a truncated first line at the start of the tail window) are dropped.
 */
export function parseAlertsTail(text: string): string[] {
  const bullets = text.split("\n").filter((line) => line.startsWith("- "));
  return bullets
    .slice(-MAX_ITEMS)
    .map((line) => (line.length > MAX_LEN ? `${line.slice(0, MAX_LEN)}…` : line));
}
