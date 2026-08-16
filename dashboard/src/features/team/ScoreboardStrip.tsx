"use client";

/**
 * ScoreboardStrip (P5) — per-person X× + % to 10× tiles + a team tile.
 *
 * Network/auth failures remain non-blocking: `useTeamScoreboard` settles to
 * `"unavailable"` and the Team page continues without this optional strip.
 */

import { Card } from "@/design";
import { useTeamScoreboard } from "./hooks";
import { NAMED_EMPLOYEE_IDS, employeeName } from "./types";
import type { TeamScoreboardStat } from "./types";
import styles from "./team.module.css";

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function formatMultiplier(stat: TeamScoreboardStat | undefined): string | null {
  if (!stat || !isFiniteNumber(stat.production_x)) return null;
  return `${stat.production_x.toFixed(1)}×`;
}

function formatPct(stat: TeamScoreboardStat | undefined): string | null {
  if (!stat || !isFiniteNumber(stat.pct_to_10x)) return null;
  return `${Math.round(stat.pct_to_10x)}% to 10×`;
}

function hasUsableStat(stat: TeamScoreboardStat | undefined): boolean {
  return formatMultiplier(stat) !== null || formatPct(stat) !== null;
}

function ScoreTile({
  label,
  stat,
  team = false,
}: {
  label: string;
  stat: TeamScoreboardStat | undefined;
  team?: boolean;
}) {
  return (
    <Card padding="sm" className={team ? `${styles.scoreTile} ${styles.scoreTileTeam}` : styles.scoreTile}>
      <div className="ds-stat__label">{label}</div>
      <div className="ds-stat__value">{formatMultiplier(stat) ?? "—"}</div>
      {formatPct(stat) ? <div className="ds-stat__hint">{formatPct(stat)}</div> : null}
    </Card>
  );
}

export function ScoreboardStrip() {
  const { state, value } = useTeamScoreboard();
  if (state !== "ready" || !value) return null;
  const people = Array.isArray(value.people) ? value.people : [];
  const employees = new Map(people.map((person) => [person.employee_id, person]));
  // Skip a person entirely when their stat carries no recognizable number —
  // no empty tile with just a name and a dash in it.
  const employeeIds = NAMED_EMPLOYEE_IDS.filter((id) => hasUsableStat(employees.get(id)));
  const showTeam = hasUsableStat(value.team);
  if (employeeIds.length === 0 && !showTeam) return null;

  return (
    <section className={styles.scoreboard} aria-label="Team scoreboard">
      {employeeIds.map((id) => (
        <ScoreTile key={id} label={employeeName(id) ?? id} stat={employees.get(id)} />
      ))}
      {showTeam ? <ScoreTile label="Team" stat={value.team} team /> : null}
    </section>
  );
}
