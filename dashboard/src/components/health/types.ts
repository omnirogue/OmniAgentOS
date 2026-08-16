/** Re-exported so client components share the exact server contract instead
 * of a hand-duplicated shape that could drift from what /api/health emits. */
import type { CapabilityStatus } from "@/app/api/health/route";

export type { CapabilityHealth, CapabilityDetail, CapabilityStatus, HealthPayload } from "@/app/api/health/route";

export type Company = "omniagentos" | "initech" | "globex" | "acmeuni" | "hooli" | "estate";
export type Kind = "mechanical-automation" | "llm-loop" | "external-service" | "data-store" | "human-process";

export type HealthFiltersState = {
  company: Company | "";
  kind: Kind | "";
  status: CapabilityStatus | "";
};
