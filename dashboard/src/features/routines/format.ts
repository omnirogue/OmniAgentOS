/** Small, pure display helpers shared by the Routines feature's pages/components. */
import type { BadgeTone } from "../../design";
import type { GateType, HardCapType, Routine, RoutineStatus, TriggerType } from "./types";

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function statusTone(status: RoutineStatus): BadgeTone {
  switch (status) {
    case "active":
      return "ok";
    case "disabled":
      return "neutral";
    case "auto_paused":
      return "danger";
    default:
      return "neutral";
  }
}

export function statusLabel(status: RoutineStatus): string {
  switch (status) {
    case "active":
      return "Active";
    case "disabled":
      return "Disabled";
    case "auto_paused":
      return "Auto-paused";
    default:
      return status;
  }
}

export function triggerSummary(type: TriggerType, config: Record<string, unknown>): string {
  if (type === "cron") return `cron ${String(config.cron ?? "?")}`;
  return `on ${String(config.event ?? "?")}`;
}

export function gateSummary(type: GateType, config: Record<string, unknown>): string {
  if (type === "metric_threshold") {
    return `${String(config.metric ?? "?")} ${String(config.operator ?? "?")} ${String(config.threshold ?? "?")}`;
  }
  if (type === "merge_candidate") {
    const candidateSha = String(config.candidate_sha ?? "?");
    const mergeBaseSha = config.merge_base_sha;
    // The API validates merge_base_sha exactly as strictly as candidate_sha
    // (a 40-char lowercase git SHA); a missing or empty value is a broken
    // row, not a healthy one with nothing to say. Render it distinctly so a
    // broken row never looks byte-identical to a healthy twin.
    const mergeBaseLabel =
      typeof mergeBaseSha === "string" && mergeBaseSha.length > 0
        ? mergeBaseSha.slice(0, 12)
        : "unknown";
    return `${String(config.command ?? "?")} @ ${candidateSha.slice(0, 12)} (base ${mergeBaseLabel})`;
  }
  return String(config.command ?? "?");
}

const GATE_LABELS: Record<GateType, string> = {
  exit_code: "Exit code",
  test_command: "Test command",
  metric_threshold: "Metric threshold",
  merge_candidate: "Merge candidate",
};

export function gateTypeLabel(type: GateType): string {
  return GATE_LABELS[type];
}

const HARD_CAP_LABELS: Record<HardCapType, string> = {
  max_iterations: "iterations",
  budget_usd: "budget",
  human_checkpoint: "checkpoint after",
};

export function hardCapSummary(type: HardCapType, value: number): string {
  switch (type) {
    case "max_iterations":
      return `max ${value} iterations`;
    case "budget_usd":
      return `$${value.toFixed(2)} budget`;
    case "human_checkpoint":
      return `human checkpoint every ${value} iterations`;
    default:
      return `${value} ${HARD_CAP_LABELS[type]}`;
  }
}

export function acceptanceRateLabel(routine: Routine): string {
  const neutral = routine.neutral_runs ?? 0;
  if (routine.acceptance_rate === null) {
    // Empty denominator. Say WHY, because "no runs yet" and "many runs, none
    // of them judgeable" are different problems: the second is a loop that has
    // been parking or idling every tick and needs a human to look at it.
    if (neutral > 0) return `No judged runs (${neutral} non-results)`;
    return "No runs yet";
  }
  // The fraction must be over the JUDGED runs, not every firing — otherwise
  // the percentage and the fraction beside it disagree the moment a routine
  // has any non-results.
  const judged = Math.max(routine.total_runs - neutral, 0);
  const suffix = neutral > 0 ? `, ${neutral} non-results` : "";
  return `${Math.round(routine.acceptance_rate * 100)}% (${routine.accepted_runs}/${judged}${suffix})`;
}

/**
 * ISSUE-8: `null` means the cost is UNKNOWN — at least one contributing run's
 * cost was never reported by its provider (omniagentos/scheduler/store.py's
 * `total_cost_usd`, nullable since migration 119) — never that it was free.
 * Rendering it as "$0.00" told an operator "this cost nothing" when the
 * honest claim is "we don't know what this cost"; the house doctrine is that
 * unknown is never a favourable value, applied here to money. A genuinely
 * exact zero (a routine whose every run reported a real, known cost that
 * summed to zero) still renders as "$0.00" — the two cases are distinguished
 * upstream, not conflated here.
 */
export function costPerAcceptedChangeLabel(routine: Routine): string {
  if (routine.cost_per_accepted_change === null) return "unknown";
  return `$${routine.cost_per_accepted_change.toFixed(2)}`;
}

export function acceptanceRateTone(routine: Routine): BadgeTone {
  if (routine.acceptance_rate === null) return "neutral";
  return routine.acceptance_rate < 0.5 ? "danger" : "ok";
}
