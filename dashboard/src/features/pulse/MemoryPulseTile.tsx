"use client";

import { usePulseSeries } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeMemoryTile } from "./tiles";

export function MemoryPulseTile() {
  const { series, status, error, refresh } = usePulseSeries("memory.facts", 30);
  const data = shapeMemoryTile(series);
  return <PulseTile data={data} status={status} error={error} onRetry={refresh} />;
}
