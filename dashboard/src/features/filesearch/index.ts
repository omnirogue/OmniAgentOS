/** Machine-wide file-search feature — the search-first surface on /files that
 * gives the operator the same file-finding reach the agents have
 * (omniagentos/filesearch + api/routes/filesearch.py). Client, hooks, wire
 * types and the search panel component. */
export * from "./types";
export {
  FileSearchApiError,
  isNotDeployed,
  searchFiles,
  revealFile,
  type FileSearchParams,
} from "./client";
export { useFileSearch, ALL } from "./hooks";
export type { FileSearchState, RootFilter, CategoryFilter } from "./hooks";
export { FileSearchPanel } from "./components/FileSearchPanel";
