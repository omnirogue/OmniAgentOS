/** Executive Decision Center (EDC) feature — the owner-scoped Decisions surface
 * behind the Inbox "Decisions" tab. One feature folder, one generic decide
 * mutation, built against the §10.2 wire contract. */
export * from "./types";
export {
  decideDecision,
  DecisionApiError,
  fetchDecisions,
  normalizeAvailableAction,
  normalizeDecision,
  normalizeRecommended,
  NO_RECOMMENDATION_SENTINEL,
} from "./api";
export { useDecisions, groupDecisions } from "./hooks";
export type { UseDecisions } from "./hooks";
export { DecisionCard } from "./DecisionCard";
export type { DecisionCardProps } from "./DecisionCard";
