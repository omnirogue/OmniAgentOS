/** Observatory pulse module barrel — tile components + cockpit pulse widgets. */

export { PulseTile } from "./PulseTile";
export type { PulseTileProps } from "./PulseTile";

export { SkillsPulseTile } from "./SkillsPulseTile";
export { ImprovementsPulseTile } from "./ImprovementsPulseTile";
export { LoopsPulseTile } from "./LoopsPulseTile";
export { CapabilityPulseTile } from "./CapabilityPulseTile";
export { MemoryPulseTile } from "./MemoryPulseTile";
export { ReliabilityPulseTile } from "./ReliabilityPulseTile";

export { usePulseSeries, useSystemDelta, useActiveRoutines, useCapabilityPulse, totalDelta } from "./hooks";
export type { AsyncStatus, UseSeriesResult, UseDeltaResult, UseCapabilityResult } from "./hooks";

export {
  fetchPulseSeries,
  fetchPulseMetrics,
  fetchSystemDelta,
  fetchActiveRoutines,
  fetchCapabilitySnapshot,
} from "./client";
export type { ActiveRoutineRow } from "./client";

export type {
  MetricName,
  PulsePoint,
  SeriesResponse,
  MetricsResponse,
  DeltaResponse,
  PulseTileData,
  CapabilitySnapshot,
} from "./types";
export { METRIC_NAMES } from "./types";

export {
  formatValue,
  latestValue,
  deltaFromSeries,
  trendFromDelta,
  shapeSkillsTile,
  shapeImprovementsTile,
  shapeLoopsTile,
  shapeCapabilityTile,
  shapeMemoryTile,
  shapeReliabilityTile,
} from "./tiles";
