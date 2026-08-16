"use client";

import { usePulseSeries } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeImprovementsTile } from "./tiles";

export function ImprovementsPulseTile() {
  const { series, status, error, refresh } = usePulseSeries("improvements.applied", 30);
  const data = shapeImprovementsTile(series);
  return <PulseTile data={data} status={status} error={error} onRetry={refresh} />;
}
