/** W2 projects feature — provider, switcher, API client and wire types. */
export * from "./types";
export {
  ProjectApiError,
  createProject,
  fetchProject,
  fetchProjectActivity,
  fetchProjectApprovals,
  fetchProjectRuns,
  fetchProjectTasks,
  fetchProjects,
  patchProject,
} from "./api";
export { ProjectProvider, useProjectContext } from "./context";
export { ProjectSwitcher } from "./ProjectSwitcher";
export { CompanyPicker } from "./CompanyPicker";

/* W3 project hierarchy — tree and conversation. Activity/logs are served by
 * api.ts's fetchProjectActivity (GET /api/projects/{id}/activity, above) —
 * hierarchy.ts's own runs-derived activity feed is intentionally NOT
 * re-exported here to avoid a duplicate `fetchProjectActivity` export; the
 * Logs/Activity tab (HierarchyViews.ActivityPanel) reads the richer shape
 * from ./api directly. */
export {
  fetchProjectTree,
  fetchConversation,
  postConversationMessage,
  stateTone,
  nodeProgress,
} from "./hierarchy";
export type {
  ProjectTreeNode,
  TreeTask,
  ConversationMessage,
  ConversationScope,
  ConversationRole,
  PostMessageInput,
} from "./hierarchy";
export { projectColor, projectColorStyle } from "./colors";
export type { ProjectColor } from "./colors";
export {
  useProjectTree,
  useConversation,
  findNode,
  findTaskInTree,
} from "./hierarchyHooks";
export type { DataSource, TreeState, ConversationState } from "./hierarchyHooks";
