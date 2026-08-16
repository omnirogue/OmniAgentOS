"use client";

import { usePulseSeries } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeReliabilityTile } from "./tiles";

export function ReliabilityPulseTile() {
  const { series, status, error, refresh } = usePulseSeries("reliability.score", 30);
  const data = shapeReliabilityTile(series);
  return <PulseTile data={data} status={status} error={error} onRetry={refresh} />;
}
