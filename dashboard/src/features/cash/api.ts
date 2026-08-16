/** Defensive fetch client for `GET /api/banking`. Mirrors features/revenue/api.ts's
 * normalize-everything-defensively style so a malformed/partial payload never
 * crashes this real-money page. */
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import { FIXTURE_CASH_DAY, USE_CASH_FIXTURES } from "./fixtures";
import {
  DATA_QUALITY_LEVELS,
  type CashAccountRow,
  type CashCoverage,
  type CashDay,
  type CashMonthToDate,
  type CashTotals,
  type CashTransactionRow,
  type DataQualityNote,
} from "./contracts";

export class CashApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "CashApiError";
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
function nullableText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
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
      throw new CashApiError("The Cash API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new CashApiError("Could not reach the Cash API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch {
      /* Retain the response status text. */
    }
    throw new CashApiError(message, response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new CashApiError("The Cash API returned invalid JSON.", response.status);
  }
}

// --- normalization -----------------------------------------------------------

function normalizeTotals(value: unknown): CashTotals {
  const rec = isRecord(value) ? value : {};
  return {
    cash_balance_usd: number(rec.cash_balance_usd),
    money_in_usd: number(rec.money_in_usd),
    money_out_usd: number(rec.money_out_usd),
    net_flow_usd: number(rec.net_flow_usd),
    note: text(rec.note),
    available_credit_usd: number(rec.available_credit_usd),
    available_credit_note: text(rec.available_credit_note),
    cash_sweep_note: nullableText(rec.cash_sweep_note),
  };
}

function normalizeMonthToDate(value: unknown): CashMonthToDate {
  const rec = isRecord(value) ? value : {};
  return {
    money_in_usd: number(rec.money_in_usd),
    money_out_usd: number(rec.money_out_usd),
    net_flow_usd: number(rec.net_flow_usd),
  };
}

function normalizeAccountRow(value: unknown): CashAccountRow {
  const rec = isRecord(value) ? value : {};
  return {
    provider: text(rec.provider, "unknown"),
    brand: text(rec.brand),
    name: text(rec.name, "(unnamed account)"),
    mask: text(rec.mask),
    kind: text(rec.kind),
    balance_usd: number(rec.balance_usd),
    money_in_usd: number(rec.money_in_usd),
    money_out_usd: number(rec.money_out_usd),
  };
}

function normalizeTransactionRow(value: unknown): CashTransactionRow {
  const rec = isRecord(value) ? value : {};
  return {
    account: text(rec.account, "unknown"),
    posted_at: nullableText(rec.posted_at),
    amount_usd: number(rec.amount_usd),
    description: text(rec.description),
  };
}

function normalizeDataQualityNote(value: unknown): DataQualityNote {
  const rec = isRecord(value) ? value : {};
  return {
    level: oneOf(rec.level, DATA_QUALITY_LEVELS, "info"),
    message: text(rec.message),
  };
}

function normalizeCoverage(value: unknown): CashCoverage {
  const rec = isRecord(value) ? value : {};
  return {
    banks_live: stringArray(rec.banks_live),
    not_wired: stringArray(rec.not_wired),
  };
}

export function normalizeCashDay(value: unknown): CashDay {
  const rec = isRecord(value) ? value : {};
  return {
    day: text(rec.day),
    timezone: text(rec.timezone, "America/New_York"),
    generated_at: text(rec.generated_at),
    totals: normalizeTotals(rec.totals),
    month_to_date: normalizeMonthToDate(rec.month_to_date),
    by_account: unknownArray(rec.by_account).map(normalizeAccountRow),
    largest_transactions: unknownArray(rec.largest_transactions).map(normalizeTransactionRow),
    data_quality: unknownArray(rec.data_quality).map(normalizeDataQualityNote),
    coverage: normalizeCoverage(rec.coverage),
  };
}

// --- fetch functions ----------------------------------------------------------
// LITERAL path: GET /api/banking?day=YYYY-MM-DD (day omitted -> server default,
// yesterday in America/New_York).

/** Fetch one day's cash / banking snapshot. `day` is YYYY-MM-DD; omit it to get
 * the server's default (yesterday, America/New_York). */
export async function banking(day?: string): Promise<CashDay> {
  if (USE_CASH_FIXTURES) return day ? { ...FIXTURE_CASH_DAY, day } : FIXTURE_CASH_DAY;
  const qs = day ? `?day=${encodeURIComponent(day)}` : "";
  return normalizeCashDay(await getJson(`/api/banking${qs}`));
}
