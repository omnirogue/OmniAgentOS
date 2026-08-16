"use client";

import { Badge, EmptyState, ErrorState, Icon, Loading } from "@/design";
import type { ComputeEstateViewProps } from "./hooks";
import { runnerBadgeTone, runnerState } from "./tiles";
import type { CiRunner } from "./types";
import styles from "./compute.module.css";

const STATE_LABEL: Record<string, string> = { busy: "busy", idle: "idle", offline: "offline" };

/**
 * Runners strip — one row per estate CI runner: name, labels, and a
 * busy/idle/offline badge (busy = accent/in-progress "running" tone, never
 * danger; idle = neutral; offline = danger). When the `runners` envelope
 * itself is unavailable, a neutral state names the backend's reason —
 * never an empty list masquerading as "no runners".
 *
 * Takes `data`/`status`/`error`/`refresh` as props (UI-2 review fix) —
 * the estate-polling hook lives ONLY at page.tsx now.
 */
export function RunnersStrip({ data, status, error, refresh }: ComputeEstateViewProps) {
  if (status === "loading") {
    return (
      <div className={styles.runnersList} aria-busy="true">
        <Loading variant="skeleton" lines={3} label="Loading runners" />
      </div>
    );
  }

  if (status === "error" || !data) {
    return <ErrorState title="Runners unavailable" message={error ?? "Could not load estate runners"} onRetry={refresh} />;
  }

  if (!data.runners.available) {
    return (
      <EmptyState
        icon={<Icon name="alertTriangle" size={20} />}
        title="Runners unavailable"
        message={data.runners.reason ?? "No runner data reported"}
      />
    );
  }

  if (data.runners.runners.length === 0) {
    return <EmptyState icon={<Icon name="scan" size={20} />} title="No runners" message="No CI runners reported for this estate." />;
  }

  return (
    <ul className={styles.runnersList}>
      {data.runners.runners.map((runner, index) => (
        // `runner.name` is nullable on the wire (CROSS-1 principle applied
        // to runners too) — fall back to a stable positional key rather
        // than ever handing React a null key.
        <RunnerRow key={runner.name ?? `unnamed-runner-${index}`} runner={runner} />
      ))}
    </ul>
  );
}

function RunnerRow({ runner }: { runner: CiRunner }) {
  const state = runnerState(runner);
  return (
    <li className={styles.runnerRow}>
      <span className={styles.runnerName}>{runner.name ?? "Unnamed runner"}</span>
      {runner.labels.length > 0 ? <span className={styles.runnerLabels}>{runner.labels.join(", ")}</span> : null}
      <Badge tone={runnerBadgeTone(runner)}>{STATE_LABEL[state] ?? state}</Badge>
    </li>
  );
}
