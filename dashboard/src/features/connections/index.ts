/** Connections feature — public surface. */

export { BrandIcon } from "./brandIcons";
export type { BrandIconId } from "./brandIcons";
export { DetailDialog } from "./DetailDialog";
export { Tile } from "./Tile";
export {
  fetchConnections,
  USE_CONNECTIONS_FIXTURES,
  ConnectionsApiError,
} from "./connectionsApi";
export {
  filterConnections,
  statusBadgeTone,
  statusSummaryLabel,
  flattenIntegrations,
} from "./logic";
export type {
  ConnectionCategory,
  ConnectionIntegration,
  ConnectionInstance,
  ConnectionStatus,
  ConnectionsResponse,
} from "./types";
