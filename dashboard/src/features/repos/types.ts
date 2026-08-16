export type RemoteRepo = {
  name: string;
  visibility: string;
  updatedAt: string;
  pushedAt: string | null;
  isArchived: boolean;
};

export type OwnerRepos = {
  owner: string;
  repos?: RemoteRepo[];
  error?: string;
};

export type LocalClone = {
  path: string;
  origin: string | null;
  branch: string | null;
  /** F03: number of dirty files from `git status --porcelain`, or `null`
   * when the command itself failed (timeout, ENOENT, not-a-repo, ...) --
   * `null` must never be treated as "clean"; see `dirtyError`. */
  dirty: number | null;
  /** Human-readable reason `dirty` is `null`; always `null` when `dirty` is
   * a real number. */
  dirtyError: string | null;
  matchedRemote: string | null;
};

export type Violation = LocalClone & {
  reason: "no-origin" | "dirty" | "no-origin+dirty" | "status-unknown";
};

export type ReposPayload = {
  owners: OwnerRepos[];
  locals: LocalClone[];
  violations: Violation[];
  truncated: boolean;
  scannedAt: string;
};
