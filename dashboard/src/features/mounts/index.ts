/** Machine-mounts browse feature (P6) — client, hooks, wire types and
 * components for the P2 mounts API (omniagentos/api/routes/mounts.py). */
export * from "./types";
export {
  MountApiError,
  fetchMounts,
  fetchMountDir,
  fetchMountFileText,
  mountFileUrl,
  looksLikeImage,
} from "./client";
export { useMounts, useMountDir } from "./hooks";
export type { MountsState, MountDirState } from "./hooks";
export { MountGrid, mountKindLabel } from "./components/MountGrid";
export { MountBrowser } from "./components/MountBrowser";
