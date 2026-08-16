/** Small, pure display helpers shared by the Steward feature's components. */
import type { BadgeTone, PillTone, StatusDotState } from "../../design";
import { RISK_CLASSES, type Alert, type AlertState, type KbStatus, type RiskClass, type SuggestionState } from "./types";

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

export function truncate(value: string | null | undefined, limit = 140): string {
  if (!value) return "-";
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

/** Same domain/tone mapping as app/runs/[id]/page.tsx's actionTones — risk_class shares
 * the ActionClass vocabulary (read_only .. irreversible). */
export function riskTone(risk: RiskClass): BadgeTone {
  switch (risk) {
    case "read_only":
      return "ok";
    case "sandboxed_creation":
      return "challenger";
    case "internal_reversible":
      return "promote";
    case "external_reversible":
      return "warn";
    case "consequential":
    case "irreversible":
      return "danger";
    default:
      return "danger";
  }
}

/** proposed_plan.action_class is a free-form string on the wire (defensively
 * normalized, not narrowed to RiskClass) but shares the same ActionClass
 * vocabulary as risk_class — reuse riskTone's mapping when it's a recognized
 * value, otherwise fail closed to a danger tone rather than a neutral one
 * (defense in depth — same reasoning as riskTone's unrecognized-value case). */
export function actionClassTone(actionClass: string): BadgeTone {
  return (RISK_CLASSES as readonly string[]).includes(actionClass)
    ? riskTone(actionClass as RiskClass)
    : "danger";
}

/** DES-002: confirmation friction scaled to risk_class. A reflex click must
 * not be able to approve a consequential action, so the friction grows with
 * the risk tier instead of being a flat "type your name and click Approve"
 * for every class:
 *   - "none"         benign (read_only / sandboxed_creation) — a single Approve is fine.
 *   - "checkbox"     elevated-but-bounded (internal_reversible / external_reversible) —
 *                    require an explicit acknowledgement checkbox.
 *   - "type_confirm" consequential / irreversible / unrecognized — require typing the
 *                    suggestion's title verbatim. */
export type ConfirmationFriction = "none" | "checkbox" | "type_confirm";

export function confirmationFriction(risk: RiskClass): ConfirmationFriction {
  switch (risk) {
    case "read_only":
    case "sandboxed_creation":
      return "none";
    case "internal_reversible":
    case "external_reversible":
      return "checkbox";
    case "consequential":
    case "irreversible":
      return "type_confirm";
    default:
      return "type_confirm";
  }
}

export function isElevatedRisk(risk: RiskClass): boolean {
  return confirmationFriction(risk) !== "none";
}

/** Human-readable warning shown in the danger banner at the point of approval —
 * null for benign classes (no banner shown). */
export function riskClassWarning(risk: RiskClass): string | null {
  switch (risk) {
    case "read_only":
    case "sandboxed_creation":
      return null;
    case "internal_reversible":
      return "This changes real internal state (e.g. Steward-managed records or config). It is reversible, but will take effect immediately once approved.";
    case "external_reversible":
      return "This takes an externally visible action (e.g. a message, ad-platform change, or customer-facing edit). It can be reversed, but someone outside this system may see it before you can undo it.";
    case "consequential":
      return "This is a consequential action — it may not be easily reversible and can have real external effects (money, infrastructure, or customer-facing changes). Read the plan below carefully before approving.";
    case "irreversible":
      return "This action is irreversible and cannot be undone once approved. Verify every detail of the plan before proceeding.";
    default:
      return "This action has an unrecognized risk class and is being treated as irreversible. Do not approve it until its risk and effects have been verified.";
  }
}

export function suggestionStateTone(state: SuggestionState): BadgeTone {
  switch (state) {
    case "approved":
      return "ok";
    case "dismissed":
      return "neutral";
    default:
      return "awaiting";
  }
}

export function alertStateTone(state: AlertState): BadgeTone {
  return state === "acked" ? "ok" : "warn";
}

/** severity is a free-form string set by alert rules (critical/high/triage/…), not a
 * frozen enum — map defensively and always pass an explicit `label` override so the
 * dot's aria-label shows the real severity rather than StatusDot's generic default.
 * DES-003: critical/high/medium must be visually distinct (not just critical-vs-not),
 * so each gets its own StatusDot color — this dot is a secondary cue only; the
 * primary, color-blind-safe signal is the text label from severityLabel()/the Badge
 * rendered alongside it (severity must never be conveyed by color alone). */
export function severityDotState(severity: string): StatusDotState {
  switch (severity.toLowerCase()) {
    case "critical":
      return "danger";
    case "high":
      return "warn";
    case "medium":
      return "validating";
    case "low":
      return "ok";
    case "triage":
      return "awaiting";
    default:
      return "queued";
  }
}

/** DES-003: the severity TEXT label an operator reads regardless of color vision —
 * always rendered next to (never instead of) the colored StatusDot/Badge. Unknown/
 * free-form severities still get a readable uppercase label rather than disappearing. */
export function severityLabel(severity: string): string {
  return severity ? severity.toUpperCase() : "UNKNOWN";
}

/** DES-003: badge tone per severity — chosen so critical (red/danger), high
 * (amber/warn) and medium (purple/validating) are distinguishable by hue alone,
 * not just by lightness, for color-blind operators; the text label carries the
 * rest of the distinction. */
export function severityBadgeTone(severity: string): BadgeTone {
  switch (severity.toLowerCase()) {
    case "critical":
      return "danger";
    case "high":
      return "warn";
    case "medium":
      return "validating";
    case "low":
      return "ok";
    case "triage":
      return "awaiting";
    default:
      return "neutral";
  }
}

/** Sort rank for the known severities (lower sorts first); anything free-form/
 * unrecognized sorts after "low" but is still sorted internally by recency. */
const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity.toLowerCase()] ?? 4;
}

/** DES-003: alerts must sort by SEVERITY first (critical → high → medium → low →
 * anything else), then by recency within the same severity — never plain
 * newest-first, or a critical alert can scroll past a screenful of noise. */
export function sortAlertsBySeverity(alerts: Alert[]): Alert[] {
  return [...alerts].sort((a, b) => {
    const rankDiff = severityRank(a.severity) - severityRank(b.severity);
    if (rankDiff !== 0) return rankDiff;
    return Date.parse(b.created_at) - Date.parse(a.created_at);
  });
}

export function kbStatusTone(status: KbStatus): PillTone {
  switch (status) {
    case "extracted":
      return "ok";
    case "selected":
      return "accent";
    case "failed":
      return "danger";
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}
