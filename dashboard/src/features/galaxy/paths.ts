/**
 * Vault path safety helpers.
 *
 * REWARD-HACKING / TRAVERSAL GUARD: the client must never construct a `..` traversal
 * request, even though the API confines reads to the vault dir server-side. Every path
 * that reaches fetchVaultNote() (api.ts) is validated with isSafeVaultPath first — paths
 * only ever originate from a trusted API response (tree/search results) or a URL the
 * user typed, and the latter is validated before use rather than trusted blindly.
 */

/** True if `path` is a plain, relative, forward-slash vault path with no traversal. */
export function isSafeVaultPath(path: string): boolean {
  if (!path) return false;
  if (path.includes("..")) return false;
  if (path.startsWith("/") || path.startsWith("\\")) return false;
  if (path.includes("\\")) return false;
  if (/^[a-zA-Z]:/.test(path)) return false; // reject drive-letter absolute paths
  if (path.includes("\0")) return false;
  return true;
}

/** Build the in-app href for a vault note, percent-encoding each path segment. */
export function vaultHref(path: string): string {
  const safe = isSafeVaultPath(path) ? path : "";
  return `/vault/${safe
    .split("/")
    .filter(Boolean)
    .map((segment) => encodeURIComponent(segment))
    .join("/")}`;
}

/** Reconstruct a vault-relative path from a Next.js catch-all `params.path` array. */
export function pathFromSegments(segments: string[] | undefined): string {
  return (segments ?? []).join("/");
}

/** Filename without its extension, lower-cased — used for Obsidian-style link matching. */
export function pathStem(path: string): string {
  const file = path.split("/").pop() ?? path;
  return file.replace(/\.[^./]+$/, "");
}
