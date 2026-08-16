/** Fetch client for the P2 mounts browse API — see omniagentos/api/routes/mounts.py.
 *
 * Mirrors features/projects/api.ts's `authorizedRead=true` pattern: mounts
 * reads are token-gated on the FastAPI side exactly like project-file reads
 * (`_is_mounts_request` in omniagentos/api/main.py), so every request here
 * goes SAME-ORIGIN through the Next.js proxy (app/api/[...path]/route.ts,
 * `isAuthorizedReadPath`), which attaches the session token server-side — the
 * browser never holds it. Without that proxy match every request here 401s. */

import { fetchWithTimeout } from "../../lib/fetchTimeout";
import type { Mount, MountApiErrorBody, MountDirListing, MountsResponse } from "./types";

/** Extensions the P2 file endpoint streams as an image (mirrors
 * omniagentos/api/routes/projects.py's `_IMAGE_EXTENSIONS`, which mounts.py's
 * `_file_kind` reuses). Purely a client-side UX guess for which preview mode
 * to render before fetching — the server is the actual source of truth and
 * returns 415 for anything it disagrees with. */
const IMAGE_EXTENSIONS = new Set([".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"]);

export function looksLikeImage(name: string): boolean {
  const dot = name.lastIndexOf(".");
  if (dot < 0) return false;
  return IMAGE_EXTENSIONS.has(name.slice(dot).toLowerCase());
}

export class MountApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "MountApiError";
  }
}

function detailFrom(body: MountApiErrorBody, fallback: string): string {
  if (typeof body?.error === "object" && body.error?.message) return body.error.message;
  if (typeof body?.error === "string") return body.message ?? body.error;
  return body?.message ?? fallback;
}

async function req<T>(
  path: string,
  parseResponse: (response: Response) => Promise<T> = (response) => response.json() as Promise<T>,
): Promise<T> {
  const res = await fetchWithTimeout(path, { cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = detailFrom(await res.json(), detail);
    } catch {
      /* keep statusText */
    }
    throw new MountApiError(detail, res.status);
  }
  return parseResponse(res);
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== "" && v !== undefined);
  return entries.length
    ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}`
    : "";
}

export function fetchMounts(): Promise<Mount[]> {
  return req<MountsResponse>("/api/mounts").then((body) => body.mounts);
}

export function fetchMountDir(
  mountId: string,
  path: string,
  offset = 0,
  limit = 500,
): Promise<MountDirListing> {
  return req<MountDirListing>(
    `/api/mounts/${encodeURIComponent(mountId)}/dir${qs({ path, offset, limit })}`,
  );
}

/** A safe, server-validated file URL. `path` is mount-relative. */
export function mountFileUrl(mountId: string, path: string, download = false): string {
  return `/api/mounts/${encodeURIComponent(mountId)}/file${qs({ path, download: download || undefined })}`;
}

export function fetchMountFileText(mountId: string, path: string): Promise<string> {
  return req<string>(mountFileUrl(mountId, path), (response) => response.text());
}
