"use client";

import { Badge, Section, StatusDot } from "@/design";
import { relativeFromIso, roleLabel } from "../format";
import type { LoopRoleStatus, LoopsStatus, Result } from "../types";
import styles from "../status.module.css";

const ROLES = ["implementer", "reviewer", "planning"] as const;
type Role = (typeof ROLES)[number];

function LoopChip({ role, result, now }: { role: Role; result: Result<LoopRoleStatus>; now: number }) {
  if (!result.ok) {
    return (
      <div className={styles.chip}>
        <div className={styles.chipHead}>
          <StatusDot state="danger" label="Error" />
          <span className={styles.chipName}>{roleLabel(role)}</span>
          <Badge tone="danger">error</Badge>
        </div>
        <p className={styles.chipDetail}>{result.error}</p>
      </div>
    );
  }

  const { alive, lastIterEnd, lastRc } = result.data;
  const rcTone = lastRc === null ? "neutral" : lastRc === 0 ? "ok" : "danger";

  return (
    <div className={styles.chip}>
      <div className={styles.chipHead}>
        <StatusDot state={alive ? "ok" : "danger"} label={alive ? "Up" : "Down"} />
        <span className={styles.chipName}>{roleLabel(role)}</span>
        <Badge tone={alive ? "ok" : "danger"}>{alive ? "UP" : "DOWN"}</Badge>
      </div>
      <p className={styles.chipDetail}>
        {lastIterEnd ? `last end ${relativeFromIso(lastIterEnd, now)} (${lastIterEnd})` : "no iteration end seen"}
      </p>
      {lastRc !== null ? (
        <p className={styles.chipRc}>
          rc=<Badge tone={rcTone}>{lastRc}</Badge>
        </p>
      ) : null}
    </div>
  );
}

/**
 * Three tmux-liveness chips (implementer/reviewer/planning): green UP / red
 * DOWN, plus each role's last logged iteration end and exit code straight
 * from its own log tail. A role whose sub-fetch failed renders its own
 * error chip — the other two roles still render normally.
 */
export function LoopsSection({ loops, now }: { loops: LoopsStatus; now: number }) {
  return (
    <Section eyebrow="Daemons" title="Loops">
      <div className={styles.chipRow}>
        {ROLES.map((role) => (
          <LoopChip key={role} role={role} result={loops[role]} now={now} />
        ))}
      </div>
    </Section>
  );
}
