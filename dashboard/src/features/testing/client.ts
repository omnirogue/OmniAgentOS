/**
 * Test Observability fetchers — thin wrappers over `api.get` matching the
 * pinned wire contract in ./types.ts. Fixture fallbacks (local dev with
 * ``NEXT_PUBLIC_USE_TESTING_FIXTURES=true``) live in ./fixtures.ts.
 *
 * Errors propagate: there is no try/catch here that swallows a network or
 * API error into a fixture — fixtures only ever run when USE_FIXTURES is
 * explicitly on. A live API failure reaches the caller as a rejected
 * promise, which the hooks turn into `status: "error"`.
 */

import { api } from "@/lib/api";
import type { TestObsCategory, TestObsOverview, TestObsSeriesResponse, WeakspotsResponse } from "./types";
import { FIXTURE_OVERVIEW, FIXTURE_SERIES, FIXTURE_WEAKSPOTS, USE_FIXTURES } from "./fixtures";

export function fetchTestObsOverview(): Promise<TestObsOverview> {
  if (USE_FIXTURES) return Promise.resolve(FIXTURE_OVERVIEW);
  return api.get<TestObsOverview>("/api/testobs/overview");
}

export function fetchTestObsSeries(category: TestObsCategory, days = 90): Promise<TestObsSeriesResponse> {
  if (USE_FIXTURES) {
    const fx = FIXTURE_SERIES[category];
    if (!fx) return Promise.resolve({ category, days, available: false, reason: "unknown category" });
    return Promise.resolve({ ...fx, days } as TestObsSeriesResponse);
  }
  return api.get<TestObsSeriesResponse>(
    `/api/testobs/series?category=${encodeURIComponent(category)}&days=${days}`,
  );
}

export function fetchTestObsWeakspots(): Promise<WeakspotsResponse> {
  if (USE_FIXTURES) return Promise.resolve(FIXTURE_WEAKSPOTS);
  return api.get<WeakspotsResponse>("/api/testobs/weakspots");
}
