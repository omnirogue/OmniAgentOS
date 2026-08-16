import { Badge, type BadgeTone } from "@/design";
import type { CapabilityStatus } from "./types";

/**
 * Every status gets its own distinct badge tone — none of the six collapse
 * onto each other, and only OK gets the green "ok" tone. UNVERIFIED
 * deliberately does NOT get the muted/grey "invalid" tone real absence-of-
 * data statuses might reach for by default: this is the "we are not even
 * watching this" bucket the brief calls out as easy to accidentally hide, so
 * it gets "challenger" — a bold, saturated tone of its own, not a color any
 * other status in this table uses — instead of fading into the background.
 */
const STATUS_TONE: Record<CapabilityStatus, BadgeTone> = {
  OK: "ok",
  DEGRADED: "warn",
  DOWN: "danger",
  STALE: "paused",
  CANNOT_EVALUATE: "validating",
  UNVERIFIED: "challenger",
};

const STATUS_LABEL: Record<CapabilityStatus, string> = {
  OK: "OK",
  DEGRADED: "Degraded",
  DOWN: "Down",
  STALE: "Stale",
  CANNOT_EVALUATE: "Cannot evaluate",
  UNVERIFIED: "Unverified",
};

export function StatusBadge({ status }: { status: CapabilityStatus }) {
  return (
    <Badge tone={STATUS_TONE[status]} category="capabilityStatus">
      {STATUS_LABEL[status]}
    </Badge>
  );
}

export { STATUS_TONE, STATUS_LABEL };
