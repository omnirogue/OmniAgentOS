/** Observatory /compute (Estate Compute) module barrel. */

export { ComputeSummaryRow } from "./ComputeSummaryRow";
export { MachineGrid } from "./MachineGrid";
export { RunnersStrip } from "./RunnersStrip";

export { useComputeEstate, COMPUTE_POLL_MS } from "./hooks";
export type { AsyncStatus, ComputeEstateViewProps, UseComputeEstateResult } from "./hooks";

export { fetchComputeEstate } from "./client";

export {
  deriveMachineCards,
  deriveSummaryTiles,
  fmtCount,
  formatLoadRatio,
  isDraining,
  isStale,
  lastSeenCaption,
  loadRatioTone,
  machineCardFromLocal,
  machineCardFromPool,
  runnerBadgeTone,
  runnerState,
  shapeFreeSlots,
  shapeLocalLoad,
  shapeQueueDepth,
  shapeRunnersSummary,
  STALE_AGE_S,
} from "./tiles";
export type { MachineCardView, RunnerState, SummaryTile, Tone } from "./tiles";

export type {
  CiRunner,
  ComputeEstate,
  ComputeLocal,
  ComputeMachine,
  ComputePool,
  ComputePoolCapacity,
  ComputePoolDepth,
  ComputeRefusals24h,
  ComputeRunners,
} from "./types";
