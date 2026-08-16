/** Fetch client for the portfolio attention screen (Phase C). */

import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";
import type { PortfolioResponse } from "./types";

export class PortfolioApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "PortfolioApiError";
  }
}

export async function fetchPortfolio(): Promise<PortfolioResponse> {
  const res = await fetchWithTimeout(apiUrl(API_BASE, "/api/projects/portfolio", "GET"), {
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new PortfolioApiError(detail, res.status);
  }
  return res.json() as Promise<PortfolioResponse>;
}
