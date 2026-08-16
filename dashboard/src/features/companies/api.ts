import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "@/lib/fetchTimeout";
import type { CompaniesPayload, Company, CompanyGoal, CompanyProduct, RevenueSource, RevenueVertical } from "./contracts";

export class CompaniesApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "CompaniesApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown, fallback = ""): string { return typeof value === "string" ? value : fallback; }
function number(value: unknown, fallback = 0): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function nullableText(value: unknown): string | null { return typeof value === "string" ? value : null; }
function nullableNumber(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }

async function getJson(path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) throw new CompaniesApiError("The Companies API did not respond in time — it may be down or overloaded.", 0);
    throw new CompaniesApiError("Could not reach the Companies API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch { /* Keep the response status text. */ }
    throw new CompaniesApiError(message, response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new CompaniesApiError("The Companies API returned invalid JSON.", response.status);
  }
}

function normalizeProduct(value: unknown): CompanyProduct {
  const record = isRecord(value) ? value : {};
  return { id: text(record.id), company_id: text(record.company_id), slug: text(record.slug), name: text(record.name, "(unnamed product)") };
}
function normalizeCompany(value: unknown): Company {
  const record = isRecord(value) ? value : {};
  return { id: text(record.id), slug: text(record.slug), name: text(record.name, "(unnamed company)"), products: array(record.products).map(normalizeProduct) };
}
function normalizeGoal(value: unknown): CompanyGoal | null {
  const record = isRecord(value) ? value : {};
  const horizon = record.horizon;
  const status = record.status;
  if ((horizon !== "long_term" && horizon !== "short_term") || !["active", "paused", "achieved", "archived"].includes(String(status))) return null;
  return { id: text(record.id), org_company_id: text(record.org_company_id), title: text(record.title, "(untitled goal)"), horizon, status: status as CompanyGoal["status"] };
}
function normalizeVertical(value: unknown): RevenueVertical {
  const record = isRecord(value) ? value : {};
  // roas_collected: collected revenue over spend — deliberately named apart
  // from the sibling endpoint's ad-platform-attributed ROAS. The contract is
  // null-honest end-to-end: absence is null, rendered as an em-dash, never 0.
  return {
    vertical: text(record.vertical),
    revenue_usd: nullableNumber(record.revenue_usd),
    ad_spend_usd: nullableNumber(record.ad_spend_usd),
    roas_collected: nullableNumber(record.roas_collected),
    refunds_usd: nullableNumber(record.refunds_usd),
    days_with_data: nullableNumber(record.days_with_data),
  };
}
function normalizeSource(value: unknown): RevenueSource | null {
  const record = isRecord(value) ? value : {};
  // Preserve new or unexpected backend statuses. The card renders those as an
  // amber literal instead of silently removing an important health signal.
  return {
    key: text(record.key),
    status: text(record.status, "unknown"),
    last_ok_at: nullableText(record.last_ok_at),
    last_day: nullableText(record.last_day),
    consecutive_failures: number(record.consecutive_failures),
  };
}

/** Same-origin reads through the dashboard proxy; no direct FastAPI origin. */
export async function companiesPayload(): Promise<CompaniesPayload> {
  const [companiesBody, goalsBody, revenueBody] = await Promise.all([
    getJson("/api/orgdims/companies"),
    getJson("/api/company-goals"),
    getJson("/api/revenue/verticals?days=7"),
  ]);
  const companies = isRecord(companiesBody) ? companiesBody : {};
  const goals = isRecord(goalsBody) ? goalsBody : {};
  const revenue = isRecord(revenueBody) ? revenueBody : {};
  return {
    companies: array(companies.companies).map(normalizeCompany),
    goals: array(goals.goals).reduce<CompanyGoal[]>((items, value) => {
      const goal = normalizeGoal(value);
      if (goal) items.push(goal);
      return items;
    }, []),
    revenue: {
      days: number(revenue.days),
      generated_at: text(revenue.generated_at),
      verticals: array(revenue.verticals).map(normalizeVertical),
      sources: array(revenue.sources).reduce<RevenueSource[]>((items, value) => {
        const source = normalizeSource(value);
        if (source) items.push(source);
        return items;
      }, []),
    },
  };
}
