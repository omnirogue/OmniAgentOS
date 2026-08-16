/**
 * Estate Compute fetcher — a thin wrapper over `api.get` matching the pinned
 * wire contract in ./types.ts. Fixture fallback (local dev with
 * ``NEXT_PUBLIC_USE_COMPUTE_FIXTURES=true``) lives in ./fixtures.ts.
 *
 * Errors propagate: there is no try/catch here that swallows a network or
 * API error into a fixture — the fixture short-circuit only fires when
 * USE_FIXTURES is explicitly on. A live API failure reaches the caller as a
 * rejected promise, which the hooks turn into `status: "error"`.
 */

import { api } from "@/lib/api";
import type { ComputeEstate } from "./types";
import { FIXTURE_ESTATE, USE_FIXTURES } from "./fixtures";

export function fetchComputeEstate(): Promise<ComputeEstate> {
  if (USE_FIXTURES) return Promise.resolve(FIXTURE_ESTATE);
  return api.get<ComputeEstate>("/api/compute/estate");
}
