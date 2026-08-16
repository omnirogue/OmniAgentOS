"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, ErrorState, Loading, Stat } from "@/design";
import { API_BASE } from "../../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../../lib/fetchTimeout";
import { formatMoney } from "../format";
import styles from "../cash.module.css";

/**
 * F07: a compact daily revenue summary above the existing banking report —
 * revenue, ad spend, gross contribution, ROI, blended ROAS, and payment
 * failures from the public `GET /api/revenue` route. This is a deliberately
 * minimal, cash-feature-local fetch (LITERAL path: `/api/revenue`, day omitted
 * -> server default, yesterday in America/New_York) rather than reaching into
 * features/revenue's full by-vertical/by-source page — /cash only needs the
 * headline totals, not the whole P&L breakdown.
 *
 * Honest states: loading shows a skeleton, a fetch failure shows the real
 * reason (never a silent $0 row), and the "gross contribution" label is
 * deliberately never "profit" — same rule as features/revenue/contracts.ts.
 */

interface RevenueSummaryTotals {
  revenue_usd: number;
  ad_spend_usd: number;
  /** revenue_usd − ad_spend_usd. NOT net profit. */
  gross_contribution_usd: number;
  payment_failures: number;
  /** (revenue − ad spend) / ad spend. null when no ad spend — render "—". */
  roi: number | null;
  /** revenue / ad spend (blended real, NOT Meta attribution). null when no spend. */
  blended_roas: number | null;
  note: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
/** A finite number, or null. Zero-spend ratios arrive as null and must NEVER be
 * coerced to 0 — "0%" would read as a zero return, not "no ad spend". */
function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/** ROI ratio → signed whole percent (0.59 → "+59%", −0.4 → "−40%"), null → "—". */
function formatRoiPercent(value: number | null): string {
  if (value === null) return "—";
  const pct = Math.round(value * 100);
  return `${pct > 0 ? "+" : ""}${pct.toLocaleString("en-US")}%`;
}
/** ROAS ratio → "N.NNx", null → "—". */
function formatRoas(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(2)}x`;
}

function normalizeTotals(value: unknown): RevenueSummaryTotals | null {
  // HONESTY RULE (review F07): a 200 with a malformed/partial body must NOT
  // normalize into favourable zeros — $0.00 revenue and 0 payment failures
  // rendered from `{}` is indistinguishable from a genuinely quiet day. Every
  // required field must be a finite number or the whole payload is malformed.
  // roi/blended_roas are NULLABLE (null == no ad spend), so they are validated
  // separately and are never part of the "malformed" gate.
  if (!isRecord(value) || !isRecord(value.totals)) return null;
  const rec = value.totals;
  const required = [
    rec.revenue_usd,
    rec.ad_spend_usd,
    rec.gross_contribution_usd,
    rec.payment_failures,
  ];
  if (!required.every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  return {
    revenue_usd: number(rec.revenue_usd),
    ad_spend_usd: number(rec.ad_spend_usd),
    gross_contribution_usd: number(rec.gross_contribution_usd),
    payment_failures: Math.max(0, number(rec.payment_failures)),
    roi: nullableNumber(rec.roi),
    blended_roas: nullableNumber(rec.blended_roas),
    note: text(rec.note),
  };
}

function normalizeDay(value: unknown): string {
  return isRecord(value) ? text(value.day) : "";
}

// LITERAL path: GET /api/revenue (day omitted -> server default, yesterday
// in America/New_York) — see features/revenue/contracts.ts for the frozen
// contract this route serves.
const REVENUE_SUMMARY_PATH = "/api/revenue";

async function fetchRevenueSummary(): Promise<{ totals: RevenueSummaryTotals; day: string }> {
  let response: Response;
  try {
    response = await fetchWithTimeout(API_BASE + REVENUE_SUMMARY_PATH, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new Error("The Revenue API did not respond in time — it may be down or overloaded.");
    }
    throw new Error("Could not reach the Revenue API.");
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch {
      /* Retain the response status text. */
    }
    throw new Error(message);
  }
  const body: unknown = await response.json();
  const totals = normalizeTotals(body);
  if (totals === null) {
    throw new Error("The Revenue API returned a malformed response — figures withheld rather than shown as zero.");
  }
  return { totals, day: normalizeDay(body) };
}

export function RevenueStrip() {
  const [totals, setTotals] = useState<RevenueSummaryTotals | null>(null);
  const [day, setDay] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const snapshot = await fetchRevenueSummary();
      setTotals(snapshot.totals);
      setDay(snapshot.day);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to load revenue.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Card className={styles.revenueStrip}>
      <div className={styles.revenueStripHeading}>
        <h2>Revenue (daily)</h2>
        {totals && day ? <span className={styles.faint}>{day}</span> : null}
      </div>
      {loading && !totals ? <Loading label="Loading revenue…" /> : null}
      {error && !totals ? (
        <ErrorState message={`Revenue unavailable: ${error}`} onRetry={() => void refresh()} />
      ) : null}
      {totals ? (
        <div className={styles.statGrid}>
          <Stat label="Revenue" value={formatMoney(totals.revenue_usd)} tone="accent" />
          <Stat label="Ad Spend" value={formatMoney(totals.ad_spend_usd)} tone="warn" />
          <Stat
            label="Gross Contribution (rev − ad spend)"
            value={formatMoney(totals.gross_contribution_usd)}
            tone={totals.gross_contribution_usd >= 0 ? "ok" : "danger"}
            hint={totals.note}
          />
          <Stat
            label="ROI (ad spend)"
            value={formatRoiPercent(totals.roi)}
            tone={totals.roi === null ? "default" : totals.roi >= 0 ? "ok" : "danger"}
            hint="(revenue − ad spend) ÷ ad spend. Shows — when there is no ad spend."
          />
          <Stat
            label="Blended ROAS"
            value={formatRoas(totals.blended_roas)}
            tone={totals.blended_roas === null ? "default" : totals.blended_roas >= 1 ? "ok" : "warn"}
            hint="revenue ÷ ad spend (all collected revenue, not Meta attribution)."
          />
          <Stat
            label="Payment Failures"
            value={totals.payment_failures}
            tone={totals.payment_failures > 0 ? "danger" : "default"}
          />
        </div>
      ) : null}
    </Card>
  );
}
