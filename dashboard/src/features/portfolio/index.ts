/**
 * Portfolio data layer. The standalone /portfolio screen (PortfolioScreen,
 * AttentionQueue, BudgetMeter, PortfolioTree) was retired in the dashboard
 * prune; what survives here is the shared portfolio API + formatting that the
 * board's NeedsResponseQueue and the command palette's #project scope depend on.
 */

export type {
  PortfolioProject,
  PortfolioResponse,
  PortfolioState,
  PortfolioTreeNode,
} from "./types";
export { fetchPortfolio, PortfolioApiError } from "./api";
export {
  buildForest,
  fuzzyMatchProject,
  flattenVisible,
  defaultExpanded,
} from "./tree";
export {
  compactRelative,
  formatMoney,
  parentBreadcrumb,
  QUEUE_BANDS,
  stateStripeClass,
  stateToDot,
  stateToBadgeTone,
} from "./format";
