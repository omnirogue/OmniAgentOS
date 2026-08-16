/** Defensive fetch client for the FROZEN `GET /api/revenue` route (backend
 * builds it in parallel — see .fusion/REVENUE-DASH.md). Mirrors
 * features/steward/api.ts's normalize-everything-defensively style so a
 * malformed/partial payload never crashes this real-money page. */
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import { FIXTURE_REVENUE_DAY, USE_REVENUE_FIXTURES } from "./fixtures";
import {
  DATA_QUALITY_LEVELS,
  type DataQualityNote,
  type RevenueCampaignRow,
  type RevenueCoverage,
  type RevenueDay,
  type RevenueSourceRow,
  type SourceStatus,
  type RevenueTotals,
  type RevenueVerticalRow,
} from "./contracts";

export class RevenueApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "RevenueApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}
function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function integer(value: unknown, fallback = 0): number {
  return Number.isInteger(value) ? (value as number) : fallback;
}
function oneOf<T extends readonly string[]>(value: unknown, values: T, fallback: T[number]): T[number] {
  return typeof value === "string" && (values as readonly string[]).includes(value) ? (value as T[number]) : fallback;
}
function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
function unknownArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

async function getJson(path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new RevenueApiError("The Revenue API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new RevenueApiError("Could not reach the Revenue API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch {
      /* Retain the response status text. */
    }
    throw new RevenueApiError(message, response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new RevenueApiError("The Revenue API returned invalid JSON.", response.status);
  }
}

// --- normalization -----------------------------------------------------------

function normalizeTotals(value: unknown): RevenueTotals {
  const rec = isRecord(value) ? value : {};
  return {
    revenue_usd: number(rec.revenue_usd),
    ad_spend_usd: number(rec.ad_spend_usd),
    gross_contribution_usd: number(rec.gross_contribution_usd),
    blended_roas: nullableNumber(rec.blended_roas),
    roi: nullableNumber(rec.roi),
    payment_failures: Math.max(0, integer(rec.payment_failures)),
    note: text(rec.note),
  };
}

function normalizeVerticalRow(value: unknown): RevenueVerticalRow {
  const rec = isRecord(value) ? value : {};
  return {
    vertical: text(rec.vertical, "unknown"),
    revenue_usd: number(rec.revenue_usd),
    ad_spend_usd: number(rec.ad_spend_usd),
    gross_contribution_usd: number(rec.gross_contribution_usd),
    roas: nullableNumber(rec.roas),
    blended_roas: nullableNumber(rec.blended_roas),
    roi: nullableNumber(rec.roi),
    payment_failures: Math.max(0, integer(rec.payment_failures)),
  };
}

function normalizeSourceRow(value: unknown): RevenueSourceRow {
  const rec = isRecord(value) ? value : {};
  return {
    vertical: text(rec.vertical, "unknown"),
    source: text(rec.source, "unknown"),
    revenue_usd: number(rec.revenue_usd),
    ad_spend_usd: number(rec.ad_spend_usd),
    payment_failures: Math.max(0, integer(rec.payment_failures)),
  };
}

function normalizeCampaignRow(value: unknown): RevenueCampaignRow {
  const rec = isRecord(value) ? value : {};
  return {
    vertical: text(rec.vertical, "unknown"),
    source: text(rec.source, "unknown"),
    campaign_id: text(rec.campaign_id),
    campaign_name: text(rec.campaign_name, "(unnamed campaign)"),
    ad_spend_usd: number(rec.ad_spend_usd),
    roas: nullableNumber(rec.roas),
    revenue_attributed_usd: number(rec.revenue_attributed_usd),
  };
}

function normalizeDataQualityNote(value: unknown): DataQualityNote {
  const rec = isRecord(value) ? value : {};
  return {
    level: oneOf(rec.level, DATA_QUALITY_LEVELS, "info"),
    message: text(rec.message),
  };
}

function normalizeSourceStatus(value: unknown): SourceStatus | null {
  if (!isRecord(value)) return null;
  const status = value.status;
  if (
    typeof value.vertical !== "string" ||
    !value.vertical ||
    typeof value.source !== "string" ||
    !value.source ||
    (status !== "ok" && status !== "failed" && status !== "unconfigured") ||
    typeof value.message !== "string" ||
    !Number.isInteger(value.consecutive_failures)
  ) {
    return null;
  }
  return {
    vertical: value.vertical,
    source: value.source,
    status,
    message: value.message,
    consecutive_failures: Math.max(0, integer(value.consecutive_failures)),
  };
}

function normalizeCoverage(value: unknown): RevenueCoverage {
  const rec = isRecord(value) ? value : {};
  return {
    revenue_sources_live: stringArray(rec.revenue_sources_live),
    spend_sources_live: stringArray(rec.spend_sources_live),
    not_wired: stringArray(rec.not_wired),
  };
}

export function normalizeRevenueDay(value: unknown): RevenueDay {
  const rec = isRecord(value) ? value : {};
  return {
    day: text(rec.day),
    timezone: text(rec.timezone, "America/New_York"),
    generated_at: text(rec.generated_at),
    totals: normalizeTotals(rec.totals),
    by_vertical: unknownArray(rec.by_vertical).map(normalizeVerticalRow),
    by_source: unknownArray(rec.by_source).map(normalizeSourceRow),
    by_campaign: unknownArray(rec.by_campaign).map(normalizeCampaignRow),
    data_quality: unknownArray(rec.data_quality).map(normalizeDataQualityNote),
    source_status: unknownArray(rec.source_status).reduce<SourceStatus[]>((statuses, item) => {
      const status = normalizeSourceStatus(item);
      if (status) statuses.push(status);
      return statuses;
    }, []),
    coverage: normalizeCoverage(rec.coverage),
  };
}

// --- fetch functions ----------------------------------------------------------
// LITERAL path: GET /api/revenue?day=YYYY-MM-DD (day omitted -> server default,
// yesterday in America/New_York).

/** Fetch one day's revenue/P&L snapshot. `day` is YYYY-MM-DD; omit it to get
 * the server's default (yesterday, America/New_York). */
export async function revenue(day?: string): Promise<RevenueDay> {
  if (USE_REVENUE_FIXTURES) return day ? { ...FIXTURE_REVENUE_DAY, day } : FIXTURE_REVENUE_DAY;
  const qs = day ? `?day=${encodeURIComponent(day)}` : "";
  return normalizeRevenueDay(await getJson(`/api/revenue${qs}`));
}
