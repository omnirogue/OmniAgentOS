/** Wire types for the P2 machine-mounts browse surface — mirrors
 * omniagentos/api/routes/mounts.py exactly. See that module's docstring and
 * omniagentos/mounts.py's `Mount` dataclass for the field-level contract. */

/** Free-form on the backend (defaults to "local" if unset in configs/mounts.yaml),
 * but this machine's registry only ever declares these four. */
export type MountKind = "local" | "icloud" | "gdrive" | "dropbox" | string;

export type Mount = {
  id: string;
  label: string;
  /** Raw configured value from configs/mounts.yaml — may be `~`-relative
   * (e.g. "~/Work"). Human-readable; NOT valid as a create-project root_dir. */
  path: string;
  /** `path` after expanduser + realpath (backend `Mount.resolved`): an absolute
   * '/'-prefixed path. Insert THIS into a grant/root_dirs input — the raw `path`
   * 422s the create-project intake, which requires an absolute root. */
  resolved: string;
  kind: MountKind;
  cloud: boolean;
  grantable: boolean;
  read_only: boolean;
  exists: boolean;
  notes: string;
};

export type MountsResponse = { mounts: Mount[] };

export type MountDirEntry = {
  name: string;
  is_dir: boolean;
  size: number;
  /** Raw `os.stat().st_mtime` — epoch SECONDS as a float, NOT an ISO string
   * (unlike features/projects's `ProjectFile.modified`). Multiply by 1000
   * before handing to `Date`. */
  mtime: number;
};

export type MountDirListing = {
  mount_id: string;
  path: string;
  entries: MountDirEntry[];
  total: number;
  truncated: boolean;
};

/** Structured API error body shape from `omniagentos.api.services.ApiError`
 * (used by /api/mounts and /api/mounts/{id}/dir 404/409/403 responses). The
 * file-preview endpoint's 413/415 bodies use a flatter `{error, message}`
 * shape instead — `MountApiError` in ./client normalizes both. */
export type MountApiErrorBody = {
  error: { code?: string; message?: string; details?: unknown } | string;
  message?: string;
};

/** Display metadata for each known mount `kind` — used by MountGrid's badge. */
export const MOUNT_KIND_LABEL: Record<string, string> = {
  local: "Local",
  icloud: "iCloud",
  gdrive: "Google Drive",
  dropbox: "Dropbox",
};
