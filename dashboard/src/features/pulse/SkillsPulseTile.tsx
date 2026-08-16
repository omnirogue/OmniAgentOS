"use client";

import { usePulseSeries } from "./hooks";
import { PulseTile } from "./PulseTile";
import { shapeSkillsTile } from "./tiles";

/**
 * Observatory Skills tile — total skills + versions-this-week sparkline.
 * Deep-links to /skills for the full tree.
 */
export function SkillsPulseTile() {
  const total = usePulseSeries("skills.total", 30);
  const versions = usePulseSeries("skills.versions", 30);
  const data = shapeSkillsTile(total.series, versions.series);
  // Status is whichever of the two is worse — loading if either is loading,
  // error if either errored, ready otherwise.
  const status = total.status === "loading" || versions.status === "loading"
    ? "loading"
    : total.status === "error" || versions.status === "error"
    ? "error"
    : total.status === "empty" && versions.status === "empty"
    ? "empty"
    : "ready";
  const error = total.error ?? versions.error;

  return (
    <PulseTile
      data={data}
      status={status}
      error={error}
      onRetry={() => {
        total.refresh();
        versions.refresh();
      }}
    />
  );
}
