"use client";

import { useCallback, useEffect, useState } from "react";
import { companiesPayload, CompaniesApiError } from "./api";
import type { CompaniesPortfolio } from "./contracts";
import { buildCompaniesPortfolio } from "./logic";

function errorMessage(reason: unknown): string {
  return reason instanceof CompaniesApiError || reason instanceof Error ? reason.message : "Failed to load companies.";
}

/** Company details are DB-backed read models. Load on entry and expose an
 * explicit retry rather than spending background requests on this slow surface. */
export function useCompanies() {
  const [data, setData] = useState<CompaniesPortfolio | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(buildCompaniesPortfolio(await companiesPayload()));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  return { data, loading, error, refresh };
}
