"use client";

import { usePulseSeries } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeLoopsTile } from "./tiles";

export function LoopsPulseTile() {
  const fires = usePulseSeries("loops.fires", 30);
  const acceptance = usePulseSeries("loops.acceptance", 30);
  const data = shapeLoopsTile(fires.series, acceptance.series);
  const status = fires.status === "loading" || acceptance.status === "loading"
    ? "loading"
    : fires.status === "error" || acceptance.status === "error"
    ? "error"
    : fires.status === "empty" && acceptance.status === "empty"
    ? "empty"
    : "ready";
  return (
    <PulseTile
      data={data}
      status={status}
      error={fires.error ?? acceptance.error}
      onRetry={() => {
        fires.refresh();
        acceptance.refresh();
      }}
    />
  );
}
