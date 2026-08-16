"use client";

import { Badge, type BadgeTone } from "@/design";
import type {
  EventSeverity,
  EventStatus,
  HealthState,
  JudgeVerdict,
} from "@/lib/reliabilityContracts";
import styles from "./reliability.module.css";

export function RiskBadge({ level }: { level: number }) {
  const clamped = Math.min(4, Math.max(1, Math.round(level) || 1));
  return (
    <span className={styles.riskBadge} data-level={clamped} title={`Risk L${clamped}`}>
      L{clamped}
    </span>
  );
}

export function HealthStateBadge({ state }: { state: HealthState }) {
  return (
    <span className={styles.healthBadge} data-state={state}>
      {state}
    </span>
  );
}

const severityTone: Record<EventSeverity, BadgeTone> = {
  info: "neutral",
  warning: "warn",
  critical: "danger",
};

export function SeverityBadge({ severity }: { severity: EventSeverity | string }) {
  const key = (severity in severityTone ? severity : "info") as EventSeverity;
  return <Badge tone={severityTone[key]}>{severity}</Badge>;
}

const statusTone: Record<string, BadgeTone> = {
  open: "warn",
  recovering: "running",
  recovered: "ok",
  proposed: "validating",
  resolved: "completed",
  ignored: "cancelled",
  awaiting_human: "awaiting",
  approved: "promote",
  rejected: "reject",
  applied: "ok",
  monitoring: "running",
  rolled_back: "danger",
  panel_blocked: "human_review",
  pending: "awaiting",
  designing: "validating",
  awaiting_approval: "awaiting",
  created: "ok",
  failed: "failed",
  queued: "queued",
  running: "running",
  completed: "completed",
};

export function StatusBadge({ status }: { status: EventStatus | string }) {
  const tone = statusTone[status] ?? "neutral";
  return <Badge tone={tone}>{status.replaceAll("_", " ")}</Badge>;
}

export function VerdictBadge({ verdict }: { verdict: JudgeVerdict | string }) {
  const tone: BadgeTone =
    verdict === "approve"
      ? "promote"
      : verdict === "reject"
        ? "reject"
        : verdict === "approve_with_conditions"
          ? "challenger"
          : "human_review";
  return <Badge tone={tone}>{verdict.replaceAll("_", " ")}</Badge>;
}

export function ClassBadge({ label }: { label: string }) {
  return <Badge tone="neutral">{label}</Badge>;
}
