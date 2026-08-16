/**
 * Public API — fixture-driven shared axis primitives (P1-FE-AXES).
 */

export type {
  AxisKey,
  AxisResolution,
  AxisValue,
  AxisState,
  AxisRegistryOption,
  AxisRegistry,
} from "./types";

export { AXIS_ORDER, AXIS_LABELS } from "./types";

export {
  projectActiveContext,
  emptyAxes,
  type ActiveContextLike,
  type AxisProjectionOptions,
  type AxisProjectionResult,
} from "./axisProjection";

export {
  AxisChip,
  RESOLUTION_GLYPH,
  RESOLUTION_LABEL,
  axisValueText,
  axisAccessibleName,
  type AxisChipProps,
} from "./AxisChip";

export { AxisChipRow, type AxisChipRowProps } from "./AxisChipRow";

export {
  EMBEDDED_ACTIVE_CONTEXT,
  ACTIVE_CONTEXT_FIXTURES,
  getActiveContextById,
  fixtureEmptyAxes,
  fixtureFullAppliedAxes,
  fixtureUnknownWorkKind,
  projectFixtureById,
  FIXTURE_IDS,
  type ActiveContextFixture,
} from "./fixtures";
