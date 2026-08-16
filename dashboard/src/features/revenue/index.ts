/** Revenue feature public surface — yesterday's revenue/ad spend/gross
 * contribution by vertical, source and Meta campaign, plus the data
 * quality & coverage honesty panel. */
export * from "./contracts";
export * from "./api";
export * from "./fixtures";
export * from "./hooks";
export * from "./format";
export { KpiTiles } from "./components/KpiTiles";
export type { KpiTilesProps } from "./components/KpiTiles";
export { DataQualityPanel } from "./components/DataQualityPanel";
export type { DataQualityPanelProps } from "./components/DataQualityPanel";
export { VerticalTable } from "./components/VerticalTable";
export type { VerticalTableProps } from "./components/VerticalTable";
export { SourceTable } from "./components/SourceTable";
export type { SourceTableProps } from "./components/SourceTable";
export { CampaignTable } from "./components/CampaignTable";
export type { CampaignTableProps } from "./components/CampaignTable";
