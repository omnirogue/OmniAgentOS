"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { sortBrokenFirst } from "./logic";
import type { CapabilityHealth, HealthPayload } from "./types";

function isHealthPayload(value: unknown): value is HealthPayload {
  return typeof value === "object" && value !== null && Array.isArray((value as { capabilities?: unknown }).capabilities);
}

/** Fetches /api/health and returns the capabilities ALREADY broken-first
 * sorted — the default ordering requirement (DOWN -> ... -> OK, no click
 * required) lives here so every consumer of this hook gets it for free
 * rather than each caller having to remember to apply it. */
export function useHealthData() {
  const [capabilities, setCapabilities] = useState<CapabilityHealth[] | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetchWithTimeout("/api/health", { cache: "no-store", headers: { Accept: "application/json" } });
      const body: unknown = await response.json();
      if (!response.ok || !isHealthPayload(body)) {
        const message = typeof body === "object" && body && "error" in body ? String((body as { error: unknown }).error) : `HTTP ${response.status}`;
        throw new Error(message);
      }
      setCapabilities(sortBrokenFirst(body.capabilities));
      setGeneratedAt(body.generated_at);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load capability health.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { capabilities, generatedAt, loading, error, refresh };
}
