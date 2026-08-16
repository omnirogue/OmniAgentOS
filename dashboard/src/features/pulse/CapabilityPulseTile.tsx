"use client";

import { useCapabilityPulse } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeCapabilityTile } from "./tiles";

/**
 * Observatory Capability tile — ELO leader + ranked configs + tournaments run,
 * read live from the lab API (FINAL-PLAN §6 tile 4). Deep-links to
 * /lab for the full table.
 */
export function CapabilityPulseTile() {
  const { snapshot, status, error, refresh } = useCapabilityPulse();
  const data = shapeCapabilityTile(snapshot);
  return <PulseTile data={data} status={status} error={error} onRetry={refresh} />;
}
