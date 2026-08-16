/** Inbox feature — the tabbed shell behind /approvals (Approvals · Alerts ·
 * Suggestions · Briefings). Alerts/Suggestions/Briefings data + mutations are
 * NOT duplicated here: this feature only composes the existing
 * `features/steward` hooks/components into tab content, plus owns the
 * Approvals-specific feed (extracted from the former standalone page). */
export { useApprovalsFeed } from "./useApprovalsFeed";
export type { ApprovalsFeed } from "./useApprovalsFeed";
export { ApprovalsTab } from "./ApprovalsTab";
export { AlertsTab } from "./AlertsTab";
export { SuggestionsTab } from "./SuggestionsTab";
export { BriefingsTab } from "./BriefingsTab";
export { DecisionsTab } from "./DecisionsTab";
export { TabLabel } from "./TabLabel";
