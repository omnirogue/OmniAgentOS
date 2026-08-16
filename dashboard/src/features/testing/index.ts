/** Observatory /testing (Test Observability) module barrel. */

export { TestObsStatRow } from "./TestObsStatRow";
export { CategoryTrendPanel } from "./CategoryTrendPanel";
export { WeakSpotsPanel } from "./WeakSpotsPanel";

export { useTestObsOverview, useTestObsSeries, useWeakspots } from "./hooks";
export type {
  AsyncStatus,
  UseTestObsOverviewResult,
  UseTestObsSeriesResult,
  UseWeakspotsResult,
} from "./hooks";

export {
  fetchTestObsOverview,
  fetchTestObsSeries,
  fetchTestObsWeakspots,
} from "./client";

export {
  allSourcesAvailable,
  deltaFromPoints,
  findSeriesLine,
  groupWeakspotsByCategory,
  latestPoint,
  northstarTone,
  seriesIsEmpty,
  seriesValues,
  shapeStat,
  toLineSeries,
  unavailableSources,
  weakspotTone,
} from "./tiles";
export type { StatTone, TestObsStatItem } from "./tiles";

export type {
  DiagnosticsOverview,
  LatestLaneSummary,
  MemoryOverview,
  NorthstarOverview,
  SuiteOverview,
  TestObsCategory,
  TestObsOverview,
  TestObsPoint,
  TestObsSeriesLine,
  TestObsSeriesResponse,
  WeakspotItem,
  WeakspotSourceCategory,
  WeakspotSourceInfo,
  WeakspotSources,
  WeakspotsResponse,
  WeakspotStatus,
} from "./types";
export { TESTOBS_CATEGORIES, WEAKSPOT_SOURCE_CATEGORIES } from "./types";
