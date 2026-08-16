/** Returns a case-insensitive `owner/name` key for a GitHub origin. */
export function normalizeGithubOrigin(origin: string | null | undefined): string | null {
  if (!origin) return null;

  const value = origin.trim().replace(/\/+$/, "").replace(/\.git$/i, "");
  const sshMatch = value.match(/^(?:git@)?github\.com:([^/]+)\/([^/]+)$/i);
  if (sshMatch) return `${sshMatch[1]}/${sshMatch[2]}`.toLowerCase();

  try {
    const url = new URL(value);
    if (url.hostname.toLowerCase() !== "github.com") return null;
    const [owner, name, ...rest] = url.pathname.split("/").filter(Boolean);
    if (!owner || !name || rest.length > 0) return null;
    return `${owner}/${name}`.toLowerCase();
  } catch {
    return null;
  }
}

/** Tests a local remote URL against a GitHub owner/repository pair. */
export function originMatchesGithubRepo(origin: string | null | undefined, owner: string, name: string): boolean {
  return normalizeGithubOrigin(origin) === `${owner}/${name}`.toLowerCase();
}

/** Replaces only an exact home-directory prefix, avoiding partial-name matches. */
export function relativizeHomePath(path: string, home: string): string {
  const normalizedHome = home.replace(/\/$/, "");
  if (path === normalizedHome) return "~";
  if (path.startsWith(`${normalizedHome}/`)) return `~${path.slice(normalizedHome.length)}`;
  return path;
}
