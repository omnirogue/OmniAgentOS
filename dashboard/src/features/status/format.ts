/** Small, pure display helpers for the Status page — same spirit as
 * features/accounts/format.ts. */

/** Coarse "Ns/Nm/Nh ago" relative-time cue. Never throws on a bad/absent
 * timestamp — callers pass the raw string through untouched instead. */
export function relativeFromIso(iso: string, nowMs: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const sec = Math.max(0, Math.round((nowMs - then) / 1000));
  if (sec < 10) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

export function roleLabel(role: "implementer" | "reviewer" | "planning"): string {
  switch (role) {
    case "implementer":
      return "Implementer";
    case "reviewer":
      return "Reviewer";
    case "planning":
      return "Planning";
    default:
      return role;
  }
}
