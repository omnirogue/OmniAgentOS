import type { BadgeTone } from "@/design/Badge";

export interface AttemptOutcomePresentation {
  label: string;
  tone: BadgeTone;
}

/** Render review truth independently from the worker process lifecycle. */
export function attemptOutcome(
  endReason: string | null | undefined,
  isOpen: boolean,
): AttemptOutcomePresentation {
  const reason = endReason?.trim().toLowerCase();
  if (!reason) {
    return isOpen
      ? { label: "IN PROGRESS", tone: "running" }
      : { label: "UNKNOWN", tone: "warn" };
  }
  if (reason === "completed") {
    return { label: "ACCEPTED", tone: "completed" };
  }
  if (reason === "review_denied") {
    return { label: "DENIED", tone: "danger" };
  }
  if (["usage_limited", "rate_limited", "budget", "rerouted", "split"].includes(reason)) {
    return { label: reason.replace(/_/g, " ").toUpperCase(), tone: "warn" };
  }
  return { label: reason.replace(/_/g, " ").toUpperCase(), tone: "danger" };
}
